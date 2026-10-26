"""Background thread running the asyncio loop (the Elgato proxy and its dock link); talks to Qt through signals."""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, Signal

from .model import Settings
from .protocol.client import ConnectionState
from .protocol.discovery import DockLocator, discover
from .shutdown import bounded

log = logging.getLogger("deckoy.backend")


class NullBackend(QObject):
    """Does nothing; used by UI tests."""
    state_changed = Signal(str)
    info_changed = Signal(str)
    hint = Signal(str)
    proxy_status = Signal(dict)       # ProxyStats.snapshot()
    proxy_warning = Signal(str)

    def __init__(self, settings: Optional[Settings] = None, parent=None):
        super().__init__(parent)
        self.calls = []

    def start(self): self.calls.append(("start",))
    def stop(self): self.calls.append(("stop",))
    def apply_settings(self): self.calls.append(("apply_settings",))
    def reconnect(self): self.calls.append(("reconnect",))
    def proxy_drop_app(self): self.calls.append(("proxy_drop_app",))


class Backend(NullBackend):
    def __init__(self, settings: Settings, app_dir: str, parent=None):
        super().__init__(settings, parent)
        self.settings = settings
        self.app_dir = app_dir
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="dock-loop", daemon=True)
        self.locator: Optional[DockLocator] = None
        self.proxy = None
        self._ready = threading.Event()

    # ---- lifecycle
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(lambda: asyncio.ensure_future(self._setup()))
        self.loop.run_forever()

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(5)

    def stop(self, deadline: float = 2.5) -> None:
        """Stop the dock side for good (app quit). Time-boxed: never blocks the GUI thread for more than ~deadline + 0.5 s."""
        t0 = time.monotonic()
        if self._thread.is_alive():
            fut = asyncio.run_coroutine_threadsafe(self._stop_proxy(fast=True), self.loop)
            try:
                fut.result(deadline)
            except concurrent.futures.TimeoutError:
                log.warning("shutdown: backend TIMED OUT after %.1f s overall; giving up on the remaining steps", deadline)
                fut.cancel()
            except Exception as ex:
                log.warning("shutdown: backend failed: %r", ex)
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(0.5)
        log.info("shutdown: backend stopped in %d ms", (time.monotonic() - t0) * 1000)

    async def _setup(self) -> None:
        await self._start_proxy()
        self._ready.set()

    async def _stop_proxy(self, fast: bool = False) -> None:
        """Tear the proxy down, step by step, each bounded and logged. fast=True is the app-quit path."""
        loc, prox = self.locator, self.proxy
        self.locator = self.proxy = None
        if loc:
            loc.stop()
        if prox:
            await bounded("proxy", prox.stop(fast=fast), 2.0)

    # ---- proxy
    async def _start_proxy(self) -> None:
        from .proxy.proxy import ElgatoProxy, ProxyConfig, lan_ip_for
        s = self.settings
        cfg = ProxyConfig(dock_host=s.dock_host, dock_port=s.dock_port, upstream_dock_port=s.upstream_dock_port, bind=s.proxy_bind,
                          listen_all=s.proxy_listen_all,
                          deck_port=s.proxy_deck_port, dock_service_port=s.proxy_dock_port,
                          forward_unknown=s.proxy_forward_unknown, naming=s.proxy_naming, brightness=s.brightness,
                          sleep_seconds=s.sleep_seconds, state_dir=self.app_dir, reannounce=s.proxy_reannounce,
                          reannounce_new_port=s.proxy_reannounce_new_port,
                          reannounce_delay=s.proxy_reannounce_delay_ms / 1000.0)
        bind = cfg.bind or lan_ip_for(cfg.dock_host.strip())
        # never mistake our own advertisement for the dock
        loc = DockLocator(discover_fn=lambda: discover(exclude_hosts=[bind, "127.0.0.1"]),
                          state_path=os.path.join(self.app_dir, "state.json"))
        if not cfg.dock_host.strip():
            loc.start_background()
        self.locator = loc
        self.proxy = p = ElgatoProxy(cfg, locator=loc)
        p.on_status = self.proxy_status.emit
        p.on_warning = self.proxy_warning.emit
        p.on_info = self.info_changed.emit
        orig = p.client.on_state
        p.client.on_state = lambda st: (orig(st), self.state_changed.emit(st.value))
        orig_log = p.client.on_log
        p.client.on_log = lambda m: (orig_log(m), self._on_log(m))
        try:
            await p.start()
        except OSError as ex:
            log.error("proxy could not start: %s", ex)
            self.hint.emit(f"Proxy could not start ({ex}). Is another program using port {cfg.deck_port} or "
                           f"{cfg.dock_service_port}?")
            self.proxy = None
            await p.stop()
            return
        self.proxy_status.emit(p.stats.snapshot())

    # ---- callbacks (loop thread)
    def _on_log(self, msg: str) -> None:
        log.info(msg)
        if "Elgato" in msg:
            self.hint.emit("The dock isn't answering. Close the Elgato Stream Deck app and disable the Touch Portal "
                           "SDDockBridge plugin: only one program can drive the dock.")
        elif "Session probe failed" in msg:
            self.hint.emit("Dock session not responding, retrying...")

    # ---- thread-safe API for the UI
    def _submit(self, coro) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def proxy_drop_app(self) -> None:
        """Test: abort the Elgato app's deck session and let the re-announce logic bring it back."""
        if self.proxy:
            self._submit(self.proxy.drop_app_session())

    def apply_settings(self) -> None:
        async def apply():
            await self._stop_proxy()      # proxy settings (ports, bind, policy) only take effect on a fresh start
            self.state_changed.emit(ConnectionState.DISCONNECTED.value)
            self.info_changed.emit("")
            self.hint.emit("")
            await self._start_proxy()
        self._submit(apply())

    def reconnect(self) -> None:
        def go():
            if self.proxy:
                self.proxy.client.start(self.proxy._deck_endpoint)
        self.loop.call_soon_threadsafe(go)
