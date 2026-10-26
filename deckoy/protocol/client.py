"""Asyncio client for the Network Dock's deck service (Bragi framing). Port of SDDock.Protocol.DockClient.

Only the commands in the verified protocol are ever written: get-feature 05/06/0b, set-feature
03 05 .. / 03 08 / 03 0d, image chunks, and the keepalive reply.
"""
from __future__ import annotations

import asyncio
import enum
import io
import struct
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional

from . import frame as fr
from .frame import Frame, FrameReader


class ConnectionState(str, enum.Enum):
    DISCONNECTED = "Disconnected"
    CONNECTING = "Connecting"
    CONNECTED = "Connected"


@dataclass
class DockInfo:
    firmware: str
    serial: str
    reply_0b: bytes = b""
    reply_05: bytes = b""   # raw handshake replies (the proxy re-serves them to the Elgato app)
    reply_06: bytes = b""


@dataclass(frozen=True)
class DockEndpoint:
    host: str
    port: int


class ProbeFailed(Exception):
    pass


def feature(*data: int) -> bytes:
    """32-byte request payload, zero padded."""
    return bytes(data).ljust(32, b"\x00")


def _cstr(p: bytes, offset: int) -> str:
    if offset >= len(p):
        return ""
    end = p.find(b"\x00", offset)
    if end < 0:
        end = len(p)
    return p[offset:end].decode("ascii", "replace")


def blank_jpeg(color=(0, 0, 0), quality: int = 80) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (96, 96), color).save(buf, "JPEG", quality=quality)
    return buf.getvalue()


EndpointProvider = Callable[[], Awaitable[Optional[DockEndpoint]]]


class DockClient:
    BACKOFF = (1.0, 2.0, 5.0, 10.0)

    def __init__(self) -> None:
        self.request_timeout = 3.0
        self.prime_before_connect = True
        self.probe_image: Optional[bytes] = None
        self.probe_timeout = 1.0
        self.prime_delay = 0.25
        self.probe_retry_delay = 0.5
        self.backoff = self.BACKOFF
        self.connect_timeout = 5.0

        self.state = ConnectionState.DISCONNECTED
        self.info: Optional[DockInfo] = None
        self.endpoint: Optional[DockEndpoint] = None
        self.brightness = 63
        self.sleep_seconds = 3600
        self.extra_features: List[bytes] = []   # 32-byte set-feature payloads replayed on every (re)connect
        self.probe_failures = 0

        # callbacks (called on the loop thread)
        self.on_key: Optional[Callable[[int, bool], None]] = None
        self.on_state: Optional[Callable[[ConnectionState], None]] = None
        self.on_connected: Optional[Callable[[DockInfo], None]] = None
        self.on_endpoint_result: Optional[Callable[[DockEndpoint, bool], None]] = None
        self.on_log: Optional[Callable[[str], None]] = None

        self._seq = 1970
        self._pending: Dict[int, asyncio.Future] = {}
        self._images: Dict[int, bytes] = {}
        self._writer: Optional[asyncio.StreamWriter] = None
        self._read_task: Optional[asyncio.Task] = None
        self._send_lock = asyncio.Lock()
        self._image_lock = asyncio.Lock()
        self._keys = [False] * fr.INPUT_KEYS
        self._supervisor: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ helpers
    def _say(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)

    def _set_state(self, s: ConnectionState) -> None:
        if self.state != s:
            self.state = s
            if self.on_state:
                self.on_state(s)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # ------------------------------------------------------------------ connection
    async def _open(self, host: str, port: int):
        return await asyncio.wait_for(asyncio.open_connection(host, port), self.connect_timeout)

    async def prime(self, host: str, port: int) -> None:
        """Connect, ask 05 and 06, close, wait. Without it the first real session never ACKed images."""
        self._say(f"Priming connection to {host}:{port} (05/06, then reconnect)")
        t0 = time.monotonic()
        reader, writer = await self._open(host, port)
        try:
            fread = FrameReader()
            for fid in (0x05, 0x06):
                seq = self._next_seq()
                writer.write(Frame(fr.REQUEST, fr.CH_GET_FEATURE, seq, feature(fid)).encode())
                await writer.drain()
                deadline = time.monotonic() + self.request_timeout
                done = False
                while not done:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise asyncio.TimeoutError(f"No reply from dock to priming request {fid:02x}")
                    data = await asyncio.wait_for(reader.read(65536), left)
                    if not data:
                        raise ConnectionError("Dock closed the priming connection")
                    done = any(f.type == fr.REPLY and f.seq == seq for f in fread.feed(data))
        finally:
            writer.close()
        self._say(f"Priming done in {int((time.monotonic() - t0) * 1000)} ms")
        await asyncio.sleep(self.prime_delay)

    async def connect(self, host: str, port: int) -> DockInfo:
        """Connect once and run the handshake. Raises on failure (state returns to Disconnected)."""
        await self._close()
        self._set_state(ConnectionState.CONNECTING)
        try:
            reader, writer = await self._open(host, port)
            self._writer = writer
            self._keys = [False] * fr.INPUT_KEYS
            self._read_task = asyncio.ensure_future(self._read_loop(reader))
            t0 = time.monotonic()
            self.info = await self._handshake()
            self._say(f"Handshake finished in {int((time.monotonic() - t0) * 1000)} ms")
            self.endpoint = DockEndpoint(host, port)
            self._set_state(ConnectionState.CONNECTED)
            return self.info
        except BaseException:
            await self._close()
            self._set_state(ConnectionState.DISCONNECTED)
            raise

    async def _handshake(self) -> DockInfo:
        r05 = await self._request(fr.CH_GET_FEATURE, feature(0x05))
        fw = _cstr(r05.payload, 6)
        r06 = await self._request(fr.CH_GET_FEATURE, feature(0x06))
        n = min(r06.payload[1], len(r06.payload) - 2) if len(r06.payload) > 1 else 0
        sn = r06.payload[2:2 + n].decode("ascii", "replace")
        r0b = await self._request(fr.CH_GET_FEATURE, feature(0x0B))
        self._say(f"0b reply (meaning unknown): {r0b.payload.hex()}")
        if not any(e[:2] == b"\x03\x05" for e in self.extra_features):
            await self._request(fr.CH_SET_FEATURE, feature(0x03, 0x05, 0x00, 0x00, 0x00, 0x02))  # replayed as captured
        for extra in self.extra_features:  # allow-listed set-features the proxy learned from the Elgato app
            await self._request(fr.CH_SET_FEATURE, extra)
        await self._send_brightness()
        await self._send_sleep()
        self._say(f"Handshake ok: fw {fw}, serial {sn}")
        return DockInfo(fw, sn, r0b.payload, r05.payload, r06.payload)

    async def _close(self) -> None:
        task, writer = self._read_task, self._writer
        self._read_task = self._writer = None
        if writer is not None:
            try:
                writer.transport.abort()
            except Exception:
                pass
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        self._fail_pending(ConnectionError("Connection closed"))

    def _fail_pending(self, exc: Exception) -> None:
        for seq, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    def _drop(self) -> None:
        """Abort the socket; the read loop ends and the supervisor reconnects."""
        if self._writer is not None:
            try:
                self._writer.transport.abort()
            except Exception:
                pass

    # ------------------------------------------------------------------ read loop
    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        fread = FrameReader()
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                for f in fread.feed(data):
                    await self._handle(f)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError, fr.FrameError) as ex:
            self._say(f"Read loop ended: {ex}")
        finally:
            self._fail_pending(ConnectionError("Connection lost"))
            if self._read_task is asyncio.current_task():
                self._set_state(ConnectionState.DISCONNECTED)

    async def _handle(self, f: Frame) -> None:
        if f.type in (fr.REPLY, fr.ACK):
            fut = self._pending.pop(f.seq, None)
            if fut is not None and not fut.done():
                fut.set_result(f)
            else:
                self._say(f"Unexpected reply seq {f.seq}")
        elif f.type == fr.NO_REPLY:
            keys = fr.parse_input_report(f.payload)
            if keys is None:
                self._say(f"Unknown 0x80 frame: {f.payload[:16].hex()}")
                return
            prev, self._keys = self._keys, keys
            for key, down in fr.diff_keys(prev, keys):
                if self.on_key:
                    try:
                        self.on_key(key, down)
                    except Exception as ex:  # a bad handler must not kill the connection
                        self._say(f"on_key handler failed: {ex!r}")
        elif f.type == fr.KEEPALIVE:
            if f.payload[:2] == b"\x01\x0a":
                reply = bytes([0x03, 0x1A]).ljust(1024, b"\x00")
                await self._send_raw(Frame(fr.KEEPALIVE, 0, 0, reply).encode())
            else:
                self._say(f"Unknown keepalive-type frame: {f.payload[:16].hex()}")
        else:
            self._say(f"Unknown frame type 0x{f.type:02x}")

    # ------------------------------------------------------------------ sending
    async def _send_raw(self, data: bytes) -> None:
        writer = self._writer
        if writer is None:
            raise ConnectionError("Not connected")
        async with self._send_lock:
            writer.write(data)
            await writer.drain()

    async def _send_and_wait(self, seq: int, wire: bytes, timeout: Optional[float] = None) -> Frame:
        fut = asyncio.get_running_loop().create_future()
        self._pending[seq] = fut
        try:
            await self._send_raw(wire)
            return await asyncio.wait_for(fut, timeout or self.request_timeout)
        except asyncio.TimeoutError:
            if self.state == ConnectionState.CONNECTED:
                self._say(f"No reply for seq {seq}; dropping the connection to reconnect")
                self._drop()
            raise
        finally:
            self._pending.pop(seq, None)

    async def _request(self, channel: int, payload: bytes) -> Frame:
        seq = self._next_seq()
        return await self._send_and_wait(seq, Frame(fr.REQUEST, channel, seq, payload).encode())

    def _brightness_payload(self) -> bytes:
        return feature(0x03, 0x08, self.brightness)

    async def _send_brightness(self) -> None:
        await self._request(fr.CH_SET_FEATURE, self._brightness_payload())

    async def _send_sleep(self) -> None:
        await self._request(fr.CH_SET_FEATURE, feature(0x03, 0x0D, *struct.pack("<H", self.sleep_seconds)))

    @property
    def is_connected(self) -> bool:
        return self.state == ConnectionState.CONNECTED and self._writer is not None

    async def set_brightness(self, percent: int) -> None:
        self.brightness = max(0, min(100, int(percent)))
        if self.is_connected:
            await self._send_brightness()

    async def send_set_feature(self, payload: bytes) -> None:
        """Remember an allow-listed set-feature (replayed on reconnect) and send it now when connected."""
        payload = bytes(payload).ljust(32, b"\x00")[:32]
        self.extra_features = [e for e in self.extra_features if e[:2] != payload[:2]] + [payload]
        if self.is_connected:
            await self._request(fr.CH_SET_FEATURE, payload)

    async def raw_set_feature(self, payload: bytes) -> None:
        """Send a set-feature once (no replay): used for unknown commands the proxy was told to forward."""
        if self.is_connected:
            await self._request(fr.CH_SET_FEATURE, bytes(payload))

    async def set_sleep(self, seconds: int) -> None:
        self.sleep_seconds = max(0, min(0xFFFF, int(seconds)))
        if self.is_connected:
            await self._send_sleep()

    async def set_key_image(self, key: int, jpeg: bytes) -> None:
        """Cache the JPEG and, when connected, send it (one image at a time, waits for the ACK)."""
        if not 0 <= key < fr.INPUT_KEYS:
            raise ValueError("key out of range")
        self._images[key] = jpeg
        if self.is_connected:
            await self._send_image(key)

    def forget_image(self, key: int) -> None:
        self._images.pop(key, None)

    def forget_all_images(self) -> None:
        self._images.clear()

    async def _send_image(self, key: int) -> None:
        async with self._image_lock:
            jpeg = self._images.get(key)  # another writer may have replaced it while we waited
            if jpeg is None:
                return
            seq = self._next_seq()
            wire = b"".join(f.encode() for f in fr.chunk_image(key, jpeg, seq))
            await self._send_and_wait(seq, wire)  # all chunks in one write under the send lock

    async def _resend_images(self) -> None:
        for key in sorted(self._images):
            await self._send_image(key)

    async def _probe(self) -> None:
        assert self.probe_image is not None
        t0 = time.monotonic()
        async with self._image_lock:
            seq = self._next_seq()
            wire = b"".join(f.encode() for f in fr.chunk_image(0, self.probe_image, seq))
            fut = asyncio.get_running_loop().create_future()
            self._pending[seq] = fut
            try:
                await self._send_raw(wire)
                await asyncio.wait_for(fut, self.probe_timeout)
                self._say(f"Session probe ok: key 0 blank ACKed in {int((time.monotonic() - t0) * 1000)} ms "
                          f"(probe failures so far: {self.probe_failures})")
            except asyncio.TimeoutError:
                self.probe_failures += 1
                self._say(f"Session probe failed (no ACK within {self.probe_timeout * 1000:.0f} ms), "
                          f"failure #{self.probe_failures}: closing and re-priming")
                raise ProbeFailed("Dock did not ACK the probe image")
            finally:
                self._pending.pop(seq, None)

    # ------------------------------------------------------------------ supervisor
    def start(self, endpoint: EndpointProvider) -> None:
        """Run forever: prime, connect, probe, re-push images, wait for a drop, back off, repeat."""
        self.stop()
        self._supervisor = asyncio.ensure_future(self._supervise(endpoint))

    def stop(self) -> None:
        if self._supervisor is not None:
            self._supervisor.cancel()
            self._supervisor = None

    async def aclose(self) -> None:
        sup, self._supervisor = self._supervisor, None
        if sup is not None:
            sup.cancel()
            try:
                await sup
            except BaseException:
                pass
        await self._close()
        self._set_state(ConnectionState.DISCONNECTED)

    async def _supervise(self, endpoint: EndpointProvider) -> None:
        attempt = 0
        try:
            while True:
                probe_failed = False
                reached = False
                ep: Optional[DockEndpoint] = None
                try:
                    ep = await endpoint()
                    if ep is None:
                        raise RuntimeError("No dock endpoint found")
                    if self.prime_before_connect:
                        await self.prime(ep.host, ep.port)
                    self._say(f"Connecting to {ep.host}:{ep.port}")
                    await self.connect(ep.host, ep.port)
                    if self.probe_image is not None:
                        await self._probe()
                    attempt = 0
                    await self._resend_images()
                    reached = True
                    if self.on_endpoint_result:
                        self.on_endpoint_result(ep, True)
                    if self.on_connected:
                        self.on_connected(self.info)
                    task = self._read_task
                    if task is not None:
                        await asyncio.shield(task)
                except asyncio.CancelledError:
                    raise
                except Exception as ex:
                    probe_failed = isinstance(ex, ProbeFailed)
                    hint = ""
                    if isinstance(ex, asyncio.TimeoutError):
                        hint = " (is the Elgato Stream Deck app or the Touch Portal plugin still running? Only one client can drive the dock.)"
                    self._say(f"Dock connection problem: {ex!r}{hint}")
                    if ep is not None and not reached and self.on_endpoint_result:
                        self.on_endpoint_result(ep, False)
                await self._close()
                self._set_state(ConnectionState.DISCONNECTED)
                if probe_failed:
                    delay = self.probe_retry_delay
                else:
                    delay = self.backoff[min(attempt, len(self.backoff) - 1)]
                    attempt += 1
                await asyncio.sleep(delay)
        finally:
            await self._close()
            self._set_state(ConnectionState.DISCONNECTED)


# ---------------------------------------------------------------------- CLI smoke test
def _key_image(key: int, quality: int = 85) -> bytes:
    """Upright key number (0 = top-left), rotated 180 degrees as the dock needs, as 96x96 JPEG."""
    from PIL import Image, ImageDraw, ImageFont
    row, col = divmod(key, 8)
    bg = (20, 60, 120) if (row + col) % 2 == 0 else (20, 100, 70)
    if key == 0:
        bg = (160, 40, 40)
    img = Image.new("RGB", (96, 96), bg)
    d = ImageDraw.Draw(img)
    try:
        big = ImageFont.truetype("arial.ttf", 44)
        small = ImageFont.truetype("arial.ttf", 16)
    except OSError:
        big = small = ImageFont.load_default()
    d.text((48, 40), str(key), font=big, fill="white", anchor="mm")
    d.text((48, 80), f"r{row} c{col}", font=small, fill=(220, 220, 220), anchor="mm")
    d.rectangle((0, 0, 95, 95), outline=(255, 255, 255))
    buf = io.BytesIO()
    img.rotate(180).save(buf, "JPEG", quality=quality)
    return buf.getvalue()


async def _smoke(host: str, port: int) -> None:
    from .discovery import DockLocator
    locator = DockLocator()
    client = DockClient()
    client.probe_image = blank_jpeg()
    client.on_log = lambda m: print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)
    client.on_state = lambda s: print(f"state: {s.value}", flush=True)
    client.on_key = lambda k, d: print(f"key {k} {'DOWN' if d else 'UP'}", flush=True)

    async def endpoint() -> Optional[DockEndpoint]:
        r = await locator.resolve(host, port)
        return DockEndpoint(*r) if r else None

    async def connected(info: DockInfo) -> None:
        print(f"CONNECTED fw={info.firmware} serial={info.serial} at {client.endpoint}", flush=True)
        for k in range(fr.INPUT_KEYS):
            await client.set_key_image(k, _key_image(k))
        print("Drew key numbers. Key 0 (red) should be top-left and upright. Press keys; Ctrl+C quits.", flush=True)

    client.on_connected = lambda info: asyncio.ensure_future(connected(info))
    client.on_endpoint_result = lambda ep, ok: (locator.mark_good if ok else locator.mark_failed)(ep.host, ep.port)
    client.start(endpoint)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await client.aclose()


def main(argv=None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Deckoy dock smoke test: connect, draw key numbers, print key presses")
    ap.add_argument("--host", default="", help="dock IP (blank = mDNS)")
    ap.add_argument("--port", type=int, default=0, help="blank/0 = auto (20002 then 20001)")
    a = ap.parse_args(argv)
    try:
        asyncio.run(_smoke(a.host, a.port))
    except KeyboardInterrupt:
        print("bye")


if __name__ == "__main__":
    main()
