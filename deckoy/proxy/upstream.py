"""Upstream helpers: a latest-wins image sender for the deck service and a persistent client for the dock's 5343 service."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Dict, Optional

from ..protocol import frame as fr
from ..protocol.client import DockClient, DockEndpoint
from ..protocol.frame import Frame, FrameReader

log = logging.getLogger("deckoy.proxy")


class CoalescingSender:
    """Latest image per key wins. The Elgato app is ACKed immediately, so animated keys can't back up the dock link."""

    def __init__(self, client: DockClient):
        self.client = client
        self.pending: Dict[int, bytes] = {}
        self.sent = 0
        self.submitted = 0
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        t, self._task = self._task, None
        if t:
            t.cancel()
            try:
                await t
            except BaseException:
                pass

    def submit(self, key: int, jpeg: bytes) -> None:
        self.submitted += 1
        self.pending[key] = jpeg
        self._wake.set()

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self.pending:
                key = next(iter(self.pending))
                jpeg = self.pending.pop(key)
                try:
                    # Caches the image in the client; when the link is down it is re-sent after the reconnect.
                    await self.client.set_key_image(key, jpeg)
                    self.sent += 1
                except asyncio.CancelledError:
                    raise
                except Exception as ex:
                    log.info("upstream image for key %s not delivered now (%r); kept for the reconnect", key, ex)


class UpstreamDockService:
    """Deckoy's own persistent session to the real dock's 5343 service: fills the reply cache, answers keepalives."""

    IDS = (b"\x03\x83", b"\x03\x8f", b"\x03\x84", b"\x03\x87", b"\x03\x1c")
    REPLY_PREFIX = {b"\x03\x1c": b"\x01\x0b"}   # the descriptor reply does not echo the request id

    def __init__(self, endpoint: Callable[[], Awaitable[Optional[DockEndpoint]]], on_replies: Callable[[Dict[str, bytes]], None],
                 request_timeout: float = 3.0, backoff=(1.0, 2.0, 5.0, 10.0)):
        self.endpoint = endpoint
        self.on_replies = on_replies
        self.request_timeout = request_timeout
        self.backoff = backoff
        self.replies: Dict[str, bytes] = {}
        self.connected = False
        self.ready = asyncio.Event()
        self._writer: Optional[asyncio.StreamWriter] = None
        self._waiters: Dict[bytes, asyncio.Future] = {}
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._supervise())

    async def stop(self) -> None:
        t, self._task = self._task, None
        if t:
            t.cancel()
            try:
                await t
            except BaseException:
                pass

    def reply_prefix(self, rid: bytes) -> bytes:
        return self.REPLY_PREFIX.get(rid, rid)

    async def request(self, payload: bytes) -> Optional[bytes]:
        """Send a 1024-byte property request and wait for its reply (used for the 5343 ids and, if enabled, unknown ids)."""
        w = self._writer
        if w is None:
            return None
        rid = payload[:2]
        want = self.reply_prefix(rid)
        fut = asyncio.get_running_loop().create_future()
        self._waiters[want] = fut
        try:
            w.write(Frame(fr.KEEPALIVE, 0, 0, bytes(payload).ljust(1024, b"\x00")).encode())
            await w.drain()
            return await asyncio.wait_for(fut, self.request_timeout)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            return None
        finally:
            self._waiters.pop(want, None)

    async def _supervise(self) -> None:
        attempt = 0
        while True:
            try:
                ep = await self.endpoint()
                if ep is None:
                    raise RuntimeError("no dock endpoint for the 5343 service")
                reader, writer = await asyncio.wait_for(asyncio.open_connection(ep.host, ep.port), 5)
                self._writer = writer
                reader_task = asyncio.ensure_future(self._read(reader, writer))
                got = {}
                for rid in self.IDS:
                    rep = await self.request(rid)
                    if rep is None:
                        raise asyncio.TimeoutError(f"no reply from dock 5343 to {rid.hex()}")
                    got[rid.hex()] = rep
                self.replies.update(got)
                self.connected = True
                self.ready.set()
                attempt = 0
                self.on_replies(dict(self.replies))
                log.info("dock 5343 service session up (%s:%s)", ep.host, ep.port)
                await reader_task
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                log.info("dock 5343 session problem: %r", ex)
            finally:
                self.connected = False
                w, self._writer = self._writer, None
                if w is not None:
                    try:
                        w.transport.abort()
                    except Exception:
                        pass
            await asyncio.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
            attempt += 1

    async def _read(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        fread = FrameReader()
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                for f in fread.feed(data):
                    if f.type != fr.KEEPALIVE:
                        continue
                    if f.payload[:2] == b"\x01\x0a":     # keepalive from the dock
                        writer.write(Frame(fr.KEEPALIVE, 0, 0, bytes([0x03, 0x1A]).ljust(1024, b"\x00")).encode())
                        await writer.drain()
                        continue
                    fut = self._waiters.get(f.payload[:2])
                    if fut is not None and not fut.done():
                        fut.set_result(f.payload)
        except (ConnectionError, OSError, fr.FrameError):
            return
