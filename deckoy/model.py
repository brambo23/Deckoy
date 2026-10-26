"""Settings with tolerant JSON load/save (atomic, versioned, .bak on corrupt)."""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, fields
from typing import Any, Dict, Optional

log = logging.getLogger("deckoy.model")

SCHEMA_VERSION = 1


@dataclass
class Settings:
    dock_host: str = ""        # blank = auto (mDNS)
    dock_port: int = 0         # 0 = auto
    brightness: int = 63
    sleep_seconds: int = 3600
    start_with_windows: bool = False
    proxy_bind: str = ""               # the address advertised via mDNS; blank = this PC's LAN address toward the dock
    proxy_listen_all: bool = True      # listen on all local addresses (only this PC can connect); needed, see README
    proxy_deck_port: int = 20002
    proxy_dock_port: int = 5343
    proxy_forward_unknown: bool = False
    proxy_naming: str = "distinct"     # distinct | same (mDNS instance names)
    upstream_dock_port: int = 5343     # the real dock's own service port (advanced; not in the settings dialog)
    proxy_reannounce: str = "deck"     # off | deck | both: re-announce via mDNS after the Elgato app drops its session
    proxy_reannounce_new_port: bool = True
    proxy_reannounce_delay_ms: int = 1500

    def to_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "Settings":
        """Tolerant: unknown keys are ignored, wrongly typed values keep the default, numbers are clamped."""
        s = Settings()
        for f in fields(Settings):
            if d and f.name in d:
                v, cur = d[f.name], getattr(s, f.name)
                if isinstance(cur, bool):
                    ok = isinstance(v, bool)
                else:
                    ok = isinstance(v, type(cur)) and not isinstance(v, bool)
                if ok:
                    setattr(s, f.name, v)
        s.brightness = max(0, min(100, s.brightness))
        s.sleep_seconds = max(0, min(65535, s.sleep_seconds))
        s.dock_port = s.dock_port if 0 <= s.dock_port <= 65535 else 0
        s.upstream_dock_port = s.upstream_dock_port if 1 <= s.upstream_dock_port <= 65535 else 5343
        if s.proxy_naming not in ("distinct", "same"):
            s.proxy_naming = "distinct"
        if s.proxy_reannounce not in ("off", "deck", "both"):
            s.proxy_reannounce = "deck"
        s.proxy_reannounce_delay_ms = max(0, min(30000, s.proxy_reannounce_delay_ms))
        s.proxy_deck_port = s.proxy_deck_port if 1 <= s.proxy_deck_port <= 65535 else 20002
        s.proxy_dock_port = s.proxy_dock_port if 1 <= s.proxy_dock_port <= 65535 else 5343
        return s


def load_settings(path: str) -> Settings:
    """Load settings.json. Missing -> defaults. Corrupt -> keep a timestamped .bak copy and start from defaults."""
    if not os.path.exists(path):
        return Settings()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("settings root is not an object")
        v = data.get("version", SCHEMA_VERSION)
        if isinstance(v, int) and v > SCHEMA_VERSION:
            log.warning("settings version %s is newer than this app (%s); loading what we understand", v, SCHEMA_VERSION)
        return Settings.from_dict(data)
    except Exception as ex:
        bak = f"{path}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
        log.error("settings unreadable (%s); saved a copy as %s and starting fresh", ex, bak)
        try:
            shutil.copy2(path, bak)
        except OSError:
            pass
        return Settings()


def save_settings(settings: Settings, path: str) -> None:
    """Atomic write (temp file + replace)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": SCHEMA_VERSION, **settings.to_dict()}, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def app_data_dir() -> str:
    if os.environ.get("DECKOY_HOME"):  # test / portable override
        return os.environ["DECKOY_HOME"]
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Deckoy")
