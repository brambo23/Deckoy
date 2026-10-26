"""Decode Bragi frames from a pcap/pcapng of an Elgato-app <-> Network Dock session using tshark (read-only).

Usage:  python -m tools.pcap_decode "<capture.pcapng>" --dock <dock ip> [--summary] [--allowlist out.json]

Output: per TCP connection (stream) to the dock: start/end, who sent FIN/RST first, port, frame counts; and a
request-id table per port. Frames are parsed with deckoy.protocol.frame (same code the client uses).
"""
from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from deckoy.protocol import frame as fr
from deckoy.protocol.frame import Frame, FrameError, FrameReader

TSHARK = r"C:\Program Files\Wireshark\tshark.exe"
DOCK_PORTS = (5343, 20001, 20002, 20003)


@dataclass
class Conn:
    stream: int
    port: int = 0
    client_port: int = 0
    first: float = 0.0
    last: float = 0.0
    syn: Optional[float] = None
    closes: List[Tuple[float, str, str]] = field(default_factory=list)  # (time, "pc"|"dock", "FIN"|"RST")
    frames: List[Tuple[float, str, Frame]] = field(default_factory=list)  # (time, "pc"|"dock", frame)
    bytes_pc: int = 0
    bytes_dock: int = 0
    errors: List[str] = field(default_factory=list)


def _tshark(args: List[str]) -> List[List[str]]:
    p = subprocess.run([TSHARK] + args, capture_output=True, text=True, check=True)
    return [ln.split("\t") for ln in p.stdout.splitlines() if ln]


def load(pcap: str, dock: str, ports=DOCK_PORTS) -> Dict[int, Conn]:
    pf = " || ".join(f"tcp.port=={p}" for p in ports)
    base = f"ip.addr=={dock} && ({pf})"
    conns: Dict[int, Conn] = {}
    # 1) connection-level events (SYN, FIN, RST)
    rows = _tshark(["-r", pcap, "-Y", f"{base} && (tcp.flags.syn==1 || tcp.flags.fin==1 || tcp.flags.reset==1)",
                    "-T", "fields", "-e", "tcp.stream", "-e", "frame.time_epoch", "-e", "ip.src", "-e", "tcp.srcport",
                    "-e", "tcp.dstport", "-e", "tcp.flags.syn", "-e", "tcp.flags.ack", "-e", "tcp.flags.fin",
                    "-e", "tcp.flags.reset"])
    for s, t, src, sp, dp, syn, ack, fin, rst in rows:
        c = conns.setdefault(int(s), Conn(int(s)))
        t = float(t)
        from_dock = src == dock
        c.port = int(sp) if from_dock else int(dp)
        c.client_port = int(dp) if from_dock else int(sp)
        if syn == "True" and ack == "False":
            c.syn = t
        if rst == "True":
            c.closes.append((t, "dock" if from_dock else "pc", "RST"))
        elif fin == "True":
            c.closes.append((t, "dock" if from_dock else "pc", "FIN"))
    # 2) payload bytes, reassembled per direction in capture order (retransmissions skipped)
    rows = _tshark(["-r", pcap, "-Y", f"{base} && tcp.len>0 && !tcp.analysis.retransmission", "-T", "fields",
                    "-e", "tcp.stream", "-e", "frame.time_epoch", "-e", "ip.src", "-e", "tcp.payload"])
    readers: Dict[Tuple[int, str], FrameReader] = {}
    for s, t, src, payload in rows:
        c = conns.setdefault(int(s), Conn(int(s)))
        t = float(t)
        side = "dock" if src == dock else "pc"
        c.first = c.first or t
        c.last = t
        data = bytes.fromhex(payload.replace(":", ""))
        if side == "dock":
            c.bytes_dock += len(data)
        else:
            c.bytes_pc += len(data)
        rd = readers.setdefault((int(s), side), FrameReader())
        try:
            for f in rd.feed(data):
                c.frames.append((t, side, f))
        except FrameError as ex:
            c.errors.append(f"{side}: {ex}")
    for c in conns.values():
        if not c.port:  # no SYN/FIN rows (capture started mid-connection)
            pass
        c.closes.sort()
    return dict(sorted(conns.items()))


def request_key(port: int, f: Frame) -> str:
    """Stable id for a PC->dock request: channel + the leading command bytes (not the variable payload)."""
    p = f.payload
    if f.type == fr.KEEPALIVE:  # also the property traffic on port 5343
        return f"type00 {p[:2].hex(' ')}"
    if f.channel == fr.CH_IMAGE:
        return "ch0 image-chunk 02 07"
    return f"ch{f.channel} {p[:1].hex()}" if f.channel == fr.CH_GET_FEATURE else f"ch{f.channel} {p[:2].hex(' ')}"


def summarize(conns: Dict[int, Conn], t0: Optional[float] = None) -> str:
    out: List[str] = []
    t0 = t0 if t0 is not None else min((c.syn or c.first) for c in conns.values() if (c.syn or c.first))
    for c in conns.values():
        n = collections.Counter((side, f.type) for _, side, f in c.frames)
        closes = ", ".join(f"{who} {kind} @{t - t0:.3f}s" for t, who, kind in c.closes) or "none seen"
        out.append(f"stream {c.stream} port {c.port} (client port {c.client_port}) "
                   f"{(c.syn or c.first) - t0:8.3f}s..{c.last - t0:8.3f}s  pc->dock {c.bytes_pc}B dock->pc {c.bytes_dock}B  "
                   f"frames { {f'{s}:0x{t:02x}': v for (s, t), v in sorted(n.items())} }  closes: {closes}"
                   + (f"  ERRORS {c.errors}" if c.errors else ""))
    return "\n".join(out)


def build_allowlist(conns: Dict[int, Conn]) -> Dict[str, Dict[str, dict]]:
    """port -> request-key -> {count, example payload prefix (hex), reply types seen, reply example}."""
    table: Dict[str, Dict[str, dict]] = collections.defaultdict(dict)
    for c in conns.values():
        replies: Dict[int, Frame] = {f.seq: f for _, side, f in c.frames if side == "dock" and f.type in (fr.REPLY, fr.ACK)}
        for _, side, f in c.frames:
            if side != "pc":
                continue
            key = request_key(c.port, f)
            e = table[str(c.port)].setdefault(key, {"count": 0, "payload_lens": set(), "example": f.payload[:16].hex(),
                                                    "types": set(), "reply_types": set(), "reply_example": ""})
            e["count"] += 1
            e["payload_lens"].add(len(f.payload))
            e["types"].add(f"0x{f.type:02x}")
            r = replies.get(f.seq) if f.type == fr.REQUEST else None
            if r is not None:
                e["reply_types"].add(f"0x{r.type:02x}")
                e["reply_example"] = e["reply_example"] or r.payload[:24].hex()
    for tbl in table.values():
        for e in tbl.values():
            for k in ("payload_lens", "types", "reply_types"):
                e[k] = sorted(e[k])
    return table


def diagnose_conn(c: Conn, end_time: Optional[float] = None, keepalive_reply_window: float = 2.0) -> dict:
    """Pure analysis of one connection (testable without a pcap). Times are epoch seconds.

    Answers: who closed first, which requests never got a reply, whether the dock's keepalives (01 0a) were
    answered by the PC (03 1a) and whether the dock stopped sending them, and how long each side was silent
    before the close (or the end of the capture).
    """
    end = end_time if end_time is not None else c.last
    d: dict = {"stream": c.stream, "port": c.port, "first_closer": None, "closes": list(c.closes)}
    if c.closes:
        t, who, kind = c.closes[0]
        d["first_closer"] = f"{who} {kind}"
        end = t
    replies = {f.seq: t for t, side, f in c.frames if side == "dock" and f.type in (fr.REPLY, fr.ACK)}
    unanswered = [(t, f.seq, request_key(c.port, f)) for t, side, f in c.frames
                  if side == "pc" and f.type == fr.REQUEST and f.seq not in replies]
    d["unanswered_requests"] = [{"t": t, "seq": s, "what": w, "waited_s": round(end - t, 3)} for t, s, w in unanswered]
    lat = sorted((replies[f.seq] - t) for t, side, f in c.frames
                 if side == "pc" and f.type == fr.REQUEST and f.seq in replies)
    d["max_reply_latency_s"] = round(lat[-1], 3) if lat else None
    ka_dock = [t for t, side, f in c.frames if side == "dock" and f.type == fr.KEEPALIVE and f.payload[:2] == b"\x01\x0a"]
    ka_pc = [t for t, side, f in c.frames if side == "pc" and f.type == fr.KEEPALIVE and f.payload[:2] == b"\x03\x1a"]
    # Only keepalives that had the full reply window before the end/close count (a priming connection that
    # closes right after the dock's greeting keepalive is normal).
    unanswered_ka = [t for t in ka_dock if t <= end - keepalive_reply_window
                     and not any(0 <= r - t <= keepalive_reply_window for r in ka_pc)]
    d["dock_keepalives"] = len(ka_dock)
    d["pc_keepalive_replies"] = len(ka_pc)
    d["dock_keepalives_unanswered_by_pc"] = unanswered_ka
    d["last_dock_keepalive"] = ka_dock[-1] if ka_dock else None
    d["dock_keepalive_stopped_before_end_s"] = round(end - ka_dock[-1], 3) if ka_dock else None
    last_dock = max((t for t, side, f in c.frames if side == "dock"), default=None)
    last_pc = max((t for t, side, f in c.frames if side == "pc"), default=None)
    d["dock_silent_for_s"] = round(end - last_dock, 3) if last_dock else None
    d["pc_silent_for_s"] = round(end - last_pc, 3) if last_pc else None
    verdict: List[str] = []
    if d["unanswered_requests"]:
        w = d["unanswered_requests"][0]
        verdict.append(f"UNANSWERED REQUEST: {w['what']} seq {w['seq']} got no reply for {w['waited_s']} s before the end "
                       "(dock stopped answering, or the packet never arrived)")
    if unanswered_ka:
        verdict.append(f"PC did not answer {len(unanswered_ka)} dock keepalive(s) within {keepalive_reply_window} s "
                       "(the app stopped answering keepalives)")
    if ka_dock and d["dock_keepalive_stopped_before_end_s"] and d["dock_keepalive_stopped_before_end_s"] > 6:
        verdict.append(f"DOCK stopped sending keepalives {d['dock_keepalive_stopped_before_end_s']} s before the end "
                       "(dock or network went silent)")
    if d["first_closer"]:
        verdict.append(f"First close: {d['first_closer']}")
    if not verdict:
        verdict.append("nothing abnormal in this connection")
    d["verdict"] = verdict
    return d


def tcp_flags(pcap: str, dock: str, ports=DOCK_PORTS) -> Dict[int, Dict[str, int]]:
    """Per-stream counts of tshark TCP analysis flags (retransmission, zero window, ...). Needs tshark."""
    pf = " || ".join(f"tcp.port=={p}" for p in ports)
    rows = _tshark(["-r", pcap, "-Y", f"ip.addr=={dock} && ({pf}) && tcp.analysis.flags", "-T", "fields",
                    "-e", "tcp.stream", "-e", "_ws.expert.message"])
    out: Dict[int, Dict[str, int]] = collections.defaultdict(lambda: collections.Counter())
    for r in rows:
        out[int(r[0])][r[1] if len(r) > 1 else "?"] += 1
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--diagnose", action="store_true", help="print the per-connection drop diagnosis")
    ap.add_argument("pcap")
    ap.add_argument("--dock", required=True, help="the dock's IP address")
    ap.add_argument("--allowlist", help="write the request-id table (JSON) here")
    ap.add_argument("--ports", default=",".join(map(str, DOCK_PORTS)))
    a = ap.parse_args(argv)
    conns = load(a.pcap, a.dock, tuple(int(p) for p in a.ports.split(",")))
    print(summarize(conns))
    if a.diagnose:
        flags = tcp_flags(a.pcap, a.dock, tuple(int(p) for p in a.ports.split(",")))
        last = max(c.last for c in conns.values())
        print("\n=== diagnosis (per connection) ===")
        for c in conns.values():
            d = diagnose_conn(c, end_time=last if not c.closes else None)
            print(f"\nstream {c.stream} port {c.port}: closes={[(round(t, 3), w, k) for t, w, k in c.closes]}")
            print(f"  dock keepalives {d['dock_keepalives']}, PC replies {d['pc_keepalive_replies']}, "
                  f"dock silent {d['dock_silent_for_s']} s, PC silent {d['pc_silent_for_s']} s, "
                  f"max reply latency {d['max_reply_latency_s']} s, tcp flags {dict(flags.get(c.stream, {}))}")
            for v in d["verdict"]:
                print("  *", v)
    table = build_allowlist(conns)
    for port, tbl in table.items():
        print(f"\nport {port} PC->dock request ids:")
        for k, e in sorted(tbl.items()):
            print(f"  {k:28} x{e['count']:<5} lens={e['payload_lens']} types={e['types']} replies={e['reply_types']} "
                  f"ex={e['example']}")
    if a.allowlist:
        with open(a.allowlist, "w") as f:
            json.dump(table, f, indent=2)


if __name__ == "__main__":
    main()
