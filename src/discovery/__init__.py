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
import time
import fcntl
import ipaddress
from typing import List, Optional

from ..control.wire import frame, recv_frame, legacy_hmac
from ..control.client import SOURCE_VERSION

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


def local_networks() -> List[ipaddress.IPv4Network]:
    """Return routed IPv4 LAN networks from local interfaces.

    Uses interface address/netmask ioctls on macOS/Linux and falls back to a
    conservative /24 only when the platform does not expose them. Loopback,
    link-local, multicast, and point-to-point networks are excluded.
    """
    networks = []
    for ifindex, ifname in socket.if_nameindex():
        name = ifname.encode()[:15]
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            addr = fcntl.ioctl(sock.fileno(), 0xC0206921,
                               struct.pack('256s', name))[20:24]
            mask = fcntl.ioctl(sock.fileno(), 0xC0206925,
                               struct.pack('256s', name))[20:24]
            sock.close()
            ip = ipaddress.IPv4Address(addr)
            mask_int = int.from_bytes(mask, 'big')
            net = ipaddress.IPv4Network((int(ip), mask_int), strict=False)
            if (not ip.is_loopback and not ip.is_link_local and
                    not net.is_loopback and not net.is_link_local and
                    not net.is_multicast and net.prefixlen < 32 and
                    net not in networks):
                networks.append(net)
        except (OSError, ValueError):
            continue
    if networks:
        return networks
    base = default_base()
    return [ipaddress.IPv4Network(f'{base}.0/24')]


def discovery_networks() -> List[str]:
    """Human/CLI-friendly CIDR strings for all routed LAN candidates."""
    return [str(net) for net in local_networks()]


def parse_miplay_frame(data: bytes):
    """Parse the first $-framed command from *data* -> (cmd, seq, payload)."""
    if len(data) < 9 or data[:1] != _MAGIC:
        return None
    cmd, seq, length = struct.unpack(">HHI", data[1:9])
    payload = data[9:9 + length]
    return cmd, seq, payload


def classify(data: bytes) -> str:
    """Classify a first-response frame.

    ``speaker`` is the legacy L15A challenge shape.  ``miplay-candidate``
    covers newer Xiaomi devices such as the observed L16B (12-digit
    challenge); it still needs the optional DeviceInfo handshake before being
    selected for a model-specific media path.  This avoids confusing the
    local miplay-br bridge (14-digit challenge) with a physical speaker.
    """
    parsed = parse_miplay_frame(data)
    if parsed is None:
        return "unknown"
    cmd, _seq, payload = parsed
    if cmd != 0x0028:
        return "unknown"
    if len(payload) in (16, 17) and payload.isdigit():
        return "speaker"       # L15A, V14-verified
    if len(payload) in (12, 13) and payload.isdigit():
        return "miplay-candidate"  # newer Xiaomi MiPlay family, e.g. L16B
    if len(payload) == 14 and payload.isdigit():
        return "miplay-like"   # observed local bridge, not a device
    return "miplay-like"


def parse_device_info(payload: bytes) -> dict:
    """Extract printable key/value fields from the 0x001f DeviceInfo blob.

    The payload is a compact binary serialization rather than JSON.  Keys and
    values are length-prefixed, but unknown field types exist across firmware;
    this deliberately extracts only printable ``key``/``value`` pairs and
    preserves the raw payload for later protocol work.
    """
    text = payload.decode("latin1", errors="ignore")
    fields = {}
    known = ("accountId", "alonePlayCapacity", "audioLatencyMode",
             "bluetoothMac", "canAlonePlayCtrl", "channel", "deviceId",
             "deviceType", "groupId", "groupName", "house_Id", "isMaster",
             "miName", "miotDid", "model", "serialNumber")
    for key in known:
        pos = text.find(key)
        if pos < 0:
            continue
        tail = text[pos + len(key):]
        # Observed scalar string encoding: 0x0c + BE uint16 length + bytes.
        marker = tail.find("\x0c")
        if marker >= 0 and marker + 3 <= len(tail):
            n = int.from_bytes(tail[marker + 1:marker + 3].encode("latin1"), "big")
            value = tail[marker + 3:marker + 3 + n]
            if value and all(32 <= ord(ch) < 127 for ch in value):
                fields[key] = value
    return fields


def probe_device_info(ip: str, port: int = CONTROL_PORT,
                      timeout: float = 2.0) -> dict:
    """Perform challenge/auth/GetDeviceInfo and return a model profile.

    This is a protocol probe only: it stops after receiving 0x001f and never
    sends SetPlaySource, Open, SetMediaInfo, or media.
    """
    result = {"ip": ip, "kind": "unknown", "model": None,
              "device_info": {}, "version": None}
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(timeout)
            cmd, seq, challenge = recv_frame(s)
            result["kind"] = classify(frame(cmd, seq, challenge))
            if cmd != 0x0028 or not challenge.isdigit():
                return result
            s.sendall(frame(0x36, 0, SOURCE_VERSION))
            s.sendall(frame(0x29, seq, legacy_hmac(challenge)))
            s.sendall(frame(0x1e, 1))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                s.settimeout(max(.1, min(.5, deadline - time.monotonic())))
                try:
                    cmd, seq, payload = recv_frame(s)
                except socket.timeout:
                    continue
                if cmd == 0x37 and seq == 0:
                    result["version"] = payload.rstrip(b"\0").decode("ascii", errors="replace")
                elif cmd == 0x1f and seq == 1:
                    result["device_info"] = parse_device_info(payload)
                    result["model"] = result["device_info"].get("model")
                    return result
    except (OSError, ValueError, EOFError):
        return result
    return result



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


def scan_lan(base_ip: Optional[str] = None, port: int = CONTROL_PORT,
             timeout: float = 0.5, workers: int = 60,
             skip_ips: Optional[List[str]] = None) -> List[dict]:
    """Scan base_ip.0/24; return descriptors of everything answering on *port*.

    Descriptor: {"ip", "kind" ("speaker"|"miplay-like"|"unknown"), "raw"}.
    """
    skip = set(skip_ips if skip_ips is not None else local_ips())
    if base_ip is None:
        networks = local_networks()
        if not networks:
            return []
        # Preserve the historical return shape while scanning all routed LANs.
        all_found = []
        for network in networks:
            all_found.extend(scan_lan(str(network), port, timeout, workers, list(skip)))
        return sorted(all_found, key=lambda d: d["ip"])
    if "/" in base_ip:
        network = ipaddress.ip_network(base_ip, strict=False)
        hosts = [str(ip) for ip in network.hosts()]
    else:
        hosts = [f"{base_ip}.{i}" for i in range(1, 255)]
    q: "queue.Queue[str]" = queue.Queue()
    for ip in hosts:
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
                    kind = classify(data)
                    profile = {"ip": ip, "kind": kind,
                               "raw": data[:32]}
                    if kind == "miplay-candidate":
                        # Confirm physical device and obtain model before
                        # exposing it as a speaker; this rejects bridge-like
                        # responders without DeviceInfo.
                        info = probe_device_info(ip, port, max(timeout, 1.0))
                        if info.get("device_info"):
                            profile.update({"kind": "miplay-speaker",
                                            "model": info.get("model"),
                                            "version": info.get("version"),
                                            "device_info": info.get("device_info")})
                    with lock:
                        found.append(profile)
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


def discover_speakers(base_ip: Optional[str] = None, port: int = CONTROL_PORT,
                      timeout: float = 0.5) -> List[str]:
    """Return only L15A IPs whose media path is currently verified."""
    return [d["ip"] for d in scan_lan(base_ip, port, timeout)
            if d["kind"] == "speaker"]


def discover_miplay_devices(base_ip: Optional[str] = None, port: int = CONTROL_PORT,
                            timeout: float = 0.5) -> List[dict]:
    """Return L15A and model-confirmed newer MiPlay speaker profiles."""
    return [d for d in scan_lan(base_ip, port, timeout)
            if d["kind"] in ("speaker", "miplay-speaker")]


def main() -> int:
    p = argparse.ArgumentParser(description="Discover MiPlay L15A speakers on the LAN")
    p.add_argument("--base", default=None,
                   help="optional CIDR/base override; default derives all routed LANs")
    p.add_argument("--timeout", type=float, default=0.5)
    a = p.parse_args()
    from ..common import log
    targets = [a.base] if a.base else discovery_networks()
    log(f"DISCOVERY scanning {', '.join(targets)} "
        f"(excluding {', '.join(local_ips()) or 'localhost'})")
    found = scan_lan(a.base, timeout=a.timeout)
    speakers = [d for d in found if d["kind"] in ("speaker", "miplay-speaker")]
    for d in found:
        log(f"DISCOVERY {d['ip']:>15}  {d['kind']:<16} "
            f"model={d.get('model', '-')!s:<8} {d['raw']!r}")
    if speakers:
        log("DISCOVERY MiPlay devices: " + ", ".join(
            f"{d['ip']}({d.get('model') or 'L15A'}):{d['kind']}" for d in speakers))
    else:
        log("DISCOVERY no MiPlay speaker found")
    return 0 if speakers else 1


if __name__ == "__main__":
    raise SystemExit(main())