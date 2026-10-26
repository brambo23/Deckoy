from deckoy.protocol import frame as fr
from deckoy.protocol.frame import Frame
from tools.pcap_decode import Conn, build_allowlist, diagnose_conn, request_key


def conn(frames, closes=(), port=20002):
    return Conn(1, port=port, frames=list(frames), closes=list(closes), last=max(t for t, _, _ in frames))


def test_unanswered_request_and_dock_close():
    img = Frame(fr.REQUEST, 0, 7, bytes(1024))
    c = conn([(1.0, "pc", Frame(fr.REQUEST, 2, 5, b"\x05" + bytes(31))), (1.01, "dock", Frame(fr.REPLY, 0, 5, bytes(32))),
              (10.0, "pc", img)], closes=[(15.0, "pc", "FIN")])
    d = diagnose_conn(c)
    assert d["first_closer"] == "pc FIN"
    assert d["unanswered_requests"][0]["seq"] == 7 and d["unanswered_requests"][0]["waited_s"] == 5.0
    assert any("UNANSWERED" in v for v in d["verdict"])


def test_keepalive_tracking():
    ka = Frame(fr.KEEPALIVE, 0, 0, b"\x01\x0a" + bytes(510))
    rep = Frame(fr.KEEPALIVE, 0, 0, b"\x03\x1a" + bytes(1022))
    c = conn([(0.0, "dock", ka), (0.05, "pc", rep), (4.0, "dock", ka), (8.0, "dock", ka), (8.05, "pc", rep)])
    d = diagnose_conn(c, end_time=30.0)
    assert d["dock_keepalives"] == 3 and d["pc_keepalive_replies"] == 2
    assert d["dock_keepalives_unanswered_by_pc"] == [4.0]
    assert d["dock_keepalive_stopped_before_end_s"] == 22.0
    assert any("DOCK stopped sending keepalives" in v for v in d["verdict"])


def test_healthy_connection():
    c = conn([(0.0, "pc", Frame(fr.REQUEST, 1, 3, bytes(32))), (0.004, "dock", Frame(fr.ACK, 0, 3, bytes(4)))])
    d = diagnose_conn(c)
    assert d["verdict"] == ["nothing abnormal in this connection"] and d["max_reply_latency_s"] == 0.004


def test_request_keys_and_allowlist():
    assert request_key(5343, Frame(fr.KEEPALIVE, 0, 0, b"\x03\x83" + bytes(1022))) == "type00 03 83"
    assert request_key(20002, Frame(fr.REQUEST, 2, 1, b"\x0b" + bytes(31))) == "ch2 0b"
    assert request_key(20002, Frame(fr.REQUEST, 1, 1, b"\x03\x08\x3f" + bytes(29))) == "ch1 03 08"
    assert request_key(20002, fr.chunk_image(0, b"x", 1)[0]) == "ch0 image-chunk 02 07"
    c = conn([(0.0, "pc", Frame(fr.REQUEST, 1, 3, b"\x03\x08\x3f" + bytes(29))), (0.01, "dock", Frame(fr.ACK, 0, 3, bytes(4)))])
    t = build_allowlist({1: c})
    assert t["20002"]["ch1 03 08"]["reply_types"] == ["0x82"]
