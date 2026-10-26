"""ElgatoProxy: the only client of the real dock, standing in for the dock toward the Elgato Stream Deck app."""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional

from ..protocol import frame as fr
from ..protocol.client import ConnectionState, DockClient, DockEndpoint, DockInfo, blank_jpeg
from . import advertise
from ..shutdown import bounded
from .servers import DeckServer, DockServer, rewrite_descriptor_port
from .state import ProxyState
from .stats import ProxyStats
from .upstream import CoalescingSender, UpstreamDockService

log = logging.getLogger("deckoy.proxy")

REPLY_WAIT_SECONDS = 3.0
LEARN_ATTEMPTS = 3          # tries at learning the dock's mDNS records before falling back to generic ones
LEARN_BACKOFF = 1.0         # seconds; multiplied by the attempt number
HANDSHAKE_SERIAL_WAIT = 3.0 # how long the generic fallback waits for the upstream handshake serial


BUSY_BYTE = 5          # in the 03 8f reply: 0 = free, 1 = a client is connected (seen live; "device is already in use")


def present_as_free(reply: bytes) -> bytes:
    """Byte 5 of the dock's `03 8f` reply is 1 while any client is connected to the real dock. Deckoy is always connected to it,
    so the live reply says "in use" and the Elgato app rejects the dock (`reject dock service ... due to -12`, "device is already
    in use"). The reply in the Elgato capture (nobody connected) has 0 there. The Elgato app is the proxy's only client, so the
    proxy reports itself as free."""
    b = bytearray(reply)
    if len(b) > BUSY_BYTE:
        b[BUSY_BYTE] = 0
    return bytes(b)


def lan_ip_for(host: str = "") -> str:
    """This PC's address on the route to `host` (UDP connect sends no packets). Falls back to loopback."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host or "10.255.255.255", 9))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
Provider = Callable[[], Awaitable[Optional[DockEndpoint]]]


@dataclass
class ProxyConfig:
    dock_host: str = ""                 # real dock; blank = discover
    dock_port: int = 0                  # real deck-service port; 0 = auto
    upstream_dock_port: int = 5343      # real dock service port
    bind: str = ""                      # the address advertised via mDNS; blank = this PC's LAN address toward the dock
    listen_all: bool = True             # listen on all local addresses (only connections from this PC are accepted) (needed for mDNS)
    deck_port: int = 20002              # local deck-service port
    dock_service_port: int = 5343       # local dock-service port
    forward_unknown: bool = False
    naming: str = advertise.NAMING_DISTINCT
    advertise: bool = True
    brightness: int = 63
    sleep_seconds: int = 3600
    keepalive_interval: float = 4.0
    state_dir: Optional[str] = None
    # When the Elgato app aborts its own (healthy) session it never reconnects by itself. Withdrawing and re-announcing the
    # deck's mDNS record (optionally on a new port) is what we try to make it come back.
    reannounce: str = "deck"            # off | deck | both
    reannounce_new_port: bool = True
    reannounce_delay: float = 1.5       # seconds between the goodbye and the new announcement
    established_after: float = 3.0      # an app connection that lasted longer counts as a real session (not priming)


class ElgatoProxy:
    def __init__(self, cfg: ProxyConfig, deck_endpoint: Optional[Provider] = None,
                 dock_endpoint: Optional[Provider] = None, locator=None):
        self.cfg = cfg
        self.locator = locator
        self.stats = ProxyStats(os.path.join(cfg.state_dir, "proxy_stats.json") if cfg.state_dir else None)
        self.state = ProxyState(os.path.join(cfg.state_dir, "proxy_state.json") if cfg.state_dir else None)
        self.client = DockClient()
        self.client.brightness, self.client.sleep_seconds = cfg.brightness, cfg.sleep_seconds
        self.client.probe_image = blank_jpeg()
        self.sender = CoalescingSender(self.client)
        self.bind = cfg.bind or lan_ip_for(cfg.dock_host.strip())
        listen = "0.0.0.0" if cfg.listen_all else self.bind
        self.deck_server = DeckServer(listen, cfg.deck_port, self, cfg.keepalive_interval)
        self.dock_server = DockServer(listen, cfg.dock_service_port, self, cfg.keepalive_interval)
        self._deck_endpoint = deck_endpoint or self._default_deck_endpoint
        self._dock_endpoint = dock_endpoint or self._default_dock_endpoint
        self.upstream_dock = UpstreamDockService(self._dock_endpoint, self._on_dock_replies)
        self.advertiser: Optional[advertise.Advertiser] = None
        self.keys = [False] * fr.INPUT_KEYS
        self.stats.established_after = cfg.established_after
        self._reannounce_task: Optional[asyncio.Task] = None
        self._info_event = asyncio.Event()
        self._tasks: set = set()
        self._status_task: Optional[asyncio.Task] = None
        self._last_status = None
        # UI hooks (called on the loop thread)
        self.on_status: Optional[Callable[[dict], None]] = None
        self.on_warning: Optional[Callable[[str], None]] = None
        self.on_info: Optional[Callable[[str], None]] = None
        self.on_log: Optional[Callable[[str], None]] = None
        self.client.on_log = self._client_log
        self.client.on_key = self._on_key
        self.client.on_state = self._on_client_state
        self.client.on_connected = self._on_connected

    # ------------------------------------------------------------------ properties used by the servers
    @property
    def forward_unknown(self) -> bool:
        return self.cfg.forward_unknown

    def warn(self, msg: str) -> None:
        if self.on_warning:
            self.on_warning(msg)

    # ------------------------------------------------------------------ endpoints
    async def _default_deck_endpoint(self) -> Optional[DockEndpoint]:
        r = await self.locator.resolve(self.cfg.dock_host.strip(), self.cfg.dock_port)
        return DockEndpoint(*r) if r else None

    async def _default_dock_endpoint(self) -> Optional[DockEndpoint]:
        host = self.cfg.dock_host.strip()
        if not host and self.locator is not None and self.locator.cached is not None:
            host = self.locator.cached.host
        return DockEndpoint(host, self.cfg.upstream_dock_port) if host else None

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await self.deck_server.start()
        try:
            await self.dock_server.start()
        except OSError:
            await self.deck_server.stop()
            raise
        self.sender.start()
        self.upstream_dock.start()
        self.client.start(self._deck_endpoint)
        if self.cfg.advertise:
            self._spawn(self._advertise())
        self._status_task = asyncio.ensure_future(self._status_loop())

    async def stop(self, fast: bool = False) -> None:
        """Stop everything. Each step is time-boxed and logged. fast=True (app quit) sends the mDNS goodbyes once only."""
        if self._status_task:
            self._status_task.cancel()
        if self._reannounce_task is not None:
            self._reannounce_task.cancel()
        for t in list(self._tasks):
            t.cancel()
        if self.advertiser:
            await bounded("advertiser", self.advertiser.stop(fast=fast), 1.0)
        await bounded("proxy sender", self.sender.stop(), 0.5)
        await bounded("upstream dock service", self.upstream_dock.stop(), 0.5)
        await bounded("dock client", self.client.aclose(), 0.5)
        await bounded("deck server", self.deck_server.stop(), 0.5)
        await bounded("dock server", self.dock_server.stop(), 0.5)
        self.stats._save()

    def _spawn(self, coro) -> None:
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _advertise(self) -> None:
        st = self.state
        if not st.dock_txt and self.locator is not None:
            for attempt in range(LEARN_ATTEMPTS):
                await self._learn_records()
                if st.dock_txt or st.deck_txt:
                    break
                if attempt < LEARN_ATTEMPTS - 1:
                    await asyncio.sleep(LEARN_BACKOFF * (attempt + 1))      # cancelled by stop()
        dock_txt, deck_txt = dict(st.dock_txt), dict(st.deck_txt)
        dock_name, deck_name = st.dock_name, st.deck_name
        if not dock_txt or not deck_txt:
            log.warning("using GENERIC mDNS records because the real dock's could not be learned: set the dock's IP and make "
                        "sure mDNS works; the Elgato app may reject generic records")
        if not deck_txt:
            # the handshake serial (if the dock answers) is the real deck's: use it for the generic deck record
            try:
                await asyncio.wait_for(self._info_event.wait(), HANDSHAKE_SERIAL_WAIT)
            except asyncio.TimeoutError:
                pass
            serial = self.client.info.serial if self.client.info else ""
            if serial:
                deck_txt = {**advertise.DEFAULT_DECK_TXT, "sn": serial}
                deck_name = serial
        self.advertiser = advertise.Advertiser(
            self.bind, self.dock_server.port, self.deck_server.port, dock_txt or None, deck_txt or None,
            dock_name, deck_name, self.cfg.naming)
        ok = await self.advertiser.start()
        if self.on_info:
            self.on_info("Advertising as a Network Dock on this PC" if ok else "mDNS advertisement failed (see log)")

    async def _learn_records(self) -> None:
        """Copy the real dock's mDNS records (so the proxy's TXT is identical). Our own records are excluded."""
        from ..protocol.discovery import discover_all, pick, pick_dock
        try:
            cands = [c for c in await discover_all(4.0) if c.host != self.bind
                     and "(Deckoy)" not in c.name and "(DeckPad)" not in c.name]
        except Exception as ex:
            log.info("could not learn the dock's mDNS records: %r", ex)
            return
        dock, deck = pick_dock(cands), pick(cands)
        if dock:
            self.state.dock_txt, self.state.dock_name = dict(dock.txt), dock.name
        if deck:
            self.state.deck_txt, self.state.deck_name = dict(deck.txt), deck.name
        if dock or deck:
            self.state.save()

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            snap = self.stats.snapshot()
            key = {k: v for k, v in snap.items() if k != "downtime_s"}
            if key != self._last_status:
                self._last_status = key
                if self.on_status:
                    self.on_status(snap)

    # ------------------------------------------------------------------ upstream events
    def _client_log(self, msg: str) -> None:
        log.info("upstream: %s", msg)
        if self.on_log:
            self.on_log(msg)

    def _on_connected(self, info: DockInfo) -> None:
        self.stats.upstream_up()
        st = self.state
        for fid, raw in ((0x05, info.reply_05), (0x06, info.reply_06), (0x0B, info.reply_0b)):
            if raw:
                st.deck_replies[fid] = bytes(raw).ljust(32, b"\x00")[:32]
        st.save()
        self._info_event.set()
        if self.on_info:
            ep = self.client.endpoint
            self.on_info(f"{info.serial}  fw {info.firmware}  {ep.host}:{ep.port}" if ep else info.serial)

    def _on_client_state(self, s: ConnectionState) -> None:
        if s == ConnectionState.DISCONNECTED and self.stats.upstream_connected:
            self.stats.upstream_down()
            if any(self.keys):    # a key that was down when the link dropped is released for the app
                self.keys = [False] * fr.INPUT_KEYS
                self._spawn(self.deck_server.send_keys(list(self.keys)))

    def _on_dock_replies(self, replies: Dict[str, bytes]) -> None:
        self.state.dock_replies.update(replies)
        self.state.save()

    def _on_key(self, key: int, down: bool) -> None:
        self.keys[key] = down
        self._spawn(self.deck_server.send_keys(list(self.keys)))

    # ------------------------------------------------------------------ the app dropped its own session
    def on_app_session_closed(self, duration: float, label: str, by_us: bool) -> None:
        """Called by the deck server when an established Elgato app session ends (not the app's short priming connections)."""
        if duration < self.cfg.established_after:
            return
        self.stats.record_app_drop(duration, by_us)
        log.warning("ELGATO APP SESSION ENDED at %s after %.1f s (%s)%s; dock link %s", time.strftime("%Y-%m-%d %H:%M:%S"),
                    duration, label, " [dropped by Deckoy for a test]" if by_us else "",
                    "up" if self.stats.upstream_connected else "DOWN")
        if self.cfg.reannounce == "off":
            return
        if self._reannounce_task is not None and not self._reannounce_task.done():
            log.info("re-announce already in progress")
            return
        self._reannounce_task = asyncio.ensure_future(self._reannounce())

    async def _next_deck_port(self) -> Optional[int]:
        base = max(self.cfg.deck_port, 1024)
        cur = self.deck_server.port
        for cand in range(cur + 1, cur + 30):
            p = cand if cand <= 65000 else base + (cand % 50)     # wrap instead of running off the end
            try:
                return await self.deck_server.rebind(p)
            except OSError:
                continue
        try:
            return await self.deck_server.rebind(0)
        except OSError:
            return None

    async def _reannounce(self) -> None:
        cfg = self.cfg
        try:
            new_port = None
            if cfg.reannounce_new_port and cfg.reannounce in ("deck", "both"):
                new_port = await self._next_deck_port()
            if self.advertiser is None:
                log.warning("cannot re-announce: the proxy is not advertising via mDNS (see the earlier log lines)")
            else:
                ok = await self.advertiser.reannounce(cfg.reannounce, new_port, cfg.reannounce_delay)
                log.info("re-announce %s (deck service on port %s)", "done" if ok else "FAILED", self.deck_server.port)
            self.stats.note_reannounce()
            if self.on_info:
                self.on_info(f"Elgato app dropped its session; re-announced the deck on port {self.deck_server.port}")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("re-announce failed")

    async def drop_app_session(self) -> bool:
        """Test hook: abort the app's newest deck connection (as if its session had died)."""
        return await self.deck_server.drop_active()

    # ------------------------------------------------------------------ core API (called by the servers)
    async def get_deck_reply(self, fid: int) -> bytes:
        if fid not in self.state.deck_replies:
            try:
                await asyncio.wait_for(self._info_event.wait(), REPLY_WAIT_SECONDS)
            except asyncio.TimeoutError:
                pass
        reply = self.state.deck_replies.get(fid)
        if reply is None:
            log.warning("no cached reply for get-feature %02x; answering with zeros", fid)
            reply = bytes([fid]).ljust(32, b"\x00")
        return reply

    async def get_dock_reply(self, ident: bytes) -> Optional[bytes]:
        key = ident.hex()
        reply = self.upstream_dock.replies.get(key) or self.state.dock_replies.get(key)
        if reply is None:
            try:
                await asyncio.wait_for(self.upstream_dock.ready.wait(), REPLY_WAIT_SECONDS)
            except asyncio.TimeoutError:
                pass
            reply = self.upstream_dock.replies.get(key) or self.state.dock_replies.get(key)
        if reply is not None and ident == b"\x03\x1c":
            reply = rewrite_descriptor_port(reply, self.deck_server.port)
        if reply is not None and ident == b"\x03\x8f":
            reply = present_as_free(reply)
        return reply

    def set_brightness(self, pct: int) -> None:
        self.cfg.brightness = pct
        self._spawn(self._safely(self.client.set_brightness(pct)))

    def set_sleep(self, seconds: int) -> None:
        self.cfg.sleep_seconds = seconds
        self._spawn(self._safely(self.client.set_sleep(seconds)))

    def set_feature(self, payload: bytes) -> None:
        self._spawn(self._safely(self.client.send_set_feature(payload)))

    def forward_set_feature(self, payload: bytes) -> None:
        self._spawn(self._safely(self.client.raw_set_feature(payload)))

    def submit_image(self, key: int, jpeg: bytes) -> None:
        self.sender.submit(key, jpeg)

    async def forward_dock_request(self, payload: bytes) -> Optional[bytes]:
        reply = await self.upstream_dock.request(payload)
        if reply is not None:
            self.state.dock_replies[payload[:2].hex()] = reply
        return reply

    async def _safely(self, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            log.info("upstream command not delivered now (%r); settings are replayed on reconnect", ex)
