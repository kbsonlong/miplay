"""LAN auto-discovery of MiPlay speakers (L15A) on TCP/8899.

Strategy: concurrent TCP connects across the local /24 segment; a genuine
L15A answers a fresh control connection with a MiPlay frame 0x0028 whose
payload is a 16-17 digit decimal challenge (the exact format the V14
bootstrap validates). Anything else answering on 8899 (e.g. a local
``miplay-br`` bridge that returns a 14-digit challenge with a static seq)
is reported but NOT classified as a speaker. The machine's own IPs are
skipped entirely.

CLI:
    python3 -m src.discovery [--base 192.168.3] [--timeout 0.5]

Library:
    from src.discovery import discover_speakers, scan_lan
"""
from __future__ import annotations

import argparse
import queue
import socket
import struct
import threading
from typing import List, Optional

CONTROL_PORT = 8899
_MAGIC = b"$"


def local_ips() -> List[str]:
    """All IPv4 addresses of this machine (to exclude from scanning)."""
    ips: List[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        for info in infos:
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    # The robust fallback: a UDP "connect" to a broadcast-ish address
    # reveals the default-route source IP without sending anything.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect(("192.168.255.255", 1))
        ip = str(s.getsockname()[0])
        s.close()
        if ip not in ips:
            ips.append(ip)
    except OSError:
        pass
    return ips


def parse_miplay_frame(data: bytes):
    """Parse the first $-framed command from *data* -> (cmd, seq, payload)."""
    if len(data) < 9 or data[:1] != _MAGIC:
        return None
    cmd, seq, length = struct.unpack(">HHI", data[1:9])
    payload = data[9:9 + length]
    return cmd, seq, payload


def classify(data: bytes) -> str:
    """Classify a first-response frame: 'speaker' | 'miplay-like' | 'unknown'."""
    parsed = parse_miplay_frame(data)
    if parsed is None:
        return "unknown"
    cmd, _seq, payload = parsed
    if cmd == 0x0028 and len(payload) in (16, 17) and payload.isdigit():
        # Exact L15A challenge format validated by the V14 bootstrap.
        return "speaker"
    if cmd == 0x0028:
        return "miplay-like"  # answers MiPlay but not an L15A challenge
    return "unknown"


def _probe(ip: str, port: int, timeout: float, send_probe: bool):
    """Open one control connection; return (ip, first_frame_bytes|None)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        if send_probe:
            # A $-framed no-op; the receiver answers with its challenge
            # regardless (it greets every fresh control connection).
            s.sendall(_MAGIC + struct.pack(">HHI", 0x0028, 0, 0))
        s.settimeout(timeout)
        data = s.recv(64)
        s.close()
        return ip, (data if data else None)
    except OSError:
        return ip, None


def scan_lan(base_ip: str = "192.168.3", port: int = CONTROL_PORT,
             timeout: float = 0.5, workers: int = 60,
             skip_ips: Optional[List[str]] = None) -> List[dict]:
    """Scan base_ip.0/24; return descriptors of everything answering on *port*.

    Descriptor: {"ip", "kind" ("speaker"|"miplay-like"|"unknown"), "raw"}.
    """
    skip = set(skip_ips if skip_ips is not None else local_ips())
    q: "queue.Queue[str]" = queue.Queue()
    for i in range(1, 255):
        ip = f"{base_ip}.{i}"
        if ip not in skip:
            q.put(ip)

    found: List[dict] = []
    lock = threading.Lock()

    def worker():
        while True:
            try:
                ip = q.get_nowait()
            except queue.Empty:
                return
            try:
                _ip, data = _probe(ip, port, timeout, send_probe=True)
                if data:
                    with lock:
                        found.append({"ip": ip, "kind": classify(data),
                                      "raw": data[:32]})
            finally:
                q.task_done()

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(min(workers, max(1, q.qsize())))]
    for t in threads:
        t.start()
    q.join()
    found.sort(key=lambda d: d["ip"])
    return found


def default_base() -> str:
    """Best-guess /24 base from this machine's non-loopback IPv4."""
    for ip in local_ips():
        if not ip.startswith("127."):
            return ip.rsplit(".", 1)[0]
    return "192.168.3"


def discover_speakers(base_ip: str = "192.168.3", port: int = CONTROL_PORT,
                      timeout: float = 0.5) -> List[str]:
    """Return IPs of genuine L15A speakers found on the /24 segment."""
    return [d["ip"] for d in scan_lan(base_ip, port, timeout)
            if d["kind"] == "speaker"]


def main() -> int:
    p = argparse.ArgumentParser(description="Discover MiPlay L15A speakers on the LAN")
    p.add_argument("--base", default="192.168.3", help="/24 segment base, e.g. 192.168.3")
    p.add_argument("--timeout", type=float, default=0.5)
    a = p.parse_args()
    from ..common import log
    log(f"DISCOVERY scanning {a.base}.0/24 (excluding {', '.join(local_ips()) or 'localhost'})")
    found = scan_lan(a.base, timeout=a.timeout)
    speakers = [d for d in found if d["kind"] == "speaker"]
    for d in found:
        log(f"DISCOVERY {d['ip']:>15}  {d['kind']:<12} {d['raw']!r}")
    if speakers:
        log(f"DISCOVERY speakers: {', '.join(d['ip'] for d in speakers)}")
    else:
        log("DISCOVERY no L15A speaker found")
    return 0 if speakers else 1


if __name__ == "__main__":
    raise SystemExit(main())