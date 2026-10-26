import asyncio
import json
import time

import pytest

from deckoy.protocol import frame as fr
from deckoy.protocol.client import DockEndpoint, blank_jpeg
from deckoy.protocol.frame import Frame, FrameReader
from deckoy.proxy import advertise
from deckoy.proxy.proxy import ElgatoProxy, ProxyConfig
from deckoy.proxy.servers import DESCRIPTOR_PORT_OFFSET, FIRMWARE_WARNING, local_addresses, rewrite_descriptor_port
from deckoy.proxy.stats import ProxyStats
from tools.fake_elgato import FakeElgatoApp, expected_final_images
from tools.mock_dock import MockDock, MockDockService

from .conftest import FIXTURES

FIX5343 = json.load(open(FIXTURES / "dock5343.json"))


def run(coro, timeout=40):
    return asyncio.run(asyncio.wait_for(coro, timeout))


async def until(pred, timeout=8.0):
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


class Rig:
    """MockDock (deck service) + MockDockService (5343) + ElgatoProxy in front of them."""

    def __init__(self, tmp_path=None, forward_unknown=False, image_delay=0.0, dock_down=False, dock_busy=False, **cfg_kw):
        self.tmp_path, self.forward_unknown, self.image_delay, self.dock_down = tmp_path, forward_unknown, image_delay, dock_down
        self.dock_busy = dock_busy
        self.cfg_kw = cfg_kw
        self.warnings = []

    async def __aenter__(self):
        self.mock = await MockDock(keepalive_interval=0.2, image_delay=self.image_delay).start()
        self.svc = await MockDockService(keepalive_interval=0.1).start()
        if self.dock_busy:      # the live dock reports "in use" (byte 5 = 1) while Deckoy itself is connected to it
            busy = bytearray(self.svc.replies[b"\x03\x8f"])
            busy[5] = 1
            self.svc.replies[b"\x03\x8f"] = bytes(busy)
        cfg = ProxyConfig(dock_host="127.0.0.1", bind="127.0.0.1", deck_port=0, dock_service_port=0, advertise=False,
                          forward_unknown=self.forward_unknown, keepalive_interval=0.1,
                          state_dir=str(self.tmp_path) if self.tmp_path else None, **self.cfg_kw)

        async def deck_ep():
            return None if self.dock_down else DockEndpoint("127.0.0.1", self.mock.port)

        async def dock_ep():
            return None if self.dock_down else DockEndpoint("127.0.0.1", self.svc.port)

        self.proxy = ElgatoProxy(cfg, deck_ep, dock_ep)
        c = self.proxy.client
        c.prime_delay, c.probe_retry_delay, c.backoff = 0.01, 0.05, (0.05, 0.05, 0.1)
        self.proxy.upstream_dock.backoff = (0.05, 0.1)
        self.proxy.on_warning = self.warnings.append
        await self.proxy.start()
        self.apps = []
        return self

    async def app(self) -> FakeElgatoApp:
        a = await FakeElgatoApp("127.0.0.1", self.proxy.deck_server.port).connect()
        self.apps.append(a)
        return a

    async def __aexit__(self, *exc):
        for a in self.apps:
            await a.close()
        await self.proxy.stop()
        await self.mock.stop()
        await self.svc.stop()


# ---------------------------------------------------------------- replay of the real Elgato session
def test_replay_of_elgato_capture_through_proxy():
    async def go():
        async with Rig() as r:
            app = await r.app()
            expected = expected_final_images()
            n = await app.replay_capture()
            assert n == 509                               # 9 feature requests + 500 images, each got exactly one reply
            await until(lambda: len(r.mock.images) == 32)
            await until(lambda: all(r.mock.images.get(k) == v for k, v in expected.items()))
            assert r.mock.brightness == 63 and r.mock.sleep_seconds == 3600
            assert not r.mock.violations
            assert not app.closed.is_set()
            snap = r.proxy.stats.snapshot()
            assert snap["answered_local"] >= 509 and snap["app_connected"] and snap["blocked"] == 0
            await until(lambda: app.keepalives >= 2)      # proxy keepalives toward the app
            await until(lambda: r.mock.keepalive_replies >= 1)  # and Deckoy answers the dock's keepalives upstream
    run(go())


def test_handshake_replies_match_dock():
    async def go():
        async with Rig() as r:
            app = await r.app()
            r05 = await app.request(fr.CH_GET_FEATURE, b"\x05" + bytes(31))
            assert r05.type == fr.REPLY and r05.payload[6:14] == b"1.01.000"
            r06 = await app.request(fr.CH_GET_FEATURE, b"\x06" + bytes(31))
            assert r06.payload[2:2 + r06.payload[1]] == b"TESTDECK0001"
            r0b = await app.request(fr.CH_GET_FEATURE, b"\x0b" + bytes(31))
            assert r0b.payload[:2] == b"\x0b\x02"
    run(go())


# ---------------------------------------------------------------- upstream drop
def test_upstream_drop_is_hidden_from_the_app():
    async def go():
        async with Rig() as r:
            r.proxy.client.backoff = (0.8,)               # a visible outage window
            app = await r.app()
            imgs = {k: blank_jpeg((k * 7 % 255, 40, 90)) for k in range(32)}
            for k, j in imgs.items():
                await app.send_image(k, j)
            await app.request(fr.CH_SET_FEATURE, bytes([3, 8, 40]) + bytes(29))
            await until(lambda: len(r.mock.images) == 32 and r.mock.brightness == 40)
            r.mock.images.clear()
            r.mock.brightness = -1
            await until(lambda: r.proxy.stats.upstream_connected)
            r.mock.drop_clients()
            await until(lambda: not r.proxy.stats.upstream_connected)
            # while the dock link is down the app is still served: ACKs, replies, keepalives
            newer = blank_jpeg((255, 255, 0))
            t0 = time.monotonic()
            ack = await app.send_image(3, newer, timeout=1.0)
            assert ack.type == fr.ACK and time.monotonic() - t0 < 0.5
            assert (await app.request(fr.CH_GET_FEATURE, b"\x05" + bytes(31))).type == fr.REPLY
            await until(lambda: r.proxy.stats.upstream_connected, timeout=10)
            await until(lambda: len(r.mock.images) == 32 and r.mock.brightness == 40)
            assert r.mock.images[3] == newer             # the image sent during the outage arrived after recovery
            assert r.mock.images[10] == imgs[10]
            assert not app.closed.is_set()               # the app never saw a disconnect
            # a key press after recovery reaches the app
            await r.mock.inject_key(5, True)
            await r.mock.inject_key(5, False)
            await until(lambda: len(app.inputs) >= 2)
            assert app.inputs[-2][5] and not app.inputs[-1][5]
            snap = r.proxy.stats.snapshot()
            assert snap["upstream_drops"] == 1 and snap["drops_hidden"] == 1 and snap["app_disconnects"] == 0
    run(go())


def test_keys_down_during_a_drop_are_released_for_the_app():
    async def go():
        async with Rig() as r:
            app = await r.app()
            await until(lambda: r.proxy.stats.upstream_connected)
            await r.mock.inject_key(4, True)
            await until(lambda: app.inputs and app.inputs[-1][4])
            r.mock.drop_clients()
            await until(lambda: app.inputs and not any(app.inputs[-1]))
    run(go())


def test_app_sees_no_missing_ack_when_dock_never_acks_images():
    # the dock's deck service goes deaf (the suspected failure); the proxy still ACKs the app and recovers via probe/re-prime
    async def go():
        async with Rig() as r:
            app = await r.app()
            await until(lambda: r.proxy.stats.upstream_connected)
            r.mock.silent_connections = r.mock.connections_accepted + 1   # next connection is deaf
            r.mock.drop_clients()
            for k in range(8):
                assert (await app.send_image(k, blank_jpeg((k * 30, 0, 0)), timeout=1.0)).type == fr.ACK
            await until(lambda: r.proxy.stats.upstream_connected, timeout=15)
            await until(lambda: len(r.mock.images) >= 8)
            assert not app.closed.is_set()
    run(go(), timeout=60)


# ---------------------------------------------------------------- app reconnect pattern
def test_app_priming_then_real_connection_gets_input():
    async def go():
        async with Rig() as r:
            prime = await r.app()
            await prime.request(fr.CH_GET_FEATURE, b"\x05" + bytes(31))
            await prime.request(fr.CH_GET_FEATURE, b"\x06" + bytes(31))
            await prime.close()                           # the app's own "05/06, close, reconnect"
            real = await r.app()
            await real.request(fr.CH_GET_FEATURE, b"\x05" + bytes(31))
            await until(lambda: r.proxy.stats.upstream_connected)
            await r.mock.inject_key(1, True)
            await until(lambda: real.inputs)
            assert r.proxy.stats.snapshot()["app_disconnects"] == 0   # a short priming connection is not a disconnect
    run(go())


def test_newest_connection_gets_input_old_one_does_not():
    async def go():
        async with Rig() as r:
            old = await r.app()
            new = await r.app()
            await until(lambda: len(r.proxy.deck_server.conns) == 2)
            await until(lambda: r.proxy.stats.upstream_connected)
            await r.mock.inject_key(2, True)
            await until(lambda: new.inputs)
            await asyncio.sleep(0.1)
            assert not old.inputs
    run(go())


# ---------------------------------------------------------------- allow-list
def unknown_set(payload_tail=b"\x7f\x01\x02"):
    return b"\x03" + payload_tail + bytes(32 - 1 - len(payload_tail))


def test_unknown_set_feature_is_acked_logged_not_forwarded():
    async def go():
        async with Rig() as r:
            app = await r.app()
            rep = await app.request(fr.CH_SET_FEATURE, unknown_set())
            assert rep.type == fr.ACK
            await asyncio.sleep(0.1)
            assert not any(q.payload[:2] == b"\x03\x7f" for q in r.mock.requests)
            snap = r.proxy.stats.snapshot()
            assert snap["unknown_recent"] and "037f" in snap["unknown_recent"][-1]
            assert snap["forwarded"] == 0 and snap["blocked"] == 0
    run(go())


def test_unknown_set_feature_forwarded_when_policy_on():
    async def go():
        async with Rig(forward_unknown=True) as r:
            app = await r.app()
            await until(lambda: r.proxy.stats.upstream_connected)
            assert (await app.request(fr.CH_SET_FEATURE, unknown_set())).type == fr.ACK
            await until(lambda: any(q.payload[:2] == b"\x03\x7f" for q in r.mock.requests))
            assert r.proxy.stats.snapshot()["forwarded"] == 1
    run(go())


def test_bulk_unknown_transfer_is_blocked_with_warning():
    async def go():
        async with Rig(forward_unknown=True) as r:
            app = await r.app()
            await until(lambda: r.proxy.stats.upstream_connected)
            big = b"\x03\x7e" + bytes(2046)
            for _ in range(4):                              # 4 x 2 KB of unknown payload: over the 4 KB budget
                assert (await app.request(fr.CH_SET_FEATURE, big)).type == fr.ACK   # the app is never left waiting
            await asyncio.sleep(0.1)
            forwarded = [q for q in r.mock.requests if q.payload[:2] == b"\x03\x7e"]
            assert 1 <= len(forwarded) < 4
            snap = r.proxy.stats.snapshot()
            assert snap["blocked"] >= 1
            assert r.warnings == [FIRMWARE_WARNING]
    run(go())


def test_unknown_get_feature_and_image_like_junk_do_not_stall_the_app():
    async def go():
        async with Rig() as r:
            app = await r.app()
            rep = await app.request(fr.CH_GET_FEATURE, b"\x44" + bytes(31))
            assert rep.type == fr.REPLY and rep.payload[0] == 0x44
            junk = Frame(fr.REQUEST, fr.CH_IMAGE, 99, b"\x09\x09" + bytes(1022))
            app._writer.write(junk.encode())
            await asyncio.sleep(0.1)
            assert any(f.seq == 99 and f.type == fr.ACK for f in app.reply_log)
    run(go())


# ---------------------------------------------------------------- port 5343
async def dock_conn(port):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    return reader, writer


def test_dock_service_replies_are_identical_to_the_capture():
    async def go():
        async with Rig() as r:
            reader, writer = await dock_conn(r.proxy.dock_server.port)
            fread = FrameReader()
            got = {}
            for rid_hex in FIX5343["replies_by_request_id"]:
                writer.write(Frame(fr.KEEPALIVE, 0, 0, bytes.fromhex(rid_hex).ljust(1024, b"\x00")).encode())
                await writer.drain()
                while rid_hex not in got:
                    for f in fread.feed(await asyncio.wait_for(reader.read(65536), 3)):
                        if f.payload[:2] == b"\x01\x0a":
                            writer.write(Frame(fr.KEEPALIVE, 0, 0, bytes([3, 0x1A]).ljust(1024, b"\x00")).encode())
                            continue
                        got["031c" if f.payload[:2] == b"\x01\x0b" else f.payload[:2].hex()] = f.payload
            for rid_hex, expect_hex in FIX5343["replies_by_request_id"].items():
                expect = bytes.fromhex(expect_hex)
                if rid_hex == "031c":      # the deck port inside the descriptor points at the proxy, nothing else differs
                    expect = rewrite_descriptor_port(expect, r.proxy.deck_server.port)
                    assert got[rid_hex] != bytes.fromhex(expect_hex)
                assert got[rid_hex] == expect, rid_hex
            writer.close()
    run(go())


def test_descriptor_port_rewrite():
    d = bytes.fromhex(FIX5343["replies_by_request_id"]["031c"])
    assert int.from_bytes(d[DESCRIPTOR_PORT_OFFSET:DESCRIPTOR_PORT_OFFSET + 2], "little") == FIX5343["deck_port_in_capture"]
    assert rewrite_descriptor_port(d, FIX5343["deck_port_in_capture"]) == d
    w = rewrite_descriptor_port(d, 20002)
    assert w[:DESCRIPTOR_PORT_OFFSET] == d[:DESCRIPTOR_PORT_OFFSET] and w[DESCRIPTOR_PORT_OFFSET + 2:] == d[DESCRIPTOR_PORT_OFFSET + 2:]
    assert int.from_bytes(w[DESCRIPTOR_PORT_OFFSET:DESCRIPTOR_PORT_OFFSET + 2], "little") == 20002


def test_dock_service_keepalive_roundtrip():
    async def go():
        async with Rig() as r:
            reader, writer = await dock_conn(r.proxy.dock_server.port)
            fread = FrameReader()
            seen = 0
            while seen < 3:
                for f in fread.feed(await asyncio.wait_for(reader.read(65536), 3)):
                    if f.payload[:6] == bytes.fromhex(FIX5343["keepalive_from_dock"])[:6]:
                        seen += 1
                        writer.write(Frame(fr.KEEPALIVE, 0, 0, bytes([3, 0x1A]).ljust(1024, b"\x00")).encode())
            conn = r.proxy.dock_server.conns[0]
            await until(lambda: conn.last_keepalive_reply is not None)
            writer.close()
    run(go())


def test_dock_service_unknown_request_not_forwarded_by_default():
    async def go():
        async with Rig() as r:
            reader, writer = await dock_conn(r.proxy.dock_server.port)
            writer.write(Frame(fr.KEEPALIVE, 0, 0, b"\x03\x99" + bytes(1022)).encode())
            await writer.drain()
            await asyncio.sleep(0.15)
            assert b"\x03\x99" not in r.svc.requests
            assert r.proxy.stats.snapshot()["unknown_recent"]
            writer.close()
    run(go())


# ---------------------------------------------------------------- start-up with the dock unreachable
def test_cached_replies_answer_the_app_while_the_dock_is_down(tmp_path):
    async def go():
        async with Rig(tmp_path=tmp_path) as r:               # first run: learn the replies
            app = await r.app()
            first05 = await app.request(fr.CH_GET_FEATURE, b"\x05" + bytes(31))
            await until(lambda: "031c" in r.proxy.upstream_dock.replies)
            await asyncio.sleep(0.1)
        async with Rig(tmp_path=tmp_path, dock_down=True) as r:   # second run: dock unreachable
            app = await r.app()
            t0 = time.monotonic()
            rep = await app.request(fr.CH_GET_FEATURE, b"\x05" + bytes(31))
            assert time.monotonic() - t0 < 1.0 and rep.payload == first05.payload
            reader, writer = await dock_conn(r.proxy.dock_server.port)
            writer.write(Frame(fr.KEEPALIVE, 0, 0, b"\x03\x84" + bytes(1022)).encode())
            await writer.drain()
            fread, f = FrameReader(), None
            while f is None:      # skip the greeting keepalive the service sends on connect
                for x in fread.feed(await asyncio.wait_for(reader.read(65536), 2)):
                    if x.payload[:2] == b"\x03\x84":
                        f = x
            assert f.payload == bytes.fromhex(FIX5343["replies_by_request_id"]["0384"])
            writer.close()
    run(go())


# ---------------------------------------------------------------- coalescing
def test_rapid_images_are_acked_immediately_and_coalesced_upstream():
    async def go():
        async with Rig(image_delay=0.05) as r:
            app = await r.app()
            await until(lambda: r.proxy.stats.upstream_connected)
            before = len([1 for k, _ in r.mock.image_log if k == 3])
            frames = [blank_jpeg((i * 5 % 255, 0, i * 3 % 255), quality=60 + i % 30) for i in range(50)]
            t0 = time.monotonic()
            for j in frames:
                assert (await app.send_image(3, j, timeout=1.0)).type == fr.ACK
            assert time.monotonic() - t0 < 2.0                 # 50 ACKs while upstream needs 50 x 50 ms = 2.5 s
            await until(lambda: r.mock.images.get(3) == frames[-1])
            got = len([1 for k, _ in r.mock.image_log if k == 3]) - before
            assert got < 25                                    # far fewer than 50 reached the dock
    run(go())


# ---------------------------------------------------------------- stats, advertising, discovery
def test_stats_counting_and_persistence(tmp_path):
    t = [1_000_000.0]
    path = str(tmp_path / "stats.json")
    s = ProxyStats(path, clock=lambda: t[0])
    s.upstream_up()
    s.upstream_down()                          # no app connected: a drop, not a hidden drop
    s.upstream_up()
    s.app_opened()
    s.upstream_down()
    assert (s.upstream_drops, s.drops_hidden) == (2, 1)
    t[0] += 5
    s.upstream_up()
    assert round(s.snapshot()["downtime_s"]) >= 5
    s.app_closed(0.2)                          # priming connection
    assert s.app_disconnects == 0
    s.app_opened()
    s.app_closed(30)
    assert s.app_disconnects == 1
    s2 = ProxyStats(path, clock=lambda: t[0])
    assert s2.drops_hidden == 1 and s2.upstream_drops == 2 and s2.snapshot()["hidden_today"] == 1
    assert any("Drops hidden today: 1" in ln for ln in s2.summary_lines())


def test_advertiser_records_copy_txt_and_names():
    adv = advertise.Advertiser("127.0.0.1", 5343, 20002, naming=advertise.NAMING_DISTINCT)
    docks, decks = adv.service_infos()
    assert docks.name == "Stream Deck Network Dock (Deckoy)._elg._tcp.local."
    assert docks.port == 5343 and decks.port == 20002
    assert docks.properties[b"dt"] == b"215" and docks.properties[b"sn"] == b"DECKOY000000"
    assert decks.properties[b"sn"] == b"DECKOY000001" and decks.properties[b"pid"] == b"0x006C"
    assert docks.parsed_addresses() == ["127.0.0.1"]
    same = advertise.Advertiser("127.0.0.1", 1, 2, dock_name="X", deck_name="Y", naming=advertise.NAMING_SAME)
    assert same.names == ["X", "Y"]


def test_discovery_excludes_own_records(monkeypatch):
    from deckoy.protocol import discovery
    own = discovery.Candidate("127.0.0.1", "TESTDECK0001 (Deckoy)", 20002, {"dt": "213", "pid": "0x006C"})
    real = discovery.Candidate("192.168.1.50", "TESTDECK0001", 20003, {"dt": "213", "pid": "0x006C"})

    async def fake(timeout=5.0):
        return [own, real]
    monkeypatch.setattr(discovery, "discover_all", fake)
    got = asyncio.run(discovery.discover(1, exclude_hosts=["127.0.0.1"]))
    assert got.host == "192.168.1.50"
    got = asyncio.run(discovery.discover(1, exclude_names=["TESTDECK0001 (Deckoy)"]))
    assert got.host == "192.168.1.50"


def test_loopback_bind_is_not_advertised():
    adv = advertise.Advertiser("127.0.0.1", 5343, 20002)
    assert asyncio.run(adv.start()) is False


def test_lan_ip_helper_and_peer_filter():
    from deckoy.proxy.proxy import lan_ip_for
    from deckoy.proxy.servers import DeckServer

    class W:
        def __init__(self, ip):
            self.ip = ip

        def get_extra_info(self, name):
            return (self.ip, 5000)
    assert lan_ip_for("127.0.0.1") == "127.0.0.1"
    srv = DeckServer("192.168.1.20", 0, core=None)
    assert srv.peer_allowed(W("192.168.1.20")) and srv.peer_allowed(W("127.0.0.1"))
    assert not srv.peer_allowed(W("192.168.1.99"))
    # connections to ANY of this PC's own addresses arrive with that address as the source: all are this PC
    for ip in local_addresses():
        assert srv.peer_allowed(W(ip))


def test_dock_service_greets_with_an_immediate_keepalive():
    # the Elgato app's first connection to the dock service is a probe that closes ~15 ms after connecting unless the
    # dock has already sent its keepalive (as the real dock does)
    async def go():
        async with Rig() as r:
            reader, writer = await dock_conn(r.proxy.dock_server.port)
            t0 = time.monotonic()
            data = await asyncio.wait_for(reader.read(65536), 1.0)
            assert time.monotonic() - t0 < 0.05
            f = FrameReader().feed(data)[0]
            assert f.type == fr.KEEPALIVE and f.payload == bytes.fromhex(FIX5343["keepalive_from_dock"])
            writer.close()
            # the deck service does not greet (the real deck service doesn't either)
            app = await r.app()
            await asyncio.sleep(0.03)
            assert app.keepalives == 0
    run(go())


# ---------------------------------------------------------------- the app drops its own session: log + re-announce
class FakeAdvertiser:
    def __init__(self):
        self.calls = []

    async def reannounce(self, which, deck_port, delay):
        self.calls.append((which, deck_port, delay))
        return True

    async def stop(self, fast=False):
        pass


def test_app_session_drop_is_logged_and_reannounced_on_a_new_port(caplog):
    import socket
    from tools.fake_elgato import FakeElgatoApp

    async def go():
        async with Rig(reannounce_delay=0.0, established_after=0.2) as r:
            r.proxy.advertiser = FakeAdvertiser()
            old_port = r.proxy.deck_server.port
            await until(lambda: r.proxy.stats.upstream_connected)
            upstream_conns = r.mock.connections_accepted
            app = await r.app()
            await asyncio.sleep(0.35)                     # an established session, not a priming connection
            await app.close()                             # the app aborts its own session
            await until(lambda: r.proxy.advertiser.calls)
            which, port, delay = r.proxy.advertiser.calls[0]
            assert which == "deck" and delay == 0.0
            assert port == r.proxy.deck_server.port and port != old_port
            await until(lambda: r.proxy.stats.snapshot()["reannounces"] == 1)
            snap = r.proxy.stats.snapshot()
            assert snap["app_drop_count"] == 1 and snap["last_app_drop"] is not None
            assert r.proxy.stats.app_drops[0]["by_us"] is False
            # the dock link stayed up the whole time (no reconnect upstream)
            assert r.proxy.stats.upstream_connected and r.mock.connections_accepted == upstream_conns
            # the app can come back on the new port; the 5343 descriptor advertises it; the old port is closed
            app2 = await FakeElgatoApp("127.0.0.1", port).connect()
            assert (await app2.request(fr.CH_GET_FEATURE, b"\x05" + bytes(31))).type == fr.REPLY
            await app2.close()
            reader, writer = await dock_conn(r.proxy.dock_server.port)
            writer.write(Frame(fr.KEEPALIVE, 0, 0, b"\x03\x1c" + bytes(1022)).encode())
            await writer.drain()
            fread, desc = FrameReader(), None
            while desc is None:
                for x in fread.feed(await asyncio.wait_for(reader.read(65536), 2)):
                    if x.payload[:2] == b"\x01\x0b":
                        desc = x.payload
            assert int.from_bytes(desc[DESCRIPTOR_PORT_OFFSET:DESCRIPTOR_PORT_OFFSET + 2], "little") == port
            writer.close()
            with socket.socket() as s:
                s.settimeout(0.5)
                assert s.connect_ex(("127.0.0.1", old_port)) != 0
    with caplog.at_level("WARNING", logger="deckoy.proxy"):
        run(go())
    assert any("ELGATO APP SESSION ENDED" in rec.message for rec in caplog.records)


def test_priming_connections_are_not_drops():
    async def go():
        async with Rig(reannounce_delay=0.0, established_after=0.5) as r:
            r.proxy.advertiser = FakeAdvertiser()
            for _ in range(3):
                a = await r.app()
                await a.request(fr.CH_GET_FEATURE, b"\x05" + bytes(31))
                await a.close()
            await asyncio.sleep(0.2)
            assert r.proxy.advertiser.calls == [] and r.proxy.stats.snapshot()["app_drop_count"] == 0
    run(go())


def test_reannounce_off_only_logs_the_drop():
    async def go():
        async with Rig(reannounce="off", established_after=0.1) as r:
            r.proxy.advertiser = FakeAdvertiser()
            app = await r.app()
            await asyncio.sleep(0.25)
            await app.close()
            await until(lambda: r.proxy.stats.snapshot()["app_drop_count"] == 1)
            await asyncio.sleep(0.2)
            assert r.proxy.advertiser.calls == [] and r.proxy.stats.snapshot()["reannounces"] == 0
    run(go())


def test_reannounce_both_and_same_port_option():
    async def go():
        async with Rig(reannounce="both", reannounce_new_port=False, reannounce_delay=0.0, established_after=0.1) as r:
            r.proxy.advertiser = FakeAdvertiser()
            port = r.proxy.deck_server.port
            app = await r.app()
            await asyncio.sleep(0.25)
            await app.close()
            await until(lambda: r.proxy.advertiser.calls)
            assert r.proxy.advertiser.calls[0] == ("both", None, 0.0) and r.proxy.deck_server.port == port
    run(go())


def test_test_hook_drops_the_app_session_and_marks_it():
    async def go():
        async with Rig(reannounce_delay=0.0, established_after=0.1) as r:
            r.proxy.advertiser = FakeAdvertiser()
            app = await r.app()
            await asyncio.sleep(0.25)
            assert await r.proxy.drop_app_session() is True
            await asyncio.wait_for(app.closed.wait(), 3)
            await until(lambda: r.proxy.advertiser.calls)
            assert r.proxy.stats.app_drops[0]["by_us"] is True
            assert await r.proxy.drop_app_session() is False      # nothing left to drop
    run(go())


def test_advertiser_reannounce_sends_goodbye_then_new_registration():
    class FakeAzc:
        def __init__(self):
            self.ops = []

        async def async_unregister_service(self, info):
            self.ops.append(("unregister", info.name, info.port))

        async def async_register_service(self, info, cooperating_responders=False):
            self.ops.append(("register", info.name, info.port, cooperating_responders))

    async def go():
        adv = advertise.Advertiser("192.168.1.20", 5343, 20002)
        adv._azc = FakeAzc()
        adv._infos = adv.service_infos()
        assert await adv.reannounce("deck", 20009, 0) is True
        deck = "Stream Deck XL (Deckoy)._elg._tcp.local."
        assert adv._azc.ops == [("unregister", deck, 20002), ("register", deck, 20009, True)]
        assert adv.deck_port == 20009
        adv._azc.ops.clear()
        assert await adv.reannounce("both", None, 0) is True
        assert [o[0] for o in adv._azc.ops] == ["unregister", "unregister", "register", "register"]
        assert adv._azc.ops[0][2] == 5343 and adv._azc.ops[-1][2] == 20009
        adv._azc = None
        assert await adv.reannounce("deck", None, 0) is False       # not advertising (loopback bind): nothing to do
    run(go())


def test_app_drops_persist_in_stats(tmp_path):
    s = ProxyStats(str(tmp_path / "s.json"))
    s.record_app_drop(125.4, by_us=False)
    s.note_reannounce()
    s2 = ProxyStats(str(tmp_path / "s.json"))
    assert s2.app_drops[0]["duration"] == 125.4 and s2.reannounces == 1


def test_srv_target_is_the_computer_name_not_an_invented_host():
    import socket as _socket
    adv = advertise.Advertiser("192.168.1.20", 5343, 20002)
    docks, decks = adv.service_infos()
    expected = advertise.proxy_hostname()
    assert expected.endswith(".local.") and expected.lower().startswith(_socket.gethostname().split(".")[0][:5].lower())
    assert docks.server == expected and decks.server == expected
    assert advertise.Advertiser("192.168.1.20", 1, 2, hostname="X.local.").service_infos()[0].server == "X.local."


def test_advertiser_refreshes_its_announcements_periodically():
    class FakeZc:
        def __init__(self):
            self.sent = []

        def generate_service_broadcast(self, info, ttl, broadcast_addresses):
            return ("announce", info.name, ttl)

        def async_send(self, out):
            self.sent.append(out)

    class FakeAzc:
        zeroconf = FakeZc()

    async def go():
        adv = advertise.Advertiser("192.168.1.20", 5343, 20002, refresh_interval=0.05)
        adv._azc = FakeAzc()
        adv._infos = adv.service_infos()
        adv._refresh_task = asyncio.ensure_future(adv._refresh_loop())
        await asyncio.sleep(0.28)
        await adv.stop()
        sent = FakeAzc.zeroconf.sent
        names = {s[1] for s in sent}
        assert len(sent) >= 6 and names == {i.name for i in adv.service_infos()}   # both services, several rounds
        assert await adv.refresh_once() == 0                                       # stopped: nothing is sent any more
    run(go())


def test_proxy_accepts_connections_on_every_local_address():
    # The Elgato app resolves this PC's name to ALL its addresses and tries them in order with a 1 s budget; an address
    # nothing listens on hangs, so the proxy must answer on each of them.
    import socket as _socket

    async def go():
        async with Rig(listen_all=True) as r:
            assert r.proxy.deck_server.host == "0.0.0.0"
            port = r.proxy.deck_server.port
            ips = [ip for ip in local_addresses() if ":" not in ip]
            assert "127.0.0.1" in ips
            for ip in ips:
                a = await FakeElgatoApp(ip, port).connect()
                assert (await a.request(fr.CH_GET_FEATURE, b"\x05" + bytes(31), timeout=2)).type == fr.REPLY
                await a.close()
    run(go())


def test_proxy_also_listens_on_ipv6_and_normalises_scoped_peers():
    import socket as _socket
    from deckoy.proxy.servers import norm_ip

    assert norm_ip("FE80::CD55:106A:EBBC:4701%12") == "fe80::cd55:106a:ebbc:4701"
    assert norm_ip("::ffff:127.0.0.1") == "127.0.0.1"

    async def go():
        async with Rig(listen_all=True) as r:
            port = r.proxy.deck_server.port
            v6 = [i for i in _socket.getaddrinfo(_socket.gethostname(), port, _socket.AF_INET6, _socket.SOCK_STREAM)]
            targets = [("::1", port)] + [(i[4][0], port) for i in v6[:2]]
            ok = 0
            for host, p in targets:
                try:
                    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, p), 1.0)
                except (OSError, asyncio.TimeoutError):
                    continue
                writer.write(Frame(fr.REQUEST, fr.CH_GET_FEATURE, 9, b"\x05" + bytes(31)).encode())
                await writer.drain()
                data = await asyncio.wait_for(reader.read(65536), 2)
                assert FrameReader().feed(data)[0].type == fr.REPLY
                writer.close()
                ok += 1
            assert ok == len(targets), f"IPv6 connections that worked: {ok} of {len(targets)}"
    run(go())


def test_dock_service_presents_itself_as_free_even_when_the_real_dock_reports_busy():
    from deckoy.proxy.proxy import present_as_free
    real = bytes.fromhex(FIX5343["replies_by_request_id"]["038f"])
    assert real[5] == 0                                   # capture: nobody connected
    assert present_as_free(bytes([*real[:5], 1, *real[6:]])) == real

    async def go():
        async with Rig(dock_busy=True) as r:
            await until(lambda: r.proxy.upstream_dock.replies.get("038f", b"\x00" * 6)[5] == 1)   # proxy cached "busy"
            reader, writer = await dock_conn(r.proxy.dock_server.port)
            writer.write(Frame(fr.KEEPALIVE, 0, 0, b"\x03\x8f" + bytes(1022)).encode())
            await writer.drain()
            fread, got = FrameReader(), None
            while got is None:
                for x in fread.feed(await asyncio.wait_for(reader.read(65536), 2)):
                    if x.payload[:2] == b"\x03\x8f":
                        got = x.payload
            assert got == real                              # served as "free", identical to the capture
            writer.close()
    run(go())


def test_stats_snapshot_lists_recent_drops(tmp_path):
    t = [1_000_000.0]
    s = ProxyStats(str(tmp_path / "s.json"), clock=lambda: t[0])
    s.upstream_up()
    t[0] += 5
    s.upstream_down()
    s.record_app_drop(42.0)
    snap = s.snapshot()
    assert snap["dock_drops_recent"] == [1_000_005.0] and snap["app_drops_recent"][0]["duration"] == 42.0
    again = ProxyStats(str(tmp_path / "s.json"), clock=lambda: t[0])
    assert again.snapshot()["dock_drops_recent"] == [1_000_005.0]


def test_generic_fallback_records_use_the_handshake_serial_and_carry_no_private_ids(tmp_path, monkeypatch, caplog):
    import logging
    from deckoy.proxy import proxy as proxy_mod

    attempts = []

    async def no_records(self):
        attempts.append(1)               # the real dock's mDNS records can not be learned

    async def fake_start(self):
        return True                      # no real mDNS in a unit test
    monkeypatch.setattr(ElgatoProxy, "_learn_records", no_records)
    monkeypatch.setattr(advertise.Advertiser, "start", fake_start)
    monkeypatch.setattr(proxy_mod, "LEARN_BACKOFF", 0.01)

    async def go():
        async with Rig(tmp_path=tmp_path) as r:
            r.proxy.locator = object()                      # truthy: records are meant to be learned
            await until(lambda: r.proxy.client.info is not None)
            await r.proxy._advertise()
            adv = r.proxy.advertiser
            assert len(attempts) == proxy_mod.LEARN_ATTEMPTS == 3
            assert adv.deck_txt["sn"] == "TESTDECK0001" and adv.deck_name == "TESTDECK0001"      # from the handshake
            assert adv.dock_txt == advertise.DEFAULT_DOCK_TXT and adv.dock_name == "Stream Deck Network Dock"
            for key in ("md", "dt", "vid", "pid", "fw", "pv", "prt"):
                assert key in adv.deck_txt
            assert adv.names == ["Stream Deck Network Dock (Deckoy)", "TESTDECK0001 (Deckoy)"]
    with caplog.at_level(logging.WARNING, logger="deckoy.proxy"):
        run(go())
    assert any("GENERIC mDNS records" in rec.getMessage() for rec in caplog.records)


def test_generic_defaults_hold_no_real_device_ids():
    blob = repr((advertise.DEFAULT_DOCK_NAME, advertise.DEFAULT_DOCK_TXT, advertise.DEFAULT_DECK_NAME,
                 advertise.DEFAULT_DECK_TXT))
    assert "00:00:00:00:00:00" in blob and "DECKOY0000" in blob
    for key in ("md", "dt", "vid", "pid", "fw", "pv", "prt", "hwrev", "mf"):
        assert key in advertise.DEFAULT_DOCK_TXT or key in advertise.DEFAULT_DECK_TXT


def test_own_records_are_excluded_when_learning(monkeypatch, tmp_path):
    from deckoy.protocol import discovery
    seen = []

    async def fake_all(timeout=5.0):
        mine = discovery.Candidate("192.168.1.20", "Stream Deck XL (Deckoy)", 20002, {"dt": "213", "pid": "0x006C"})
        old = discovery.Candidate("192.168.1.20", "Stream Deck XL (DeckPad)", 20002, {"dt": "213", "pid": "0x006C"})
        real = discovery.Candidate("192.168.1.50", "Stream Deck XL", 20003, {"dt": "213", "pid": "0x006C", "sn": "X"})
        seen.append(1)
        return [mine, old, real]
    monkeypatch.setattr(discovery, "discover_all", fake_all)

    async def go():
        async with Rig(tmp_path=tmp_path) as r:
            await r.proxy._learn_records()
            assert r.proxy.state.deck_name == "Stream Deck XL" and r.proxy.state.deck_txt["sn"] == "X"
    run(go())
