"""Python port of MockDockServer: behaves like the dock's deck service (port 20001/20002, Bragi framing).

Replies as seen in the pcap: fw 1.01.000, serial, `0b 02`, ACKs for set-features and completed images,
periodic keepalive, scripted key presses. `silent_connections=N` simulates the real dock's "deaf first
session": the handshake works but images are never ACKed.

Run directly:  python -m tools.mock_dock [--port 20002]   (type a key number + Enter to press/release it)
"""
from __future__ import annotations

import asyncio
import sys
from typing import Dict, List, Tuple

from deckoy.protocol import frame as fr
from deckoy.protocol.frame import Frame, FrameReader


class _Conn:
    def __init__(self, reader, writer, silent):
        self.reader, self.writer, self.silent = reader, writer, silent
        self.lock = asyncio.Lock()


class MockDock:
    def __init__(self, port: int = 0, keepalive_interval: float = 4.0, silent_connections: int = 0,
                 serial: str = "TESTDECK0001", firmware: str = "1.01.000", image_delay: float = 0.0):
        self.port = port
        self.image_delay = image_delay    # simulates a slow dock: seconds before each completed image is ACKed
        self.keepalive_interval = keepalive_interval
        self.silent_connections = silent_connections
        self.serial, self.firmware = serial, firmware
        self.requests: List[Frame] = []
        self.images: Dict[int, bytes] = {}
        self.image_log: List[Tuple[int, bytes]] = []
        self.keepalive_replies = 0
        self.brightness = -1
        self.sleep_seconds = -1
        self.connections_accepted = 0
        self.violations: List[str] = []
        self._keys = [False] * fr.INPUT_KEYS
        self._clients: List[_Conn] = []
        self._server = None
        self._tasks: set = set()

    async def start(self) -> "MockDock":
        self._server = await asyncio.start_server(self._on_client, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        self.drop_clients()
        for t in list(self._tasks):
            t.cancel()
        if self._server:
            try:
                await asyncio.wait_for(self._server.wait_closed(), 1)
            except Exception:
                pass

    def drop_clients(self) -> None:
        """Simulate pulling the cable."""
        for c in list(self._clients):
            try:
                c.writer.transport.abort()
            except Exception:
                pass

    async def _send(self, c: _Conn, f: Frame) -> None:
        async with c.lock:
            c.writer.write(f.encode())
            await c.writer.drain()

    async def _on_client(self, reader, writer) -> None:
        self.connections_accepted += 1
        c = _Conn(reader, writer, self.connections_accepted <= self.silent_connections)
        self._clients.append(c)
        task = asyncio.current_task()
        self._tasks.add(task)
        ka = asyncio.ensure_future(self._keepalive(c))
        self._tasks.add(ka)
        try:
            await self._serve(c)
        except (asyncio.CancelledError, ConnectionError, fr.FrameError):
            pass
        finally:
            ka.cancel()
            if c in self._clients:
                self._clients.remove(c)
            self._tasks.discard(task)
            self._tasks.discard(ka)
            try:
                writer.close()
            except Exception:
                pass

    async def _keepalive(self, c: _Conn) -> None:
        try:
            while True:
                await asyncio.sleep(self.keepalive_interval)
                p = bytearray(512)
                p[0], p[1], p[2], p[4] = 0x01, 0x0A, 0x02, 0x01
                await self._send(c, Frame(fr.KEEPALIVE, 0, 0, bytes(p)))
        except (asyncio.CancelledError, ConnectionError):
            pass

    async def _serve(self, c: _Conn) -> None:
        fread = FrameReader()
        pending: Dict[int, List[bytes]] = {}
        while True:
            data = await c.reader.read(65536)
            if not data:
                return
            for f in fread.feed(data):
                if f.type == fr.KEEPALIVE:
                    if f.payload[:2] == b"\x03\x1a":
                        self.keepalive_replies += 1
                elif f.type in (fr.NO_REPLY, fr.REQUEST):
                    if f.type == fr.REQUEST:
                        self.requests.append(f)
                    await self._handle(c, f, pending)
                else:
                    self.violations.append(f"unexpected type 0x{f.type:02x}")

    async def _handle(self, c: _Conn, f: Frame, pending: Dict[int, List[bytes]]) -> None:
        if f.channel == fr.CH_IMAGE:
            if len(f.payload) != 1024 or f.payload[:2] != b"\x02\x07":
                self.violations.append("bad image chunk header")
                return
            key = f.payload[2]
            pending.setdefault(key, []).append(f.payload)
            if f.payload[3] == 1:
                jpeg = fr.reassemble(pending.pop(key))
                self.images[key] = jpeg
                self.image_log.append((key, jpeg))
                if self.image_delay:
                    await asyncio.sleep(self.image_delay)
                if not c.silent:
                    await self._ack(c, f)
            elif f.type == fr.REQUEST:
                self.violations.append("request on non-last chunk")
            return
        if f.type != fr.REQUEST:
            return
        if f.channel == fr.CH_GET_FEATURE:
            p = bytearray(32)
            fid = f.payload[0]
            if fid == 0x05:
                p[0], p[1] = 0x05, 0x0C
                p[2:6] = bytes([0x84, 0x36, 0xE3, 0x9A])
                fw = self.firmware.encode()
                p[6:6 + len(fw)] = fw
            elif fid == 0x06:
                sn = self.serial.encode()
                p[0], p[1] = 0x06, len(sn)
                p[2:2 + len(sn)] = sn
            elif fid == 0x0B:
                p[0], p[1] = 0x0B, 0x02
            else:
                self.violations.append(f"unknown get-feature 0x{fid:02x}")
            await self._send(c, Frame(fr.REPLY, 0, f.seq, bytes(p)))
        elif f.channel == fr.CH_SET_FEATURE:
            if f.payload[:2] == b"\x03\x08":
                self.brightness = f.payload[2]
            elif f.payload[:2] == b"\x03\x0d":
                self.sleep_seconds = f.payload[2] | f.payload[3] << 8
            elif f.payload[:2] != b"\x03\x05":
                self.violations.append(f"unexpected set-feature {f.payload[:4].hex()}")
            await self._ack(c, f)

    async def _ack(self, c: _Conn, f: Frame) -> None:
        await self._send(c, Frame(fr.ACK, 0, f.seq, bytes(4)))

    async def inject_key(self, key: int, down: bool) -> None:
        self._keys[key] = down
        p = bytearray(512)
        p[0], p[2] = 0x01, 0x20
        for i, k in enumerate(self._keys):
            p[4 + i] = 1 if k else 0
        for c in list(self._clients):
            await self._send(c, Frame(fr.NO_REPLY, 0, 0, bytes(p)))


class MockDockService:
    """The dock's own service (port 5343): property replies recorded from the real dock, keepalive every few seconds."""

    def __init__(self, port: int = 0, keepalive_interval: float = 4.0, fixture: str = ""):
        import json
        import pathlib
        path = fixture or str(pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "dock5343.json")
        data = json.load(open(path))
        self.replies = {bytes.fromhex(k): bytes.fromhex(v) for k, v in data["replies_by_request_id"].items()}
        self.keepalive = bytes.fromhex(data["keepalive_from_dock"])
        self.port = port
        self.keepalive_interval = keepalive_interval
        self.requests: List[bytes] = []
        self.keepalive_replies = 0
        self.connections_accepted = 0
        self._server = None
        self._clients: List = []
        self._tasks: set = set()

    async def start(self) -> "MockDockService":
        self._server = await asyncio.start_server(self._on_client, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        self.drop_clients()
        for t in list(self._tasks):
            t.cancel()

    def drop_clients(self) -> None:
        for w in list(self._clients):
            try:
                w.transport.abort()
            except Exception:
                pass

    async def _on_client(self, reader, writer) -> None:
        self.connections_accepted += 1
        self._clients.append(writer)
        task = asyncio.current_task()
        self._tasks.add(task)
        ka = asyncio.ensure_future(self._ka(writer))
        self._tasks.add(ka)
        fread = FrameReader()
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                for f in fread.feed(data):
                    if f.type != fr.KEEPALIVE:
                        continue
                    if f.payload[:2] == b"":
                        self.keepalive_replies += 1
                    else:
                        self.requests.append(f.payload[:2])
                        rep = self.replies.get(f.payload[:2])
                        if rep is not None:
                            writer.write(Frame(fr.KEEPALIVE, 0, 0, rep).encode())
                            await writer.drain()
        except (asyncio.CancelledError, ConnectionError, fr.FrameError):
            pass
        finally:
            ka.cancel()
            if writer in self._clients:
                self._clients.remove(writer)
            self._tasks.discard(task)
            self._tasks.discard(ka)

    async def _ka(self, writer) -> None:
        try:
            while True:
                await asyncio.sleep(self.keepalive_interval)
                writer.write(Frame(fr.KEEPALIVE, 0, 0, self.keepalive).encode())
                await writer.drain()
        except (asyncio.CancelledError, ConnectionError):
            pass


async def _main(port: int) -> None:
    mock = await MockDock(port).start()
    print(f"MockDock listening on 127.0.0.1:{mock.port}. Type a key number (0-31) + Enter to tap it.")
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            await asyncio.sleep(3600)  # stdin closed: keep serving
            continue
        if line.strip().isdigit() and int(line) < fr.INPUT_KEYS:
            k = int(line)
            await mock.inject_key(k, True)
            await asyncio.sleep(0.1)
            await mock.inject_key(k, False)
    await mock.stop()


if __name__ == "__main__":
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 20002
    asyncio.run(_main(port))
