import asyncio
import os
import sys
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from deckoy import startup
from deckoy.backend import Backend
from deckoy.model import Settings
from tools.mock_dock import MockDock


class MockThread:
    """Runs MockDock on its own loop so the Backend's loop is independent."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.t = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.t.start()
        self.mock = self.call(MockDock(keepalive_interval=0.2).start())

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(5)

    def close(self):
        self.call(self.mock.stop())
        self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture
def mock():
    mt = MockThread()
    yield mt
    mt.close()


# ---- start with Windows
def test_startup_shortcut_create_and_remove(tmp_path):
    lnk = str(tmp_path / "Deckoy.lnk")
    startup.set_enabled(True, lnk)
    assert os.path.exists(lnk)
    startup.set_enabled(False, lnk)
    assert not os.path.exists(lnk)
    startup.set_enabled(False, lnk)  # idempotent


def test_launch_target_from_source():
    exe, args = startup.launch_target()
    assert exe.lower().endswith(("python.exe", "pythonw.exe")) and "-m deckoy" in args


# ---- proxy mode through the real Backend (Qt signals, mode switch)
def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_backend_proxy_end_to_end_and_apply_settings(qtbot, tmp_path, mock):
    from deckoy.protocol.client import blank_jpeg
    from tools.fake_elgato import FakeElgatoApp
    from tools.mock_dock import MockDockService
    from deckoy.protocol import frame as fr

    async def start_svc():
        return await MockDockService(keepalive_interval=0.2).start()
    svc = asyncio.run_coroutine_threadsafe(start_svc(), mock.loop).result(5)
    s = Settings()
    s.dock_host, s.dock_port = "127.0.0.1", mock.mock.port
    s.upstream_dock_port, s.proxy_bind = svc.port, "127.0.0.1"
    s.proxy_deck_port, s.proxy_dock_port = _free_port(), _free_port()
    s.brightness = 33
    be = Backend(s, str(tmp_path))
    statuses = []
    be.proxy_status.connect(statuses.append)
    try:
        with qtbot.waitSignal(be.state_changed, timeout=10000, check_params_cb=lambda st: st == "Connected"):
            be.start()
        assert be.proxy is not None
        qtbot.waitUntil(lambda: mock.mock.brightness == 33, timeout=5000)

        async def drive():
            app = await FakeElgatoApp("127.0.0.1", s.proxy_deck_port).connect()
            ack = await app.send_image(5, blank_jpeg(), timeout=3)
            await app.request(fr.CH_SET_FEATURE, bytes([3, 8, 77]) + bytes(29))
            await app.close()
            return ack.type
        assert asyncio.run(drive()) == fr.ACK
        qtbot.waitUntil(lambda: 5 in mock.mock.images and mock.mock.brightness == 77, timeout=5000)
        qtbot.waitUntil(lambda: bool(statuses) and statuses[-1]["upstream_connected"], timeout=5000)

        # apply_settings restarts the proxy on the new ports; the old ones close
        old_port = s.proxy_deck_port
        s.proxy_deck_port = _free_port()
        old_proxy = be.proxy
        be.apply_settings()
        qtbot.waitUntil(lambda: be.proxy is not None and be.proxy is not old_proxy, timeout=10000)
        qtbot.waitUntil(lambda: be.proxy.deck_server.port == s.proxy_deck_port, timeout=10000)
        import socket
        with socket.socket() as sock:
            sock.settimeout(0.5)
            assert sock.connect_ex(("127.0.0.1", old_port)) != 0
    finally:
        be.stop()
        asyncio.run_coroutine_threadsafe(svc.stop(), mock.loop).result(5)
