#!/usr/bin/env python3
"""Bounded Xiaomi MiPlay legacy bootstrap probe for macOS/Python 3.

Default: dry run only. With --connect and --confirm-host 192.168.3.4:
  * connect to the identified L15A TCP/8899
  * read and validate the server-first 0x0028 decimal challenge
  * send 0x0036 source version and same-sequence 0x0029 HMAC-SHA1 response
  * send one empty 0x001e GetDeviceInfo request
  * observe replies for a bounded window

Never sends OPEN, SET_PLAY_SOURCE, RTSP, media, or playback commands.
Algorithm/order are based on public S12/LX06 research; L15A 1.92.16
compatibility is unverified. Use only on your own device/network.
"""
from __future__ import annotations
import argparse
import hashlib
import hmac
import socket
import struct
import sys
import time
from datetime import datetime
from dataclasses import dataclass

PORT = 8899
MAGIC = 0x24
SOURCE_VERSION = b"1.0.1123012\x00"
LEGACY_HMAC_SEED = "0.0.0.0"
MAX_PAYLOAD = 1024 * 1024

@dataclass
class Frame:
    command: int
    sequence: int
    payload: bytes
    def encode(self) -> bytes:
        return bytes([MAGIC]) + struct.pack(">HHI", self.command, self.sequence, len(self.payload)) + self.payload

def log(msg: str) -> None:
    print(f"[{datetime.now().astimezone().isoformat(timespec='milliseconds')}] {msg}", flush=True)

def recv_exact(sock: socket.socket, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        part = sock.recv(count - len(data))
        if not part:
            raise EOFError(f"peer closed with {count - len(data)} bytes still expected")
        data.extend(part)
    return bytes(data)

def recv_frame(sock: socket.socket) -> Frame:
    header = recv_exact(sock, 9)
    if header[0] != MAGIC:
        raise ValueError(f"bad frame magic 0x{header[0]:02x}")
    command, sequence, length = struct.unpack(">HHI", header[1:9])
    if length > MAX_PAYLOAD:
        raise ValueError(f"refusing oversized payload: {length}")
    return Frame(command, sequence, recv_exact(sock, length) if length else b"")

def legacy_response(challenge: bytes) -> bytes:
    # Public research: HMAC-SHA1(key=ASCII hex MD5("0.0.0.0"), message=raw challenge).
    key = hashlib.md5(LEGACY_HMAC_SEED.encode("ascii")).hexdigest().encode("ascii")
    return hmac.new(key, challenge, hashlib.sha1).hexdigest().encode("ascii")

def cmd_name(command: int) -> str:
    return {
        0x0022: "NOTIFY", 0x0028: "DEVICE_ID/legacy challenge",
        0x0029: "AUTH_ACK", 0x0036: "NATIVE_VERSION",
        0x0037: "NATIVE_VERSION_ACK", 0x001E: "GET_DEVICE_INFO",
        0x001F: "GET_DEVICE_INFO_ACK", 0x0058: "SET_DEVICE_INFO",
        0x0059: "SET_DEVICE_INFO_ACK", 0x0034: "GET_MIRROR_MODE",
        0x0035: "GET_MIRROR_MODE_ACK", 0x0040: "SET_PLAY_SOURCE",
        0x0000: "OPEN",
    }.get(command, "UNKNOWN")

def describe(direction: str, frame: Frame) -> None:
    log(f"{direction} cmd=0x{frame.command:04x} ({cmd_name(frame.command)}) seq={frame.sequence} len={len(frame.payload)}")
    if not frame.payload:
        return
    if len(frame.payload) <= 80 and all(0x20 <= b < 0x7f for b in frame.payload):
        log(f"  payload_ascii={frame.payload.decode('ascii', errors='replace')!r}")
    else:
        log(f"  payload_prefix_hex={frame.payload[:32].hex(' ')}" + (" ..." if len(frame.payload) > 32 else ""))

def validate_challenge(frame: Frame) -> None:
    if frame.command != 0x0028:
        raise ValueError(f"expected first frame 0x0028, got 0x{frame.command:04x}")
    if len(frame.payload) not in (16, 17) or not frame.payload.isdigit():
        raise ValueError(f"unexpected 0x0028 payload: len={len(frame.payload)}, not 16/17 ASCII digits")

def planned_frames(challenge: Frame) -> list[Frame]:
    validate_challenge(challenge)
    return [Frame(0x0036, 0, SOURCE_VERSION),
            Frame(0x0029, challenge.sequence, legacy_response(challenge.payload)),
            Frame(0x001E, 1, b"")]

def dry_run() -> None:
    challenge = Frame(0x0028, 0x019E, b"5930784187532178")
    log("DRY RUN: no socket will be opened")
    describe("RX example", challenge)
    for frame in planned_frames(challenge):
        describe("TX planned", frame)
        log("  wire_hex=" + frame.encode().hex(" "))

def run_probe(host: str, port: int, confirm_host: str, timeout: float, observe: float) -> int:
    if confirm_host != host:
        log("REFUSED: --confirm-host must exactly match --host")
        return 2
    if host != "192.168.3.4" or port != PORT:
        log("REFUSED: this version is restricted to the identified L15A at 192.168.3.4:8899")
        return 2
    log(f"Connecting to {host}:{port}; bounded bootstrap only, no playback/media commands")
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            log(f"TCP connected; local endpoint={sock.getsockname()}")
            challenge = recv_frame(sock)
            describe("RX", challenge)
            validate_challenge(challenge)
            frames = planned_frames(challenge)
            first_write = frames[0].encode() + frames[1].encode()
            log(f"TX one write containing 0x0036 + 0x0029 ({len(first_write)} bytes)")
            sock.sendall(first_write)
            log("TX 0x001e GetDeviceInfo seq=1 (empty payload)")
            sock.sendall(frames[2].encode())
            deadline = time.monotonic() + observe
            while time.monotonic() < deadline:
                sock.settimeout(max(0.1, min(timeout, deadline - time.monotonic())))
                try:
                    frame = recv_frame(sock)
                except socket.timeout:
                    continue
                except EOFError as exc:
                    log(f"Peer closed: {exc}")
                    break
                describe("RX", frame)
                if frame.command in (0x0000, 0x0040):
                    log("WARNING: playback-related frame observed; probe will not reply")
            log("Probe complete; closing connection. No playback was requested.")
            return 0
    except (OSError, ValueError, EOFError) as exc:
        log(f"Probe failed: {exc}")
        return 1

def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded L15A MiPlay legacy bootstrap probe")
    parser.add_argument("--host", default="192.168.3.4")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--timeout", type=float, default=4.0)
    parser.add_argument("--observe", type=float, default=8.0)
    parser.add_argument("--connect", action="store_true", help="perform the bounded probe; default is dry-run")
    parser.add_argument("--confirm-host", default="", help="must exactly match --host to authorize network probe")
    args = parser.parse_args()
    if args.timeout <= 0 or args.observe <= 0:
        parser.error("--timeout and --observe must be positive")
    if not args.connect:
        dry_run()
        print("\nTo run the bounded local probe explicitly:\n  python3 miplay_client.py --connect --confirm-host 192.168.3.4")
        return 0
    return run_probe(args.host, args.port, args.confirm_host, args.timeout, args.observe)

if __name__ == "__main__":
    sys.exit(main())
