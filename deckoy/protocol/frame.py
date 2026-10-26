"""Bragi framing, image chunking and input reports (port of SDDock.Protocol)."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple

MAGIC = 0x418A9343
HEADER_SIZE = 16
MAX_PAYLOAD = 1 << 20
_HDR = struct.Struct("<IBBBBII")

# Frame types
REQUEST = 0xC0   # PC->dock, wants one reply
NO_REPLY = 0x80  # PC->dock image chunk (seq 0); dock->PC input report
REPLY = 0x81
ACK = 0x82
KEEPALIVE = 0x00

# Channels (PC->dock)
CH_IMAGE = 0
CH_SET_FEATURE = 1
CH_GET_FEATURE = 2

CHUNK_PAYLOAD = 1024
CHUNK_HEADER = 8
DATA_PER_CHUNK = CHUNK_PAYLOAD - CHUNK_HEADER  # 1016

INPUT_KEYS = 32


class FrameError(Exception):
    """Bad magic or absurd length: the caller should drop the connection."""


@dataclass(frozen=True)
class Frame:
    type: int
    channel: int
    seq: int
    payload: bytes = b""

    def encode(self) -> bytes:
        return _HDR.pack(MAGIC, 0, self.type, self.channel, 0, self.seq, len(self.payload)) + self.payload


def try_decode(data) -> Tuple[Optional[Frame], int]:
    """Decode one frame from the start of data. Returns (frame, consumed) or (None, 0)."""
    if len(data) < HEADER_SIZE:
        return None, 0
    magic, _, ftype, ch, _, seq, length = _HDR.unpack_from(data, 0)
    if magic != MAGIC:
        raise FrameError(f"Bad Bragi magic 0x{magic:08x}")
    if length > MAX_PAYLOAD:
        raise FrameError(f"Bragi payload too large: {length}")
    if len(data) < HEADER_SIZE + length:
        return None, 0
    return Frame(ftype, ch, seq, bytes(data[HEADER_SIZE:HEADER_SIZE + length])), HEADER_SIZE + length


class FrameReader:
    """Buffered reader: feed() raw TCP bytes, get complete frames back (handles split and merged frames)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> List[Frame]:
        self._buf += data
        out: List[Frame] = []
        while True:
            frame, used = try_decode(self._buf)
            if frame is None:
                break
            del self._buf[:used]
            out.append(frame)
        return out

    @property
    def pending(self) -> int:
        return len(self._buf)


def chunk_image(key: int, jpeg: bytes, last_seq: int) -> List[Frame]:
    """Split a JPEG into image-report chunks. Non-last: type 0x80 seq 0; last: type 0xC0 with last_seq."""
    frames: List[Frame] = []
    offset = 0
    page = 0
    while True:
        part = jpeg[offset:offset + DATA_PER_CHUNK]
        n = len(part)
        last = offset + n >= len(jpeg)
        payload = bytearray(CHUNK_PAYLOAD)
        payload[0] = 0x02
        payload[1] = 0x07
        payload[2] = key
        payload[3] = 1 if last else 0
        struct.pack_into("<HH", payload, 4, n, page)
        payload[CHUNK_HEADER:CHUNK_HEADER + n] = part
        if last:
            frames.append(Frame(REQUEST, CH_IMAGE, last_seq, bytes(payload)))
        else:
            frames.append(Frame(NO_REPLY, CH_IMAGE, 0, bytes(payload)))
        offset += n
        page += 1
        if offset >= len(jpeg):
            break
    return frames


def reassemble(payloads) -> bytes:
    out = bytearray()
    for p in payloads:
        n = struct.unpack_from("<H", p, 4)[0]
        out += p[CHUNK_HEADER:CHUNK_HEADER + n]
    return bytes(out)


def parse_input_report(payload: bytes) -> Optional[List[bool]]:
    """Dock->PC 0x80: 01 00 20 00 + 32 key bytes (1 = down). None if not an input report."""
    if len(payload) < 4 + INPUT_KEYS or payload[:4] != b"\x01\x00\x20\x00":
        return None
    return [payload[4 + i] != 0 for i in range(INPUT_KEYS)]


def diff_keys(prev: List[bool], now: List[bool]) -> Iterator[Tuple[int, bool]]:
    for i in range(INPUT_KEYS):
        if prev[i] != now[i]:
            yield i, now[i]
