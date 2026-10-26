"""Counters shown in the tray / main window and kept across restarts (so "hidden today" survives a restart)."""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Callable, Dict, List, Optional

log = logging.getLogger("deckoy.proxy")

# A downstream (Elgato app) connection shorter than this is the app's own start-up "priming" connection.
PRIMING_MAX_SECONDS = 3.0


def _today(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


class ProxyStats:
    def __init__(self, path: Optional[str] = None, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
        self.started = clock()
        self.established_after = PRIMING_MAX_SECONDS
        # upstream (Deckoy <-> real dock)
        self.upstream_connected = False
        self.upstream_drops = 0
        self.drops_hidden = 0             # drops that happened while the Elgato app was connected
        self.downtime_s = 0.0
        self.last_drop: Optional[float] = None
        self._down_since: Optional[float] = None
        # downstream (Elgato app <-> Deckoy)
        self.app_connected = 0            # currently open deck-service connections
        self.app_connections = 0          # deck-service connections accepted (the app opens a few short ones at start-up)
        self.app_disconnects = 0          # established (> 3 s) sessions that ended; stays 0 unless the app quits or gives up
        # commands
        self.answered_local = 0
        self.forwarded = 0
        self.blocked = 0
        self.unknown: List[str] = []      # last unknown/blocked commands (hex), newest last
        self.dock_drops: List[float] = []   # times the dock link dropped (persisted, newest last)
        self.app_drops: List[dict] = []   # every time the Elgato app ended an established session: {t, duration, by_us}
        self.reannounces = 0
        self.daily: Dict[str, Dict[str, int]] = {}
        self._load()

    # ------------------------------------------------------------------ upstream
    def upstream_up(self) -> None:
        now = self.clock()
        self.upstream_connected = True
        if self._down_since is not None:
            self.downtime_s += now - self._down_since
            self._down_since = None
        self._save()

    def upstream_down(self) -> None:
        """Called when a connected upstream session is lost."""
        now = self.clock()
        if not self.upstream_connected and self._down_since is not None:
            return
        was_connected = self.upstream_connected
        self.upstream_connected = False
        self._down_since = now
        if was_connected:
            self.upstream_drops += 1
            self.last_drop = now
            self.dock_drops.append(now)
            del self.dock_drops[:-100]
            day = self.daily.setdefault(_today(now), {"drops": 0, "hidden": 0})
            day["drops"] += 1
            if self.app_connected > 0:
                self.drops_hidden += 1
                day["hidden"] += 1
            self._save()

    # ------------------------------------------------------------------ downstream
    def app_opened(self) -> None:
        self.app_connected += 1
        self.app_connections += 1

    def app_closed(self, duration: float) -> None:
        self.app_connected = max(0, self.app_connected - 1)
        if duration >= self.established_after:
            self.app_disconnects += 1
        self._save()

    def record_app_drop(self, duration: float, by_us: bool = False) -> None:
        """The Elgato app's established deck session ended (match these times against display/power events)."""
        self.app_drops.append({"t": self.clock(), "duration": round(duration, 1), "by_us": by_us})
        del self.app_drops[:-100]
        self._save()

    def note_reannounce(self) -> None:
        self.reannounces += 1
        self._save()

    # ------------------------------------------------------------------ commands
    def local(self) -> None:
        self.answered_local += 1

    def forward(self) -> None:
        self.forwarded += 1

    def note_unknown(self, what: str, blocked: bool = False) -> None:
        if blocked:
            self.blocked += 1
        self.unknown.append(what)
        del self.unknown[:-50]

    # ------------------------------------------------------------------ views
    def snapshot(self) -> dict:
        now = self.clock()
        today = self.daily.get(_today(now), {"drops": 0, "hidden": 0})
        down = self.downtime_s + (now - self._down_since if self._down_since is not None else 0.0)
        return {
            "upstream_connected": self.upstream_connected,
            "app_connected": self.app_connected > 0,
            "upstream_drops": self.upstream_drops,
            "drops_hidden": self.drops_hidden,
            "drops_today": today["drops"],
            "hidden_today": today["hidden"],
            "last_drop": self.last_drop,
            "downtime_s": round(down, 1),
            "app_connections": self.app_connections,
            "app_disconnects": self.app_disconnects,
            "answered_local": self.answered_local,
            "forwarded": self.forwarded,
            "blocked": self.blocked,
            "app_drop_count": len(self.app_drops),
            "app_drops_recent": list(self.app_drops[-10:]),
            "dock_drops_recent": list(self.dock_drops[-10:]),
            "last_app_drop": self.app_drops[-1]["t"] if self.app_drops else None,
            "reannounces": self.reannounces,
            "unknown_recent": list(self.unknown[-5:]),
            "since": self.started,
        }

    def summary_lines(self) -> List[str]:
        s = self.snapshot()
        last = time.strftime("%H:%M:%S", time.localtime(s["last_drop"])) if s["last_drop"] else "never"
        return [
            f"Dock link: {'connected' if s['upstream_connected'] else 'DOWN'}",
            f"Elgato app: {'connected' if s['app_connected'] else 'not connected'}",
            f"Drops hidden today: {s['hidden_today']} (total drops today {s['drops_today']})",
            f"Last dock drop: {last}",
        ]

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.upstream_drops = int(d.get("upstream_drops", 0))
            self.drops_hidden = int(d.get("drops_hidden", 0))
            self.downtime_s = float(d.get("downtime_s", 0.0))
            self.last_drop = d.get("last_drop")
            self.daily = {k: {"drops": int(v.get("drops", 0)), "hidden": int(v.get("hidden", 0))}
                          for k, v in (d.get("daily") or {}).items()}
            self.app_drops = [x for x in (d.get("app_drops") or []) if isinstance(x, dict) and "t" in x][-100:]
            self.dock_drops = [float(x) for x in (d.get("dock_drops") or []) if isinstance(x, (int, float))][-100:]
            self.reannounces = int(d.get("reannounces", 0))
        except Exception as ex:
            log.warning("proxy stats unreadable, starting fresh: %s", ex)

    def _save(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            keep = dict(sorted(self.daily.items())[-30:])
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"upstream_drops": self.upstream_drops, "drops_hidden": self.drops_hidden,
                           "downtime_s": self.downtime_s, "last_drop": self.last_drop, "daily": keep,
                           "app_drops": self.app_drops, "dock_drops": self.dock_drops,
                           "reannounces": self.reannounces}, f, indent=1)
            os.replace(tmp, self.path)
        except OSError as ex:
            log.warning("saving proxy stats failed: %s", ex)
