"""Finding the deck service: mDNS (_elg._tcp.local.), last-good port (persisted) and TCP-connect-only port probes."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, Iterable, List, Optional

log = logging.getLogger("deckoy.discovery")

SERVICE_TYPE = "_elg._tcp.local."
DOCK_DEVICE_TYPE = "215"  # TXT dt of the dock itself (port 5343); the deck is anything else with a pid
REFRESH_INTERVAL = 30.0


@dataclass
class DeckService:
    host: str
    port: int
    name: Optional[str] = None
    txt: Dict[str, str] = field(default_factory=dict)

    @property
    def serial(self) -> Optional[str]:
        return self.txt.get("sn") or self.name


@dataclass
class Candidate:
    host: str
    name: str
    port: int
    txt: Dict[str, str]


def pick_dock(candidates: Iterable[Candidate]) -> Optional[Candidate]:
    """The dock itself (dt=215, port 5343), as opposed to the deck behind it."""
    return next((c for c in candidates if c.txt.get("dt") == DOCK_DEVICE_TYPE), None)


def pick(candidates: Iterable[Candidate]) -> Optional[DeckService]:
    for c in candidates:
        dt = c.txt.get("dt")
        if dt == DOCK_DEVICE_TYPE:
            continue
        if dt == "213" or "pid" in c.txt:
            return DeckService(c.host, c.port, c.name, c.txt)
    return None


async def discover(timeout: float = 5.0, exclude_hosts: Iterable[str] = (),
                   exclude_names: Iterable[str] = ()) -> Optional[DeckService]:
    """Browse mDNS for `timeout` seconds and pick the deck service. None on any failure.

    `exclude_*` hide records that are Deckoy's own proxy advertisement (otherwise the proxy would find itself).
    """
    hosts, names = set(exclude_hosts), set(exclude_names)
    cands = [c for c in await discover_all(timeout) if c.host not in hosts and c.name not in names]
    return pick(cands)


async def discover_all(timeout: float = 5.0) -> List[Candidate]:
    """Browse mDNS for `timeout` seconds and return every `_elg._tcp` record found (empty on any failure)."""
    try:
        from zeroconf import ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
    except Exception as ex:  # pragma: no cover
        log.warning("zeroconf unavailable: %s", ex)
        return []
    names: List[str] = []

    def handler(zeroconf, service_type, name, state_change):
        if state_change is not ServiceStateChange.Removed and name not in names:
            names.append(name)

    azc = None
    try:
        azc = AsyncZeroconf()
        browser = AsyncServiceBrowser(azc.zeroconf, SERVICE_TYPE, handlers=[handler])
        await asyncio.sleep(timeout)
        await browser.async_cancel()
        cands: List[Candidate] = []
        for name in list(names):
            info = AsyncServiceInfo(SERVICE_TYPE, name)
            if not await info.async_request(azc.zeroconf, 2000):
                continue
            txt = {(k.decode() if isinstance(k, bytes) else k): (v.decode(errors="replace") if isinstance(v, bytes) else (v or ""))
                   for k, v in info.properties.items()}
            addrs = info.parsed_addresses()
            if addrs and info.port:
                cands.append(Candidate(addrs[0], name.split(".")[0], info.port, txt))
        return cands
    except Exception as ex:
        log.warning("mDNS discovery failed: %s", ex)
        return []
    finally:
        if azc is not None:
            try:
                await azc.async_close()
            except Exception:
                pass


class DockLocator:
    """Port choice for a host: explicit setting -> last-good -> mDNS port -> 20002 -> 20001 (connect-only probes)."""

    PROBE_PORTS = (20003, 20002, 20001)
    # The dock's deck port drifts between sessions (seen: 20001, 20002, 20003), so as a last resort scan a range.
    SCAN_RANGE = range(20001, 20021)

    def __init__(self, discover_fn: Callable[[], Awaitable[Optional[DeckService]]] = discover,
                 state_path: Optional[str] = None):
        self._discover = discover_fn
        self.state_path = state_path
        self.probe_ports = list(self.PROBE_PORTS)
        self.probe_timeout = 1.5
        self._good: Dict[str, int] = {}
        self._cached: Optional[DeckService] = None
        self._last_failed: Optional[tuple] = None
        self._bg: Optional[asyncio.Task] = None
        self._load()

    # -- mDNS cache
    @property
    def cached(self) -> Optional[DeckService]:
        return self._cached

    def start_background(self) -> None:
        if self._bg is None:
            self._bg = asyncio.ensure_future(self._refresh_loop())

    def stop(self) -> None:
        if self._bg is not None:
            self._bg.cancel()
            self._bg = None

    async def _refresh_loop(self) -> None:
        while True:
            try:
                svc = await self._discover()
                if svc is not None:
                    if self._cached is None or (self._cached.host, self._cached.port) != (svc.host, svc.port):
                        log.info("mDNS: deck %s at %s:%s", svc.serial, svc.host, svc.port)
                    self._cached = svc
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                log.warning("mDNS refresh failed: %s", ex)
            await asyncio.sleep(REFRESH_INTERVAL)

    async def find_service(self) -> Optional[DeckService]:
        if self._cached is None:
            self._cached = await self._discover()
        return self._cached

    # -- port choice
    async def pick_port(self, host: str, explicit_port: int = 0) -> int:
        h = host.lower()
        if explicit_port > 0:
            if self._last_failed != (h, explicit_port):
                return explicit_port
            log.warning("Device port %s did not work at %s; trying automatic port detection", explicit_port, host)
        cands: List[int] = []
        svc = self._cached
        if svc is not None and svc.host.lower() == h:
            cands.append(svc.port)                         # the mDNS SRV port is the freshest information
        good = self._good.get(h)
        if good and good != explicit_port and good not in cands:
            cands.append(good)                             # then the port that worked last time
        cands += [p for p in self.probe_ports if p not in cands]
        if explicit_port in cands:
            cands.remove(explicit_port)
        for p in cands:
            if await self._reachable(host, p):
                log.info("Deck service at %s answers on port %s", host, p)
                return p
        scan = [p for p in self.SCAN_RANGE if p not in cands and p != explicit_port]
        if scan:
            results = await asyncio.gather(*(self._reachable(host, p) for p in scan))
            open_ports = [p for p, ok in zip(scan, results) if ok]
            if open_ports:
                log.info("Deck service at %s found by range scan on port %s", host, open_ports[0])
                return open_ports[0]
        log.warning("No deck port reachable at %s (tried %s and %s-%s)", host, cands, scan[0] if scan else "-", scan[-1] if scan else "-")
        return explicit_port if explicit_port > 0 else cands[0]

    async def _reachable(self, host: str, port: int) -> bool:
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(host, port), self.probe_timeout)  # connect only
            w.close()
            return True
        except Exception:
            return False

    def mark_good(self, host: str, port: int) -> None:
        h = host.lower()
        if self._last_failed == (h, port):
            self._last_failed = None
        if self._good.get(h) != port:
            self._good[h] = port
            self._save()

    def mark_failed(self, host: str, port: int = 0) -> None:
        h = host.lower()
        if port > 0:
            self._last_failed = (h, port)
        if h in self._good and (port == 0 or self._good[h] == port):
            del self._good[h]
            self._save()

    # -- persistence
    def _load(self) -> None:
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in (data.get("last_good") or {}).items():
                if isinstance(v, int) and 0 < v <= 65535:
                    self._good[k.lower()] = v
        except Exception as ex:
            log.warning("state file unreadable, ignoring: %s", ex)

    def _save(self) -> None:
        if not self.state_path:
            return
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"last_good": self._good}, f, indent=2)
            os.replace(tmp, self.state_path)
        except Exception as ex:
            log.warning("Saving state failed: %s", ex)

    async def resolve(self, host: str = "", port: int = 0) -> Optional["DockEndpointLike"]:
        """User IP/port -> else mDNS -> endpoint (host, port). Returns (host, port) tuple or None."""
        if host:
            return host, await self.pick_port(host, port)
        svc = await self.find_service()
        if svc is None:
            return None
        return svc.host, await self.pick_port(svc.host, 0)


DockEndpointLike = tuple
