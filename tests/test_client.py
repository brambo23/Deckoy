import asyncio
import json

import pytest

from deckoy.protocol import frame as fr
from deckoy.protocol.client import (ConnectionState, DockClient, DockEndpoint, ProbeFailed, blank_jpeg)
from deckoy.protocol.discovery import Candidate, DeckService, DockLocator, pick
from tools.mock_dock import MockDock

from .conftest import FIXTURES


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


async def until(pred, timeout=5.0):
    t = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > t:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def fast(c: DockClient) -> DockClient:
    c.prime_delay = 0.01
    c.probe_retry_delay = 0.05
    c.backoff = (0.05, 0.05, 0.1, 0.1)
    return c


def test_handshake_images_brightness():
    async def go():
        mock = await MockDock().start()
        c = DockClient()
        c.brightness, c.sleep_seconds = 40, 3600
        info = await c.connect("127.0.0.1", mock.port)
        assert (info.firmware, info.serial) == ("1.01.000", "TESTDECK0001")
        assert mock.brightness == 40 and mock.sleep_seconds == 3600
        jpg = (FIXTURES / "key14_14.jpg").read_bytes()  # multi-chunk image
        await c.set_key_image(14, jpg)
        assert mock.images[14] == jpg
        await c.set_brightness(10)
        assert mock.brightness == 10
        assert not mock.violations
        await c.aclose()
        await mock.stop()
    run(go())


def test_key_events_and_keepalive():
    async def go():
        mock = await MockDock(keepalive_interval=0.05).start()
        c = DockClient()
        events = []
        c.on_key = lambda k, d: events.append((k, d))
        await c.connect("127.0.0.1", mock.port)
        await mock.inject_key(3, True)
        await mock.inject_key(3, False)
        await until(lambda: len(events) == 2)
        assert events == [(3, True), (3, False)]
        await until(lambda: mock.keepalive_replies >= 2)
        await c.aclose()
        await mock.stop()
    run(go())


def test_request_timeout_drops_connection():
    async def go():
        mock = await MockDock(silent_connections=1).start()
        c = DockClient()
        c.request_timeout = 0.2
        await c.connect("127.0.0.1", mock.port)
        with pytest.raises(asyncio.TimeoutError):
            await c.set_key_image(0, blank_jpeg())
        await until(lambda: c.state == ConnectionState.DISCONNECTED)
        await mock.stop()
    run(go())


def test_deaf_first_session_recovers_via_probe_and_reprime():
    async def go():
        # connection 1 = priming, 2 = real (deaf), 3 = priming again, 4 = real (works)
        mock = await MockDock(silent_connections=2).start()
        c = fast(DockClient())
        c.probe_image = blank_jpeg()
        c.probe_timeout = 0.3
        logs, connected = [], asyncio.Event()
        c.on_log = logs.append
        c.on_connected = lambda info: connected.set()
        await c.set_key_image(5, blank_jpeg((255, 0, 0)))  # cached while offline
        c.start(lambda: _ep(mock.port))
        await asyncio.wait_for(connected.wait(), 10)
        assert c.probe_failures == 1
        assert mock.connections_accepted == 4
        assert 5 in mock.images  # re-pushed after connect
        assert any("probe failed" in m for m in logs)
        await c.aclose()
        await mock.stop()
    run(go())


async def _ep(port):
    return DockEndpoint("127.0.0.1", port)


def test_reconnect_after_drop_repushes_images():
    async def go():
        mock = await MockDock().start()
        c = fast(DockClient())
        n = 0
        ev = asyncio.Event()

        def on_conn(info):
            nonlocal n
            n += 1
            ev.set()
        c.on_connected = on_conn
        c.start(lambda: _ep(mock.port))
        await asyncio.wait_for(ev.wait(), 5)
        await c.set_key_image(7, blank_jpeg((0, 255, 0)))
        mock.images.clear()
        ev.clear()
        mock.drop_clients()
        await asyncio.wait_for(ev.wait(), 10)
        assert n == 2 and 7 in mock.images
        await c.aclose()
        await mock.stop()
    run(go())


def test_unreachable_dock_keeps_retrying_and_reports_failure():
    async def go():
        c = fast(DockClient())
        c.connect_timeout = 0.3
        results = []
        c.on_endpoint_result = lambda ep, ok: results.append(ok)
        c.start(lambda: _ep(1))  # nothing listens on port 1
        await until(lambda: len(results) >= 2)
        assert results[:2] == [False, False]
        await c.aclose()
    run(go())


# ---------------------------------------------------------------- discovery
def test_pick_deck_service():
    dock = Candidate("1.1.1.1", "Stream Deck Network Dock", 5343, {"dt": "215"})
    deck = Candidate("1.1.1.1", "TESTDECK0001", 20001, {"dt": "213", "pid": "6c", "sn": "TESTDECK0001"})
    s = pick([dock, deck])
    assert s and s.port == 20001 and s.serial == "TESTDECK0001"
    assert pick([dock]) is None


def test_locator_port_order_and_persistence(tmp_path):
    async def go():
        state = tmp_path / "state.json"
        mock = await MockDock().start()

        async def no_mdns():
            return None
        loc = DockLocator(no_mdns, str(state))
        loc.probe_ports = [1, mock.port]  # first refuses, second accepts
        loc.probe_timeout = 0.5
        assert await loc.pick_port("127.0.0.1") == mock.port
        loc.mark_good("127.0.0.1", mock.port)
        assert json.loads(state.read_text())["last_good"]["127.0.0.1"] == mock.port
        # reload: last-good wins without probing
        loc2 = DockLocator(no_mdns, str(state))
        loc2.probe_ports = [1]
        assert await loc2.pick_port("127.0.0.1") == mock.port
        # explicit port is honoured, until it fails once -> fall back to auto
        assert await loc2.pick_port("127.0.0.1", 20001) == 20001
        loc2.mark_failed("127.0.0.1", 20001)
        assert await loc2.pick_port("127.0.0.1", 20001) == mock.port
        loc2.mark_failed("127.0.0.1", mock.port)
        assert "127.0.0.1" not in loc2._good
        await mock.stop()
    run(go())


def test_locator_uses_mdns_port():
    async def go():
        mock = await MockDock().start()

        async def mdns():
            return DeckService("127.0.0.1", mock.port, "x", {"dt": "213"})
        loc = DockLocator(mdns)
        loc.probe_ports = [1]
        assert await loc.resolve() == ("127.0.0.1", mock.port)
        await mock.stop()
    run(go())


def test_locator_range_scan_finds_drifted_port():
    async def go():
        mock = await MockDock().start()

        async def no_mdns():
            return None
        loc = DockLocator(no_mdns)
        loc.probe_ports = [1]
        loc.SCAN_RANGE = range(mock.port - 2, mock.port + 3)
        assert await loc.pick_port("127.0.0.1") == mock.port
        await mock.stop()
    run(go())


def test_locator_prefers_mdns_port_over_last_good(tmp_path):
    async def go():
        a = await MockDock().start()      # the port mDNS advertises now
        b = await MockDock().start()      # the port that worked last session (the dock moves its port every session)
        state = tmp_path / "state.json"
        state.write_text(json.dumps({"last_good": {"127.0.0.1": b.port}}))

        async def mdns():
            return DeckService("127.0.0.1", a.port, "x", {"dt": "213"})
        loc = DockLocator(mdns, str(state))
        loc.probe_ports = [1]
        await loc.find_service()
        assert await loc.pick_port("127.0.0.1") == a.port
        # mDNS port unreachable: fall back to the last good port, then the probe list
        await a.stop()
        assert await loc.pick_port("127.0.0.1") == b.port
        assert DockLocator.PROBE_PORTS == (20003, 20002, 20001)
        await b.stop()
    run(go())
