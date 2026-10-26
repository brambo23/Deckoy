"""A fake Elgato Stream Deck app: talks to a deck service (the proxy, or MockDock) the way the real app does.

It can replay the app side of the decoded Elgato capture (tests/fixtures/session20001.txt): the 05/06/0b handshake,
the set-features, 500 images and keepalive replies, checking every request gets exactly one reply with its seq.
"""
from __future__ import annotations

import asyncio
import pathlib
from typing import Dict, List, Optional, Tuple

from deckoy.protocol import frame as fr
from deckoy.protocol.frame import Frame, FrameReader

FIXTURE = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "session20001.txt"


def load_capture(stream: str = "63") -> List[Tuple[bool, Frame]]:
    """(from_dock, frame) for one captured connection of the Elgato session, in capture order."""
    out = []
    for line in FIXTURE.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        f = line.split(" ")
        if f[0] != stream:
            continue
        out.append((f[1] == "DOCK", Frame(int(f[3], 16), int(f[4]), int(f[5]), bytes.fromhex(f[6]) if len(f) > 6 else b"")))
    return out


def expected_final_images(stream: str = "63") -> Dict[int, bytes]:
    """The last JPEG the Elgato app sent for every key in the capture."""
    pending: Dict[int, List[bytes]] = {}
    final: Dict[int, bytes] = {}
    for from_dock, f in load_capture(stream):
        if from_dock or f.channel != fr.CH_IMAGE or f.payload[:2] != b"\x02\x07":
            continue
        key = f.payload[2]
        pending.setdefault(key, []).append(f.payload)
        if f.payload[3] == 1:
            final[key] = fr.reassemble(pending.pop(key))
    return final


class FakeElgatoApp:
    def __init__(self, host: str, port: int, answer_keepalives: bool = True):
        self.host, self.port = host, port
        self.answer_keepalives = answer_keepalives
        self.seq = 1970
        self.inputs: List[List[bool]] = []
        self.keepalives = 0
        self.reply_log: List[Frame] = []
        self.closed = asyncio.Event()
        self._pending: Dict[int, asyncio.Future] = {}
        self._writer: Optional[asyncio.StreamWriter] = None
        self._task: Optional[asyncio.Task] = None

    async def connect(self) -> "FakeElgatoApp":
        reader, self._writer = await asyncio.open_connection(self.host, self.port)
        self._task = asyncio.ensure_future(self._read(reader))
        return self

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except BaseException:
                pass

    async def _read(self, reader) -> None:
        fread = FrameReader()
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                for f in fread.feed(data):
                    if f.type in (fr.REPLY, fr.ACK):
                        self.reply_log.append(f)
                        fut = self._pending.pop(f.seq, None)
                        if fut is not None and not fut.done():
                            fut.set_result(f)
                    elif f.type == fr.NO_REPLY:
                        keys = fr.parse_input_report(f.payload)
                        if keys is not None:
                            self.inputs.append(keys)
                    elif f.type == fr.KEEPALIVE and f.payload[:2] == b"\x01\x0a":
                        self.keepalives += 1
                        if self.answer_keepalives:
                            self._writer.write(Frame(fr.KEEPALIVE, 0, 0, bytes([3, 0x1A]).ljust(1024, b"\x00")).encode())
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("closed"))

    def _next(self) -> int:
        self.seq += 1
        return self.seq

    async def _send_wait(self, seq: int, wire: bytes, timeout: float) -> Frame:
        fut = asyncio.get_running_loop().create_future()
        self._pending[seq] = fut
        self._writer.write(wire)
        await self._writer.drain()
        return await asyncio.wait_for(fut, timeout)

    async def request(self, channel: int, payload: bytes, timeout: float = 5.0) -> Frame:
        seq = self._next()
        return await self._send_wait(seq, Frame(fr.REQUEST, channel, seq, payload).encode(), timeout)

    async def send_image(self, key: int, jpeg: bytes, timeout: float = 5.0) -> Frame:
        seq = self._next()
        wire = b"".join(f.encode() for f in fr.chunk_image(key, jpeg, seq))
        return await self._send_wait(seq, wire, timeout)

    async def replay_capture(self, stream: str = "63", timeout: float = 5.0) -> int:
        """Replays the app side of the captured connection. Returns the number of replies received."""
        n = 0
        pending: Dict[int, List[bytes]] = {}
        for from_dock, f in load_capture(stream):
            if from_dock or f.type == fr.KEEPALIVE:
                continue
            if f.channel == fr.CH_IMAGE:
                key = f.payload[2]
                pending.setdefault(key, []).append(f.payload)
                if f.payload[3] == 1:
                    await self.send_image(key, fr.reassemble(pending.pop(key)), timeout)
                    n += 1
            elif f.type == fr.REQUEST:
                await self.request(f.channel, f.payload, timeout)
                n += 1
        return n
