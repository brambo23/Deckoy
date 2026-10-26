import pytest

from deckoy.protocol import frame as fr
from deckoy.protocol.frame import Frame, FrameError, FrameReader

from .conftest import FIXTURES


def test_roundtrip_and_header_layout():
    f = Frame(fr.REQUEST, 2, 1971, b"\x05" + bytes(31))
    raw = f.encode()
    assert raw[:4] == bytes.fromhex("43938a41")
    assert len(raw) == 16 + 32
    got, used = fr.try_decode(raw)
    assert got == f and used == len(raw)


def test_reader_split_and_merged():
    a, b = Frame(0x81, 0, 1, b"abc"), Frame(0x82, 0, 2, bytes(4))
    raw = a.encode() + b.encode()
    r = FrameReader()
    out = []
    for i in range(len(raw)):  # one byte at a time
        out += r.feed(raw[i:i + 1])
    assert out == [a, b] and r.pending == 0
    assert FrameReader().feed(raw) == [a, b]


def test_bad_magic():
    with pytest.raises(FrameError):
        FrameReader().feed(b"\x00" * 16)


def test_chunk_sizes_and_types():
    data = bytes(range(256)) * 10  # 2560 bytes -> 3 chunks
    frames = fr.chunk_image(5, data, 42)
    assert len(frames) == 3
    assert [f.type for f in frames] == [0x80, 0x80, 0xC0]
    assert frames[2].seq == 42 and frames[0].seq == 0
    assert all(len(f.payload) == 1024 for f in frames)
    assert fr.reassemble(f.payload for f in frames) == data


def test_chunk_exact_multiple_has_no_empty_tail():
    assert len(fr.chunk_image(0, bytes(1016), 1)) == 1
    assert len(fr.chunk_image(0, bytes(1017), 1)) == 2


def test_golden_counts(captured):
    pc = [c for c in captured if not c[1]]
    dock = [c for c in captured if c[1]]
    assert sum(1 for c in pc if c[3].type == 0xC0) == 511
    assert sum(1 for c in pc if c[3].type == 0x80 and c[3].channel == 0) == 1469
    assert sum(1 for c in dock if c[3].type == 0x82) == 506
    assert sum(1 for c in dock if c[3].type == 0x81) == 5
    assert sum(1 for c in dock if c[3].type == 0x80) == 4
    assert sum(1 for c in dock if c[3].type == 0x00) == 3


def test_golden_rechunk_identical(captured):
    pending = {}
    images = 0
    for _, from_dock, _, f in captured:
        if from_dock or f.channel != 0 or len(f.payload) < 8 or f.payload[:2] != b"\x02\x07":
            continue
        key = f.payload[2]
        lst = pending.setdefault(key, [])
        lst.append(f)
        if f.payload[3] != 1:
            continue
        jpeg = fr.reassemble(x.payload for x in lst)
        assert jpeg[:3] == b"\xff\xd8\xff"
        rebuilt = fr.chunk_image(key, jpeg, f.seq)
        assert len(rebuilt) == len(lst)
        for orig, new in zip(lst, rebuilt):
            assert (orig.type, orig.channel, orig.seq) == (new.type, new.channel, new.seq)
            used = 8 + int.from_bytes(orig.payload[4:6], "little")
            assert orig.payload[:used] == new.payload[:used]  # padding in the capture is stale memory
            assert not any(new.payload[used:])
        del pending[key]
        images += 1
    assert images == 500 and not pending


def test_fixture_jpeg_single_chunk():
    jpg = (FIXTURES / "key0_0.jpg").read_bytes()
    chunks = fr.chunk_image(0, jpg, 1974)
    assert len(chunks) == 1 and chunks[0].type == 0xC0
    assert fr.reassemble(c.payload for c in chunks) == jpg


def test_golden_input_reports(captured):
    inputs = [c[3] for c in captured if c[1] and c[3].type == 0x80]
    assert len(inputs) == 4
    prev = [False] * 32
    events = []
    for f in inputs:
        assert len(f.payload) == 512
        keys = fr.parse_input_report(f.payload)
        assert keys is not None
        events += list(fr.diff_keys(prev, keys))
        prev = keys
    assert sum(1 for e in events if e[1]) == 2 and sum(1 for e in events if not e[1]) == 2


def test_input_report_rejects_other():
    assert fr.parse_input_report(b"\x01\x0a" + bytes(40)) is None
    assert fr.parse_input_report(b"\x01\x00\x20\x00") is None


def test_golden_handshake_replies(captured):
    reps = [c[3].payload for c in captured if c[1] and c[3].type == 0x81]
    fw = next(p for p in reps if p[0] == 5)
    sn = next(p for p in reps if p[0] == 6)
    assert fw[6:14] == b"1.01.000"
    assert sn[2:2 + sn[1]] == b"TESTDECK0001"
