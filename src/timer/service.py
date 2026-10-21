"""MiPlay UDP TimerService responder; 40-byte captured packet behavior."""
import socket
import struct
from ..common import monotonic_us, log


def reply_timer(data: bytes, *, recv_us: int, send_us: int) -> bytes:
    """Mirror V14: copy first 40 bytes, then write little-endian clocks."""
    out = bytearray(40)
    out[:min(40, len(data))] = data[:40]
    struct.pack_into('<Q', out, 16, send_us)
    struct.pack_into('<Q', out, 24, recv_us)
    return bytes(out)


def run_udp_loop(service):
    while not service.stop.is_set():
        try:
            data, peer = service.udp.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            if not service.stop.is_set():
                log('UDP socket closed unexpectedly')
            break
        if not data:
            continue
        recv_us = monotonic_us()
        send_us = monotonic_us()
        out = reply_timer(data, recv_us=recv_us, send_us=send_us)
        try:
            service.udp.sendto(out, peer)
        except OSError as exc:
            log(f'UDP timer reply failed: {exc}')
        log(f'TIMER UDP {peer} request={len(data)} reply=40 bytes')
