"""MiPlay Legacy source bootstrap and post-Open control notifications."""
import json
import socket
import struct
import threading
import time

from ..common import log
from .wire import MAGIC, frame, recv_frame, legacy_hmac
from ..rtsp.session import Services

HOST = '192.168.3.4'
CONTROL_PORT = 8899
RTSP_PORT = 7274
TIMER_PORT = 36524
SOURCE_VERSION = b'1.0.1123012\x00'
SOURCE_NAME = 'Mac mini'
SET_PLAY_SOURCE = b'{"ref_channel":"controlcenter","ref_function":"single_room","ref_content":"music_wangyiyun"}'

def monitor_post_open_control(sock, services):
    """Read post-Open notifications and gate media until receiver readiness."""
    stop_event = services.stop
    buf = bytearray()
    sock.settimeout(0.5)
    while not stop_event.is_set():
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError as exc:
            if not stop_event.is_set():
                log(f"CONTROL monitor stopped: {exc}")
            return
        if not chunk:
            log("CONTROL peer closed after Open")
            return
        buf.extend(chunk)
        while len(buf) >= 9:
            if buf[0] != MAGIC[0]:
                # The control stream should contain only MiPlay framed commands.
                log(f"CONTROL monitor invalid frame prefix={buf[:16].hex(' ')}")
                return
            cmd, seq, length = struct.unpack(">HHI", buf[1:9])
            if length > 1024 * 1024:
                log(f"CONTROL monitor refusing oversized frame length={length}")
                return
            if len(buf) < 9 + length:
                break
            payload = bytes(buf[9:9 + length])
            del buf[:9 + length]
            log(f"RX post-Open control cmd=0x{cmd:04x} seq={seq} len={length}")
            if cmd == 0x0022:
                # Keep the diagnostic preview bounded; recognize the exact
                # observed compact notification encodings without dumping device metadata.
                preview = payload[:96].decode("utf-8", errors="replace")
                log(f"  NOTIFY preview={preview!r}")
                if b"\x0efirst-audiopcm\x03\x01" in payload:
                    services.first_pcm_ready.set()
                    log("  receiver readiness: first-audiopcm=1")
                if payload.startswith(b"\x05state\x03\x02"):
                    services.receiver_playing_ready.set()
                    log("  receiver readiness: state=2")
                services._maybe_start_audio()

def run(a):
    if a.host!=HOST or a.confirm_host!=HOST or a.control_port!=CONTROL_PORT:
        log(f"REFUSED: target must be exactly {HOST}:{CONTROL_PORT} and --confirm-host {HOST}")
        return 2
    services=Services(a.bind_ip,a.rtsp_port,a.timer_port)
    services.audio_file = a.audio_file
    try: services.start()
    except OSError as e:
        services.close(); log(f"Cannot bind media endpoints: {e}"); return 1
    try:
        with socket.create_connection((a.host,a.control_port),timeout=4) as s:
            s.settimeout(3); log(f"CONTROL connected local={s.getsockname()}")
            c,q,p=recv_frame(s); log(f"RX 0x{c:04x} seq={q} len={len(p)}")
            if c!=0x28 or len(p) not in (16,17) or not p.isdigit(): raise ValueError("unexpected challenge")
            s.sendall(frame(0x36,0,SOURCE_VERSION)); s.sendall(frame(0x29,q,legacy_hmac(p))); s.sendall(frame(0x1e,1))
            log("TX version + legacy auth + GetDeviceInfo")
            got_v=got_info=got_src=False
            end=time.monotonic()+5
            while time.monotonic()<end and not (got_v and got_info and got_src):
                s.settimeout(max(.1,min(1,end-time.monotonic())))
                try:c,q,p=recv_frame(s)
                except socket.timeout:continue
                except EOFError:break
                log(f"RX 0x{c:04x} seq={q} len={len(p)}")
                got_v |= c==0x37 and q==0
                got_info |= c==0x1f and q==1
                if got_v and not getattr(run,"sent_src",False):
                    s.sendall(frame(0x58,2,json.dumps({"sourceName":SOURCE_NAME},separators=(",",":")).encode()))
                    log("TX 0x0058 seq=2 sourceName"); run.sent_src=True
                got_src |= c==0x59 and q==2
            if not (got_v and got_info and got_src):
                log("Bootstrap incomplete; refusing account/status or playback setup."); return 1
            s.sendall(frame(0x58,3,b'{"isSameAccount":0}'))
            s.sendall(frame(0x34,4)); s.sendall(frame(0x0e,5)); s.sendall(frame(0x1c,6)); s.sendall(frame(0x14,7))
            log("TX account context + read-only status queries")
            end=time.monotonic()+2
            while time.monotonic()<end:
                s.settimeout(max(.1,min(.5,end-time.monotonic())))
                try:c,q,p=recv_frame(s)
                except socket.timeout:continue
                except EOFError:break
                log(f"RX 0x{c:04x} seq={q} len={len(p)}")
            if not a.trigger_open:
                log("No SetPlaySource/Open sent. Add --trigger-open only after reviewing this experiment.")
                time.sleep(a.hold)
                return 0
            # Explicitly opt-in: send observed playback-time identity refresh and gate on ACKs.
            s.sendall(frame(0x58,8,json.dumps({"sourceName":SOURCE_NAME},separators=(",",":")).encode()))
            s.sendall(frame(0x1e,9))
            log("TX playback-time refresh sourceName seq=8 + GetDeviceInfo seq=9")
            seen=set(); end=time.monotonic()+4
            while time.monotonic()<end and not {(0x59,8),(0x1f,9)}.issubset(seen):
                s.settimeout(max(.1,min(.5,end-time.monotonic())))
                try:c,q,p=recv_frame(s)
                except socket.timeout:continue
                except EOFError:break
                log(f"RX 0x{c:04x} seq={q} len={len(p)}"); seen.add((c,q))
            if not {(0x59,8),(0x1f,9)}.issubset(seen):
                log("Missing playback refresh ACKs; aborting before SetPlaySource/Open."); return 1
            s.sendall(frame(0x58,10,b'{"isSameAccount":0}')); s.sendall(frame(0x34,11))
            log("TX isSameAccount seq=10 + GetMirrorMode seq=11")
            seen=set(); end=time.monotonic()+3
            while time.monotonic()<end and not {(0x59,10),(0x35,11)}.issubset(seen):
                s.settimeout(max(.1,min(.5,end-time.monotonic())))
                try:c,q,p=recv_frame(s)
                except socket.timeout:continue
                except EOFError:break
                log(f"RX 0x{c:04x} seq={q} len={len(p)}"); seen.add((c,q))
            if not {(0x59,10),(0x35,11)}.issubset(seen):
                log("Missing account/mode ACKs; aborting before SetPlaySource/Open."); return 1
            s.sendall(frame(0x1a,12)); log("TX heartbeat seq=12")
            try:
                s.settimeout(2); c,q,p=recv_frame(s); log(f"RX 0x{c:04x} seq={q} len={len(p)}")
                if (c,q)!=(0x1b,12): raise ValueError("unexpected heartbeat response")
            except (socket.timeout,EOFError,ValueError) as e:
                log(f"Heartbeat not verified; aborting before SetPlaySource/Open: {e}"); return 1
            s.sendall(frame(0x40,13,SET_PLAY_SOURCE)); log("TX 0x0040 SetPlaySource seq=13")
            # Captured source sends Open after SetPlaySource, without waiting for an ACK.
            open_payload=f"wfd://{a.bind_ip}:{a.rtsp_port}?mirrorMode=1".encode()+b"\x00"
            s.sendall(frame(0x0000,14,open_payload)); log("TX 0x0000 Open seq=14")
            log("Open sent. Waiting for RTSP PLAY/TIME_OFFSET before SetMediaInfo and media.")
            control_monitor = threading.Thread(
                target=monitor_post_open_control, args=(s, services), daemon=True)
            control_monitor.start()
            if a.audio_file:
                if not services.rtsp_ready.wait(timeout=12):
                    log("RTSP did not reach PLAY/TIME_OFFSET readiness; refusing to send audio.")
                    return 1
                media_info = {
                    "mArtist": "Windows",
                    "mAlbum": "",
                    "mTitle": "System Audio",
                    "mDuration": 2147483647,
                    "id": "",
                    "mCoverUrl": "",
                    "status": 0,
                    "volume": 24,
                    "mArt": "",
                    "mSourceName": "Mac mini",
                    "mDeviceState": 2,
                }
                media_payload = json.dumps(
                    media_info, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                s.sendall(frame(0x0012, 15, media_payload))
                log(f"TX 0x0012 SET_MEDIA_INFO seq=15 payload_len={len(media_payload)}")
                services.media_info_ready.set()
                services._maybe_start_audio()
                log("SetMediaInfo sent; waiting for first-audiopcm=1 AND state=2 before audio.")
            time.sleep(a.hold)
            return 0
    except (OSError,ValueError,EOFError) as e:
        log(f"Probe failed: {e}"); return 1
    finally:
        services.close()
        if hasattr(run,"sent_src"): delattr(run,"sent_src")

