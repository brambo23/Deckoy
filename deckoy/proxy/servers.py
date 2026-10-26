"""Downstream servers the Elgato app talks to: the deck service (images, brightness, input) and the dock service (5343).

Both bind to one address (default 127.0.0.1 only; never 0.0.0.0 unless the user asks for it in Settings).
The `core` object (see proxy.py) supplies cached replies and receives what the app sets.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from typing import List, Optional

from ..protocol import frame as fr
from ..protocol.frame import Frame, FrameReader

log = logging.getLogger("deckoy.proxy")

KEEPALIVE_FROM_DOCK = bytes([0x01, 0x0A, 0x02, 0x00, 0x01, 0x00]).ljust(512, b"\x00")
UNKNOWN_FORWARD_BUDGET = 4096     # bytes of unknown payload a connection may forward before it counts as a bulk transfer
FIRMWARE_WARNING = ("Firmware or unknown transfer blocked; to update firmware, quit Deckoy, "
                    "remove the firewall rule, and let the Elgato app connect directly.")
DECK_ALLOWED_SET = (b"\x03\x05", b"\x03\x08", b"\x03\x0d")
DECK_GET_IDS = (0x05, 0x06, 0x0B)
DOCK_IDS = (b"\x03\x83", b"\x03\x8f", b"\x03\x84", b"\x03\x87", b"\x03\x1c")
DESCRIPTOR_ID = b"\x03\x1c"
DESCRIPTOR_PORT_OFFSET = 126       # u16 LE deck port inside the 03 1c reply (verified in the pcap)


def norm_ip(ip: str) -> str:
    """Comparable form of an address: lower case, IPv6 scope id (fe80::1%12) and IPv4-mapped prefix removed."""
    ip = (ip or "").split("%")[0].lower()
    return ip[7:] if ip.startswith("::ffff:") and "." in ip else ip


def local_addresses() -> set:
    """Every IPv4 and IPv6 address of this PC (plus loopback), normalised. Connections from these are 'this PC'."""
    out = {"127.0.0.1", "::1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_UNSPEC):
            out.add(norm_ip(info[4][0]))
    except OSError:
        pass
    return out


class _Conn:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer
        self.lock = asyncio.Lock()
        self.opened = time.monotonic()
        self.pending: dict = {}          # key -> [chunk payloads]
        self.unknown_bytes = 0
        self.blocked = False
        self.last_keepalive_reply: Optional[float] = None
        self.closed = False
        self.label = ""
        self.dropped_by_us = False
        self.trace_left = 24         # the first frames of every connection are logged (diagnosing the Elgato app)
        self.rx = 0
        self.tx = 0

    def trace(self, direction: str, f: Frame) -> None:
        if self.trace_left > 0:
            self.trace_left -= 1
            log.info("%s %s type=0x%02x ch=%d seq=%d len=%d %s", self.label, direction, f.type, f.channel, f.seq,
                     len(f.payload), f.payload[:8].hex())

    async def send(self, f: Frame) -> None:
        if self.closed:
            return
        async with self.lock:
            try:
                self.writer.write(f.encode())
                await self.writer.drain()
                self.tx += 1
                self.trace("tx", f)
            except (ConnectionError, OSError):
                self.closed = True


class _BaseServer:
    def __init__(self, host: str, port: int, core, keepalive_interval: float = 4.0, name: str = "server",
                 allow_remote: bool = False, greet: bool = False):
        self.host, self.port, self.core = host, port, core
        # The real dock's 5343 service sends a keepalive the moment a client connects; the Elgato app's first (probe)
        # connection closes after ~15 ms if it sees nothing, then rejects the "dock" (observed at Checkpoint 1).
        self.greet = greet
        # Only this PC may actually connect: the source address must be one of this PC's own addresses. (The proxy
        # normally listens on all addresses because the Elgato app resolves our host name to every local address and
        # tries them in order, and connecting to one nothing listens on hangs for its whole 1 s budget.)
        self.allow_remote = allow_remote
        self._local_ips = {norm_ip(host)} | local_addresses()
        self.keepalive_interval = keepalive_interval
        self.name = name
        self.conns: List[_Conn] = []
        self._servers: List[asyncio.base_events.Server] = []
        self._tasks: set = set()
        self._stopping = False

    @property
    def active(self) -> Optional[_Conn]:
        """The newest connection is the one that gets input."""
        return self.conns[-1] if self.conns else None

    async def _listen(self, port: int) -> List[asyncio.base_events.Server]:
        """Listen on IPv4 and, when the host is 'all addresses', on IPv6 too.

        The Elgato app resolves this PC's name to its IPv6 link-local addresses FIRST and gives every address a 1 s
        connect budget; an IPv6 address nothing listens on hangs for the full second, so the app never reaches IPv4.
        """
        servers = [await asyncio.start_server(self._accept, self.host, port)]
        port = servers[0].sockets[0].getsockname()[1]
        if self.host == "0.0.0.0":
            try:
                s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                s6.bind(("::", port))
                s6.listen(100)
                s6.setblocking(False)
                servers.append(await asyncio.start_server(self._accept, sock=s6))
            except OSError as ex:
                log.warning("proxy %s service: IPv6 listener not available (%s); the Elgato app may time out on "
                            "IPv6 addresses first", self.name, ex)
        return servers

    async def start(self) -> None:
        self._servers = await self._listen(self.port)
        self.port = self._servers[0].sockets[0].getsockname()[1]
        log.info("proxy %s service listening on %s:%s", self.name, self.host, self.port)

    async def rebind(self, port: int) -> int:
        """Listen on a new port (existing connections are untouched). Returns the port actually used."""
        old = self._servers
        self._servers = await self._listen(port)
        self.port = self._servers[0].sockets[0].getsockname()[1]
        for s in old:
            s.close()
        log.info("proxy %s service now listening on %s:%s", self.name, self.host, self.port)
        return self.port

    async def drop_active(self) -> bool:
        """Test helper: abort the newest connection (as if the app's session had died)."""
        c = self.active
        if c is None:
            return False
        c.dropped_by_us = True
        try:
            c.writer.transport.abort()
        except Exception:
            return False
        return True

    async def stop(self) -> None:
        self._stopping = True
        for s in self._servers:
            s.close()
        for c in list(self.conns):
            c.closed = True
            try:
                c.writer.transport.abort()
            except Exception:
                pass
        for t in list(self._tasks):
            t.cancel()
        for s in self._servers:
            try:
                await asyncio.wait_for(s.wait_closed(), 1)
            except Exception:
                pass

    def peer_allowed(self, writer) -> bool:
        peer = writer.get_extra_info("peername")
        ip = norm_ip(peer[0]) if peer else ""
        if self.allow_remote or ip in self._local_ips:
            return True
        self._local_ips |= local_addresses()      # adapters come and go (VPNs, VMs): refresh once before refusing
        return ip in self._local_ips

    async def _accept(self, reader, writer) -> None:
        if not self.peer_allowed(writer):
            log.warning("proxy %s service: refused a connection from another host (%s)", self.name,
                        (writer.get_extra_info("peername") or ["?"])[0])
            writer.close()
            return
        conn = _Conn(reader, writer)
        peer = writer.get_extra_info("peername") or ("?", 0)
        self._count = getattr(self, "_count", 0) + 1
        conn.label = f"proxy {self.name} #{self._count} {peer[0]}:{peer[1]}"
        log.info("%s opened", conn.label)
        reason = "peer closed the connection"
        self.conns.append(conn)
        task = asyncio.current_task()
        self._tasks.add(task)
        ka = asyncio.ensure_future(self._keepalive(conn))
        self._tasks.add(ka)
        if self.name == "deck":      # only the deck service counts toward "the Elgato app is connected"
            self.core.stats.app_opened()
        try:
            if self.greet:
                await conn.send(Frame(fr.KEEPALIVE, 0, 0, KEEPALIVE_FROM_DOCK))
            fread = FrameReader()
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                for f in fread.feed(data):
                    conn.rx += 1
                    conn.trace("rx", f)
                    try:
                        await self._handle(conn, f)
                    except (asyncio.CancelledError, ConnectionError, OSError):
                        raise
                    except Exception:   # a bug in one handler must not drop the app's connection
                        log.exception("%s: error handling frame type=0x%02x", conn.label, f.type)
        except fr.FrameError as ex:
            reason = f"bad frame from the app ({ex})"
        except (ConnectionError, OSError) as ex:
            reason = f"connection error ({ex!r})"
        except asyncio.CancelledError:
            reason = "proxy stopping"
        finally:
            log.info("%s closed after %.3f s: %s (rx %d frames, tx %d frames)", conn.label,
                     time.monotonic() - conn.opened, reason, conn.rx, conn.tx)
            conn.closed = True
            ka.cancel()
            if conn in self.conns:
                self.conns.remove(conn)
            self._tasks.discard(task)
            self._tasks.discard(ka)
            if self.name == "deck":
                duration = time.monotonic() - conn.opened
                self.core.stats.app_closed(duration)
                if not self._stopping:
                    self.core.on_app_session_closed(duration, conn.label, conn.dropped_by_us)
            try:
                writer.close()
            except Exception:
                pass

    async def _keepalive(self, conn: _Conn) -> None:
        try:
            while True:
                await asyncio.sleep(self.keepalive_interval)
                await conn.send(Frame(fr.KEEPALIVE, 0, 0, KEEPALIVE_FROM_DOCK))
        except asyncio.CancelledError:
            pass

    async def _handle(self, conn: _Conn, f: Frame) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _note_unknown(self, conn: _Conn, what: str, nbytes: int) -> bool:
        """Records an unknown command. Returns True when it may be forwarded (policy On and still under the bulk budget)."""
        conn.unknown_bytes += nbytes
        policy = self.core.forward_unknown
        if policy and conn.unknown_bytes > UNKNOWN_FORWARD_BUDGET:
            if not conn.blocked:
                conn.blocked = True
                log.warning("blocked: %s", FIRMWARE_WARNING)
                self.core.warn(FIRMWARE_WARNING)
            self.core.stats.note_unknown(f"BLOCKED {what}", blocked=True)
            return False
        self.core.stats.note_unknown(what)
        log.warning("unknown command from the Elgato app (%s)%s: %s", self.name,
                    "; forwarding" if policy else "; not forwarded", what)
        return policy


class DeckServer(_BaseServer):
    """Acts as the deck service (port 20001/2): handshake, set-features, images, input reports."""

    def __init__(self, host, port, core, keepalive_interval: float = 4.0):
        super().__init__(host, port, core, keepalive_interval, "deck")

    async def _ack(self, conn: _Conn, seq: int) -> None:
        await conn.send(Frame(fr.ACK, 0, seq, bytes(4)))

    async def _handle(self, conn: _Conn, f: Frame) -> None:
        st = self.core.stats
        if f.type == fr.KEEPALIVE:
            if f.payload[:2] == b"\x03\x1a":
                conn.last_keepalive_reply = time.monotonic()
            else:
                self._note_unknown(conn, f"deck type00 {f.payload[:8].hex()}", 0)
            return
        if f.type not in (fr.REQUEST, fr.NO_REPLY):
            log.info("deck service: ignoring frame type 0x%02x from the app", f.type)
            return
        if f.channel == fr.CH_IMAGE:
            await self._image_chunk(conn, f)
        elif f.channel == fr.CH_GET_FEATURE and f.type == fr.REQUEST:
            fid = f.payload[0] if f.payload else 0
            if fid in DECK_GET_IDS:
                payload = await self.core.get_deck_reply(fid)
                st.local()
            else:
                self._note_unknown(conn, f"get-feature {f.payload[:8].hex()}", len(f.payload))
                payload = bytes([fid]).ljust(32, b"\x00")
            await conn.send(Frame(fr.REPLY, 0, f.seq, payload))
        elif f.channel == fr.CH_SET_FEATURE and f.type == fr.REQUEST:
            await self._set_feature(conn, f)
        else:
            self._note_unknown(conn, f"deck ch{f.channel} type 0x{f.type:02x} {f.payload[:8].hex()}", len(f.payload))
            if f.type == fr.REQUEST:
                await self._ack(conn, f.seq)

    async def _set_feature(self, conn: _Conn, f: Frame) -> None:
        p = f.payload.ljust(32, b"\x00")
        ident = p[:2]
        st = self.core.stats
        if ident == b"\x03\x08":
            self.core.set_brightness(p[2])
            st.local()
        elif ident == b"\x03\x0d":
            self.core.set_sleep(struct.unpack_from("<H", p, 2)[0])
            st.local()
        elif ident == b"\x03\x05":
            self.core.set_feature(p)
            st.local()
        else:
            if self._note_unknown(conn, f"set-feature {p[:12].hex()}", len(f.payload)):
                self.core.forward_set_feature(p)
                st.forward()
        await self._ack(conn, f.seq)

    async def _image_chunk(self, conn: _Conn, f: Frame) -> None:
        p = f.payload
        if len(p) < 8 or p[:2] != b"\x02\x07" or p[2] >= fr.INPUT_KEYS:
            self._note_unknown(conn, f"image-like chunk {p[:8].hex()}", len(p))
            if f.type == fr.REQUEST:
                await self._ack(conn, f.seq)
            return
        key = p[2]
        conn.pending.setdefault(key, []).append(p)
        if p[3] != 1:
            return
        jpeg = fr.reassemble(conn.pending.pop(key))
        if f.type == fr.REQUEST:
            await self._ack(conn, f.seq)          # ACK first: the app must never wait for the dock
        self.core.submit_image(key, jpeg)
        self.core.stats.local()

    async def send_keys(self, keys: List[bool]) -> None:
        """Input report to the newest connection (older ones are the app's own start-up connections)."""
        c = self.active
        if c is None:
            return
        payload = bytearray(512)
        payload[0], payload[2] = 0x01, 0x20
        for i, k in enumerate(keys[: fr.INPUT_KEYS]):
            payload[4 + i] = 1 if k else 0
        await c.send(Frame(fr.NO_REPLY, 0, 0, bytes(payload)))


class DockServer(_BaseServer):
    """Acts as the dock service (port 5343): property requests answered from the cache, keepalives."""

    def __init__(self, host, port, core, keepalive_interval: float = 4.0):
        super().__init__(host, port, core, keepalive_interval, "dock", greet=True)

    async def _handle(self, conn: _Conn, f: Frame) -> None:
        if f.type != fr.KEEPALIVE:
            log.info("dock service: ignoring frame type 0x%02x from the app", f.type)
            return
        ident = f.payload[:2]
        if ident == b"\x03\x1a":
            conn.last_keepalive_reply = time.monotonic()
            return
        if ident in DOCK_IDS:
            reply = await self.core.get_dock_reply(ident)
            self.core.stats.local()
            if reply is not None:
                await conn.send(Frame(fr.KEEPALIVE, 0, 0, reply))
            return
        if self._note_unknown(conn, f"dock request {f.payload[:12].hex()}", len(f.payload)):
            reply = await self.core.forward_dock_request(f.payload)
            self.core.stats.forward()
            if reply is not None:
                await conn.send(Frame(fr.KEEPALIVE, 0, 0, reply))


def rewrite_descriptor_port(reply: bytes, port: int) -> bytes:
    """The 03 1c descriptor carries the deck's TCP port; point it at the proxy's deck server."""
    b = bytearray(reply)
    if len(b) >= DESCRIPTOR_PORT_OFFSET + 2:
        struct.pack_into("<H", b, DESCRIPTOR_PORT_OFFSET, port)
    return bytes(b)
