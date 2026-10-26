"""mDNS advertisement of the proxy as a Stream Deck Network Dock (`_elg._tcp.local.`), TXT copied from the real dock."""
from __future__ import annotations

import logging
import re
import socket
from typing import Dict, List, Optional

log = logging.getLogger("deckoy.proxy")

SERVICE_TYPE = "_elg._tcp.local."


def proxy_hostname() -> str:
    """The SRV target host. It must resolve FAST on this PC: the Elgato app looks the name up before every connect and its
    connect then times out if the lookup used up its time budget. An invented name like Deckoy-proxy.local took 3-7 s to
    resolve through mDNS; this PC's own computer name resolves locally in milliseconds (and Bonjour already publishes it with
    the same address, so there is no conflicting record)."""
    name = re.sub(r"[^A-Za-z0-9-]", "-", socket.gethostname()).strip("-") or "Deckoy-proxy"
    return f"{name}.local."


# Generic fallback records (same keys and model strings as a real dock, placeholder ids). Used only when the real dock's
# records could not be learned; the Elgato app may reject them, so prefer letting Deckoy copy the real ones.
DEFAULT_DOCK_NAME = "Stream Deck Network Dock"
DEFAULT_DOCK_TXT = {"dt": "215", "mf": "Elgato", "md": "Stream Deck Network Dock 20GBW9901", "pv": "1.0",
                    "id": "00:00:00:00:00:00", "sn": "DECKOY000000", "fw": "1.01.014", "hwrev": "0200"}
DEFAULT_DECK_NAME = "Stream Deck XL"
DEFAULT_DECK_TXT = {"dt": "213", "mf": "Elgato", "md": "Stream Deck XL 20GAT9901", "pv": "1.0", "sn": "DECKOY000001",
                    "vid": "0x0FD9", "pid": "0x006C", "fw": "1.01.000", "prt": "2"}

NAMING_DISTINCT = "distinct"   # instance names get a " (Deckoy)" suffix, TXT identical
NAMING_SAME = "same"           # identical instance names (the real dock's records are defended by the dock itself)


def instance_names(dock_name: str, deck_name: str, naming: str) -> List[str]:
    if naming == NAMING_SAME:
        return [dock_name, deck_name]
    return [f"{dock_name} (Deckoy)", f"{deck_name} (Deckoy)"]


class Advertiser:
    def __init__(self, bind_ip: str, dock_port: int, deck_port: int, dock_txt: Optional[Dict[str, str]] = None,
                 deck_txt: Optional[Dict[str, str]] = None, dock_name: str = "", deck_name: str = "",
                 naming: str = NAMING_DISTINCT, hostname: str = "", refresh_interval: float = 20.0):
        self.bind_ip, self.dock_port, self.deck_port = bind_ip, dock_port, deck_port
        # The Elgato app dropped Deckoy's mDNS record ~60 s after adding it, every time, while the real dock's stayed (the
        # dock re-answers every refresh query by multicast and unicast). Unsolicited multicast announcements every
        # `refresh_interval` seconds keep the record in the Windows mDNS cache without relying on query answers.
        self.refresh_interval = refresh_interval
        self._refresh_task = None
        self.hostname = hostname or proxy_hostname()
        self.dock_txt = dict(dock_txt or DEFAULT_DOCK_TXT)
        self.deck_txt = dict(deck_txt or DEFAULT_DECK_TXT)
        self.dock_name = dock_name or DEFAULT_DOCK_NAME
        self.deck_name = deck_name or DEFAULT_DECK_NAME
        self.naming = naming
        self.names = instance_names(self.dock_name, self.deck_name, naming)
        self._azc = None
        self._infos: list = []

    def service_infos(self):
        from zeroconf import ServiceInfo
        addr = socket.inet_aton(self.bind_ip)
        out = []
        for name, port, txt in ((self.names[0], self.dock_port, self.dock_txt), (self.names[1], self.deck_port, self.deck_txt)):
            out.append(ServiceInfo(SERVICE_TYPE, f"{name}.{SERVICE_TYPE}", port=port, addresses=[addr],
                                   properties={k: v.encode() for k, v in txt.items()}, server=self.hostname))
        return out

    async def start(self) -> bool:
        """Publish both services. Returns False (and logs) if mDNS publishing is not possible; the proxy keeps running."""
        if self.bind_ip.startswith("127."):
            log.warning("not advertising: the proxy listens on loopback only, which the Elgato app can't discover via mDNS "
                        "(set a LAN address in Settings, or leave 'Listen on' blank)")
            return False
        try:
            from zeroconf import IPVersion
            from zeroconf.asyncio import AsyncZeroconf
            # announce only on the adapter that carries the bind address (no VPN / virtual adapters)
            self._azc = AsyncZeroconf(interfaces=[self.bind_ip], ip_version=IPVersion.V4Only)
            self._infos = self.service_infos()
            for info in self._infos:
                # cooperating_responders: skip the uniqueness probe; the real dock advertises the same names
                await self._azc.async_register_service(info, cooperating_responders=True)
            log.info("advertising %s on %s (dock %s, deck %s)", self.names, self.bind_ip, self.dock_port, self.deck_port)
            import asyncio
            if self.refresh_interval > 0:
                self._refresh_task = asyncio.ensure_future(self._refresh_loop())
            return True
        except Exception as ex:
            log.warning("mDNS advertisement failed: %r", ex)
            await self.stop()
            return False

    def _info_for(self, which: str, port: int):
        from zeroconf import ServiceInfo
        name, txt = (self.names[0], self.dock_txt) if which == "dock" else (self.names[1], self.deck_txt)
        return ServiceInfo(SERVICE_TYPE, f"{name}.{SERVICE_TYPE}", port=port, addresses=[socket.inet_aton(self.bind_ip)],
                           properties={k: v.encode() for k, v in txt.items()}, server=self.hostname)

    async def reannounce(self, which: str = "deck", deck_port: Optional[int] = None, delay: float = 1.5) -> bool:
        """Withdraw a service (mDNS goodbye, TTL 0), wait, then announce it again, optionally on a new port.

        This is what makes the Elgato app notice that the "deck" is available again after it aborted its own session.
        which: "deck" | "dock" | "both".
        """
        import asyncio
        azc = self._azc
        if azc is None:
            return False
        targets = ["dock", "deck"] if which == "both" else [which]
        old = {t: (self._infos[0] if t == "dock" else self._infos[1]) for t in targets}
        try:
            for t in targets:
                await azc.async_unregister_service(old[t])      # sends the goodbye packets
            await asyncio.sleep(delay)
            if deck_port:
                self.deck_port = deck_port
            for t in targets:
                info = self._info_for(t, self.dock_port if t == "dock" else self.deck_port)
                await azc.async_register_service(info, cooperating_responders=True)
                self._infos[0 if t == "dock" else 1] = info
            log.info("re-announced %s (deck port %s)", targets, self.deck_port)
            return True
        except Exception as ex:
            log.warning("re-announce failed: %r", ex)
            return False

    async def refresh_once(self) -> int:
        """Multicast an unsolicited announcement of every registered record set. Returns how many services were sent."""
        azc = self._azc
        if azc is None:
            return 0
        n = 0
        for info in list(self._infos):
            try:
                zc = azc.zeroconf
                zc.async_send(zc.generate_service_broadcast(info, None, True))
                n += 1
            except Exception as ex:   # API differences between zeroconf versions: fall back to a TXT re-publish
                log.info("refresh via broadcast failed (%r); trying update_service", ex)
                try:
                    await azc.async_update_service(info)
                    n += 1
                except Exception as ex2:
                    log.warning("mDNS refresh failed: %r", ex2)
        return n

    async def _refresh_loop(self) -> None:
        import asyncio
        try:
            while True:
                await asyncio.sleep(self.refresh_interval)
                await self.refresh_once()
        except asyncio.CancelledError:
            pass

    async def stop(self, fast: bool = False) -> None:
        """Withdraw the services and close mDNS. fast=True (app quit): one goodbye round (TTL 0), no repeats, no waiting."""
        t, self._refresh_task = self._refresh_task, None
        if t is not None:
            t.cancel()
        azc, self._azc = self._azc, None
        if azc is None:
            return
        try:
            if fast:
                zc = azc.zeroconf
                for info in list(self._infos):
                    zc.async_send(zc.generate_service_broadcast(info, 0, True))     # goodbye: TTL 0
                try:
                    zc.registry.async_remove(list(self._infos))    # so close() does not repeat the goodbyes
                except Exception:
                    pass
            else:
                await azc.async_unregister_all_services()
            await azc.async_close()
        except Exception as ex:
            log.info("closing mDNS: %r", ex)
