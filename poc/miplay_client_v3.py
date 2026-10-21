#!/usr/bin/env python3
"""MiPlay L15A v3: validated legacy bootstrap + reverse-media readiness probe.

This version deliberately stops before SetPlaySource/Open. It proves the
Mac can bind the reverse TCP/UDP endpoints required by the researched protocol,
then runs the already validated L15A legacy bootstrap and read-only queries.
No playback/media command is sent.
"""
import argparse
import hashlib
import hmac
import json
import socket
import struct
import sys
import threading
import time
from datetime import datetime

HOST = "192.168.3.4"
CONTROL_PORT = 8899
RTSP_PORT = 7274
TIMER_PORT = 36524
MAGIC = b"$"
VERSION = b"1.0.1123012\x00"

def log(s):
    print(f"[{datetime.now().astimezone().isoformat(timespec='milliseconds')}] {s}", flush=True)

def frame(cmd, seq, payload=b""):
    return MAGIC + struct.pack(">HHI", cmd, seq, len(payload)) + payload

def exact(sock, n):
    b = bytearray()
    while len(b) < n:
        p = sock.recv(n-len(b))
        if not p:
            raise EOFError("peer closed")
        b.extend(p)
    return bytes(b)

def recv_frame(sock):
    h = exact(sock, 9)
    if h[0:1] != MAGIC:
        raise ValueError(f"bad frame magic: {h[:9].hex()}")
    cmd, seq, n = struct.unpack(">HHI", h[1:])
    if n > 1024*1024:
        raise ValueError(f"refusing oversized frame {n}")
    return cmd, seq, exact(sock, n) if n else b""

def legacy_reply(challenge):
    key = hashlib.md5(b"0.0.0.0").hexdigest().encode()
    return hmac.new(key, challenge, hashlib.sha1).hexdigest().encode()

def desc(cmd, seq, p):
    names = {0x22:"NOTIFY",0x28:"CHALLENGE",0x29:"AUTH_ACK",0x1e:"GET_DEVICE_INFO",
             0x1f:"DEVICE_INFO_ACK",0x34:"GET_MIRROR_MODE",0x35:"MIRROR_MODE_ACK",
             0x36:"VERSION",0x37:"VERSION_ACK",0x58:"SET_DEVICE_INFO",0x59:"SET_DEVICE_INFO_ACK",
             0x0e:"GET_VOLUME",0x0f:"VOLUME_ACK",0x1c:"GET_STATE",0x1d:"STATE_ACK",0x14:"GET_MEDIA_INFO"}
    log(f"{'RX':2} 0x{cmd:04x} {names.get(cmd,'UNKNOWN')} seq={seq} len={len(p)}")
    if cmd == 0x37:
        log("    protocol_version=" + p.rstrip(b"\0").decode("ascii", "replace"))
    elif cmd == 0x35 or cmd == 0x0f or cmd == 0x1d:
        if len(p) == 5 and p[0] == 0:
            log(f"    scalar={int.from_bytes(p[1:5], 'big')}")
    elif cmd == 0x22 and p.startswith(b"\x04mode"):
        log(f"    mode={p[-1]}")
    elif cmd == 0x22 and p.startswith(b"\x05state"):
        log(f"    state={p[-1]}")
    elif cmd in (0x1f, 0x22, 0x58):
        log("    payload redacted")
    elif p and len(p) < 80 and all(0x20 <= x < 0x7f for x in p):
        log(f"    ascii={p.decode('ascii','replace')}")

class ReadinessServers:
    def __init__(self, bind_ip, tcp_port, udp_port):
        self.bind_ip, self.tcp_port, self.udp_port = bind_ip, tcp_port, udp_port
        self.stop = threading.Event()
        self.tcp = None
        self.udp = None
        self.threads = []

    def start(self):
        # Bind both sockets before any future Open could be considered.
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp.bind((self.bind_ip, self.tcp_port))
        self.tcp.listen(5)
        self.tcp.settimeout(0.5)

        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind((self.bind_ip, self.udp_port))
        self.udp.settimeout(0.5)

        self.threads = [
            threading.Thread(target=self._tcp_loop, daemon=True),
            threading.Thread(target=self._udp_loop, daemon=True),
        ]
        for t in self.threads: t.start()
        log(f"READY TCP listener bound {self.bind_ip}:{self.tcp_port} (diagnostic only; no RTSP replies)")
        log(f"READY UDP socket bound {self.bind_ip}:{self.udp_port} (diagnostic only; no timer replies)")
        log("IMPORTANT: endpoints only log incoming data; this build will NOT send Open.")

    def _tcp_loop(self):
        while not self.stop.is_set():
            try:
                c, peer = self.tcp.accept()
                log(f"INBOUND TCP {peer} -> {self.bind_ip}:{self.tcp_port}")
                c.settimeout(1)
                try:
                    data = c.recv(4096)
                    if data:
                        log(f"    first bytes ({len(data)}): {data[:96].hex(' ')}")
                        if data.startswith(b"OPTIONS") or b"RTSP/" in data[:128]:
                            log("    looks like RTSP; captured for diagnosis only, no response sent")
                except socket.timeout:
                    pass
                c.close()
            except socket.timeout:
                continue
            except OSError as e:
                if not self.stop.is_set(): log(f"TCP listener error: {e}")
                break

    def _udp_loop(self):
        while not self.stop.is_set():
            try:
                data, peer = self.udp.recvfrom(2048)
                log(f"INBOUND UDP {peer} len={len(data)} prefix={data[:64].hex(' ')}")
            except socket.timeout:
                continue
            except OSError as e:
                if not self.stop.is_set(): log(f"UDP listener error: {e}")
                break

    def close(self):
        self.stop.set()
        for s in (self.tcp, self.udp):
            if s:
                try: s.close()
                except OSError: pass

def run(args):
    if args.host != HOST or args.confirm_host != HOST or args.control_port != CONTROL_PORT:
        log(f"REFUSED: target must be exactly {HOST}:{CONTROL_PORT} and --confirm-host {HOST}")
        return 2
    servers = ReadinessServers(args.bind_ip, args.rtsp_port, args.timer_port)
    try:
        servers.start()
    except OSError as e:
        servers.close()
        log(f"Cannot establish media readiness: {e}")
        log("Do not proceed to Open until TCP/UDP ports can be bound.")
        return 1

    try:
        log(f"Connecting control TCP {args.host}:{args.control_port}")
        with socket.create_connection((args.host, args.control_port), timeout=4) as s:
            s.settimeout(3)
            cmd, seq, challenge = recv_frame(s)
            desc(cmd, seq, challenge)
            if cmd != 0x28 or len(challenge) not in (16,17) or not challenge.isdigit():
                raise ValueError("unexpected challenge; aborting without sending")
            s.sendall(frame(0x36, 0, VERSION))
            s.sendall(frame(0x29, seq, legacy_reply(challenge)))
            s.sendall(frame(0x1e, 1))
            log("TX 0x0036 version, 0x0029 challenge response, 0x001e device info")

            got_version = got_info = got_source_ack = False
            end = time.monotonic() + 6
            while time.monotonic() < end and not (got_version and got_info and got_source_ack):
                s.settimeout(max(0.1, min(1, end-time.monotonic())))
                try: c, q, p = recv_frame(s)
                except socket.timeout: continue
                except EOFError: break
                desc(c,q,p)
                got_version |= c == 0x37 and q == 0
                got_info |= c == 0x1f and q == 1
                if got_version and not getattr(run, "_sent_source", False):
                    s.sendall(frame(0x58, 2, json.dumps({"sourceName":"Mac mini"},separators=(",",":")).encode()))
                    log("TX 0x0058 seq=2 sourceName")
                    run._sent_source = True
                got_source_ack |= c == 0x59 and q == 2

            if not (got_version and got_info and got_source_ack):
                log("Bootstrap precondition missing; refusing further initialization.")
                return 1

            s.sendall(frame(0x58, 3, b'{"isSameAccount":0}'))
            s.sendall(frame(0x34, 4))
            s.sendall(frame(0x0e, 5))
            s.sendall(frame(0x1c, 6))
            s.sendall(frame(0x14, 7))
            log("TX account-context declaration and read-only mode/volume/state/media-info queries")
            end = time.monotonic() + args.observe
            while time.monotonic() < end:
                s.settimeout(max(0.1, min(1, end-time.monotonic())))
                try: c,q,p = recv_frame(s)
                except socket.timeout: continue
                except EOFError: break
                desc(c,q,p)
            log("Readiness probe finished. No SetPlaySource/Open/RTSP/media sent.")
            log("Next development boundary: implement and validate RTSP + UDP clock protocol before any Open.")
            return 0
    except (OSError, ValueError, EOFError) as e:
        log(f"Probe failed: {e}")
        return 1
    finally:
        servers.close()
        if hasattr(run, "_sent_source"): delattr(run, "_sent_source")

def main():
    p = argparse.ArgumentParser(description="L15A MiPlay v3 media readiness probe (no playback)")
    p.add_argument("--connect", action="store_true", help="run local protocol probe")
    p.add_argument("--host", default=HOST)
    p.add_argument("--control-port", type=int, default=CONTROL_PORT)
    p.add_argument("--confirm-host", default="")
    p.add_argument("--bind-ip", default="192.168.3.18")
    p.add_argument("--rtsp-port", type=int, default=RTSP_PORT)
    p.add_argument("--timer-port", type=int, default=TIMER_PORT)
    p.add_argument("--observe", type=float, default=4)
    a = p.parse_args()
    if not a.connect:
        log("DRY RUN; no sockets opened")
        log(f"would bind TCP {a.bind_ip}:{a.rtsp_port} and UDP {a.bind_ip}:{a.timer_port}")
        log(f"would connect {a.host}:{a.control_port} only after --connect --confirm-host {HOST}")
        log("This version will not send SetPlaySource/Open or reply to RTSP/timer packets.")
        return 0
    return run(a)

if __name__ == "__main__":
    sys.exit(main())
