"""Quit path: every entry point reaches request_quit, request_quit really quits, and shutdown is bounded and logged."""
import asyncio
import logging
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication

from deckoy import app as deckoy_app
from deckoy import shutdown
from deckoy.backend import Backend, NullBackend
from deckoy.model import Settings
from deckoy.proxy import advertise
from deckoy.ui.controller import Controller
from deckoy.ui.main_window import MainWindow
from deckoy.ui.tray import Tray


@pytest.fixture
def ctl(qtbot, tmp_path):
    c = Controller(Settings(), NullBackend(), str(tmp_path / "settings.json"), save_delay_ms=20)
    return c


def find_action(window, text):
    return next(a for a in window.findChildren(QAction) if a.text() == text)


# ---------------------------------------------------------------- all entry points reach request_quit
def test_tray_quit_file_quit_and_ctrl_q_call_request_quit(qtbot, ctl):
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    tray = Tray(w, ctl, QApplication.instance())
    calls = []
    w.request_quit = calls.append
    tray.request_quit = calls.append
    tray.quit_action.trigger()
    find_action(w, "Quit Deckoy").trigger()                   # File > Quit (Ctrl+Q is the same QAction)
    assert calls == ["tray", "File > Quit / Ctrl+Q"]
    assert find_action(w, "Quit Deckoy").shortcut().toString() == "Ctrl+Q"


def test_window_close_without_tray_quits_but_with_tray_only_hides(qtbot, ctl):
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    calls = []
    w.request_quit = calls.append
    w.show()
    w.tray_available = True
    w.close()
    assert calls == [] and not w.isVisible()                    # X hides to the tray
    w.show()
    w.tray_available = False
    w.close()
    assert calls == ["window close (no tray)"]                  # previously this left the process running


def test_quit_requested_falls_back_to_close_when_no_application_is_wired(qtbot, ctl):
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.show()
    w.quit_requested("x")
    assert w.quitting and not w.isVisible()


# ---------------------------------------------------------------- request_quit itself
class Rec:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append(name)


def test_request_quit_saves_hides_quits_and_arms_the_watchdog(monkeypatch, caplog):
    app, window, ctl = Rec(), Rec(), Rec()
    tray = Rec()
    tray.icon = Rec()
    started = []
    monkeypatch.setattr(shutdown, "start_exit_watchdog", lambda s: started.append(s) or "wd")
    rq = deckoy_app.make_request_quit(app, window, tray, ctl, logging.getLogger("deckoy"), watchdog_seconds=3.0)
    with caplog.at_level(logging.INFO, logger="deckoy"):
        rq("tray")
        rq("tray again")                                         # idempotent
    assert ctl.calls == ["save_now"] and window.calls == ["hide"] and tray.icon.calls == ["hide"]
    assert app.calls == ["quit"] and window.quitting is True
    assert started == [3.0] and rq.state["watchdog"] == "wd"
    assert [r.message for r in caplog.records if "quit requested" in r.message] == ["quit requested (tray)"]


# ---------------------------------------------------------------- shutdown helpers
def test_bounded_logs_ok_timeout_and_failure(caplog):
    async def ok():
        pass

    async def hang():
        await asyncio.sleep(3600)

    async def boom():
        raise RuntimeError("x")

    async def go():
        assert await shutdown.bounded("fine", ok(), 1.0) is True
        t0 = time.monotonic()
        assert await shutdown.bounded("stuck", hang(), 0.2) is False
        assert time.monotonic() - t0 < 1.0
        assert await shutdown.bounded("broken", boom(), 1.0) is False
    with caplog.at_level(logging.INFO, logger="deckoy.shutdown"):
        asyncio.run(go())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "shutdown: fine ok in" in text and "shutdown: stuck TIMED OUT after" in text and "shutdown: broken failed" in text


def test_watchdog_fires_and_can_be_cancelled():
    fired = []
    t = shutdown.start_exit_watchdog(0.05, lambda code, why: fired.append((code, why)))
    t.join(1)
    assert fired and fired[0][0] == 0 and "watchdog" in fired[0][1]
    fired.clear()
    t = shutdown.start_exit_watchdog(5.0, lambda code, why: fired.append(1))
    t.cancel()
    time.sleep(0.1)
    assert fired == []


# ---------------------------------------------------------------- bounded backend stop
def test_backend_stop_with_a_hanging_proxy(tmp_path, caplog):
    s = Settings()
    s.dock_host, s.dock_port = "127.0.0.1", 1
    s.proxy_bind = "127.0.0.1"
    s.proxy_deck_port, s.proxy_dock_port = 38211, 38212
    be = Backend(s, str(tmp_path))
    be.start()

    class HangingProxy:
        async def stop(self, fast=False):
            await asyncio.sleep(3600)

    real = be.proxy
    be.proxy = HangingProxy()
    t0 = time.monotonic()
    with caplog.at_level(logging.INFO, logger="deckoy"):
        be.stop()
    assert time.monotonic() - t0 < 3.4
    assert "shutdown: proxy TIMED OUT" in "\n".join(r.getMessage() for r in caplog.records)
    # the real proxy (replaced above) still holds its listening sockets: close them so the test leaves nothing behind
    if real is not None:
        for srv in (real.deck_server, real.dock_server):
            for sock_server in srv._servers:
                sock_server.close()


# ---------------------------------------------------------------- advertiser fast stop
def test_advertiser_fast_stop_sends_one_goodbye_round_and_closes():
    class FakeRegistry:
        def __init__(self):
            self.removed = []

        def async_remove(self, infos):
            self.removed.extend(i.name for i in infos)

    class FakeZc:
        def __init__(self):
            self.sent, self.registry = [], FakeRegistry()

        def generate_service_broadcast(self, info, ttl, broadcast_addresses):
            return ("bye", info.name, ttl)

        def async_send(self, out):
            self.sent.append(out)

    class FakeAzc:
        def __init__(self):
            self.zeroconf, self.closed, self.unregistered_all = FakeZc(), 0, 0

        async def async_unregister_all_services(self):
            self.unregistered_all += 1

        async def async_close(self):
            self.closed += 1

    async def go():
        adv = advertise.Advertiser("192.168.1.20", 5343, 20002, refresh_interval=0)
        azc = adv._azc = FakeAzc()
        adv._infos = adv.service_infos()
        await adv.stop(fast=True)
        assert [s[2] for s in azc.zeroconf.sent] == [0, 0] and {s[1] for s in azc.zeroconf.sent} == {i.name for i in adv._infos}
        assert azc.zeroconf.registry.removed and azc.closed == 1 and azc.unregistered_all == 0
        adv2 = advertise.Advertiser("192.168.1.20", 5343, 20002, refresh_interval=0)
        azc2 = adv2._azc = FakeAzc()
        adv2._infos = adv2.service_infos()
        await adv2.stop(fast=False)                              # the polite path (mode switch) is unchanged
        assert azc2.unregistered_all == 1 and azc2.closed == 1 and azc2.zeroconf.sent == []
    asyncio.run(go())


def test_backend_stop_overall_deadline_covers_a_step_that_ignores_cancellation(tmp_path, caplog):
    s = Settings()
    s.dock_host, s.dock_port = "127.0.0.1", 1
    s.proxy_bind = "127.0.0.1"
    s.proxy_deck_port, s.proxy_dock_port = 38213, 38214
    be = Backend(s, str(tmp_path))
    be.start()

    class Stubborn:
        async def stop(self, fast=False):
            while True:                  # swallows cancellation: wait_for itself can't get out
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    pass

    real = be.proxy
    be.proxy = Stubborn()
    t0 = time.monotonic()
    with caplog.at_level(logging.INFO, logger="deckoy"):
        be.stop(deadline=1.0)
    assert time.monotonic() - t0 < 2.5
    assert "overall" in "\n".join(r.getMessage() for r in caplog.records)
    if real is not None:
        for srv in (real.deck_server, real.dock_server):
            for sock_server in srv._servers:
                sock_server.close()
