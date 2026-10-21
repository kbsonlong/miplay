#!/usr/bin/env python3
"""Bounded macOS MiPlay legacy bootstrap probe for the user's L15A.

Dry-run is default. Live mode only targets 192.168.3.4:8899 and stops before
SetPlaySource/Open/RTSP/media/audio. Protocol sequence is based on public
S12/LX06 research; only the initial subset has been observed working on L15A.
"""
import argparse, hashlib, hmac, json, socket, struct, sys, time
from datetime import datetime

HOST_DEFAULT = "192.168.3.4"
PORT = 8899
MAGIC = 0x24
MAX_PAYLOAD = 1024 * 1024
SOURCE_VERSION = b"1.0.1123012\x00"
LEGACY_SEED = b"0.0.0.0"
NAMES = {0x000e:"GET_VOLUME",0x000f:"GET_VOLUME_ACK",0x0014:"GET_MEDIA_INFO",0x0015:"GET_MEDIA_INFO_ACK",0x001c:"GET_STATE",0x001d:"GET_STATE_ACK",0x001e:"GET_DEVICE_INFO",0x001f:"GET_DEVICE_INFO_ACK",0x0022:"NOTIFY",0x0028:"LEGACY_CHALLENGE",0x0029:"AUTH_ACK",0x0034:"GET_MIRROR_MODE",0x0035:"GET_MIRROR_MODE_ACK",0x0036:"NATIVE_VERSION",0x0037:"NATIVE_VERSION_ACK",0x0058:"SET_DEVICE_INFO",0x0059:"SET_DEVICE_INFO_ACK",0x0040:"SET_PLAY_SOURCE",0x0000:"OPEN"}

def log(s): print(f"[{datetime.now().astimezone().isoformat(timespec='milliseconds')}] {s}", flush=True)
def frame(cmd, seq, payload=b""):
    return b"\x24" + struct.pack(">HHI", cmd, seq, len(payload)) + payload
def exact(sock, n):
    b=bytearray()
    while len(b)<n:
        p=sock.recv(n-len(b))
        if not p: raise EOFError(f"peer closed with {n-len(b)} bytes expected")
        b.extend(p)
    return bytes(b)
def recv_frame(sock):
    h=exact(sock,9)
    if h[0]!=MAGIC: raise ValueError(f"bad magic 0x{h[0]:02x}")
    cmd,seq,n=struct.unpack(">HHI",h[1:])
    if n>MAX_PAYLOAD: raise ValueError(f"oversized payload {n}")
    return cmd,seq,exact(sock,n) if n else b""
def auth_response(challenge):
    key=hashlib.md5(LEGACY_SEED).hexdigest().encode("ascii")
    return hmac.new(key,challenge,hashlib.sha1).hexdigest().encode("ascii")
def describe(cmd,seq,payload):
    log(f"RX cmd=0x{cmd:04x} {NAMES.get(cmd,'UNKNOWN')} seq={seq} len={len(payload)}")
    if cmd==0x0037: log("  version="+repr(payload.rstrip(b"\x00").decode("ascii","replace")))
    elif cmd==0x0022:
        if payload.startswith(b"\x04mode"): log(f"  notify mode={payload[-1]}")
        elif payload.startswith(b"\x05state"): log(f"  notify state={payload[-1]}")
        elif payload.startswith(b"\x0bmediaInfoEx"): log("  notify mediaInfoEx (redacted)")
        else: log("  notify payload="+payload[:20].hex(" "))
    elif cmd==0x001f: log("  device info payload redacted (may contain account/device identifiers)")
    elif cmd in (0x000f,0x001d,0x0035) and len(payload)==5:
        log(f"  scalar={int.from_bytes(payload[1:5],'big')} (tag={payload[0]})")
    elif payload and len(payload)<=64 and all(32<=b<127 for b in payload): log("  ascii="+repr(payload.decode("ascii","replace")))
    elif payload: log("  payload prefix="+payload[:24].hex(" "))
def receive(sock, timeout):
    sock.settimeout(timeout)
    c,s,p=recv_frame(sock); describe(c,s,p); return c,s,p
def dry_run():
    challenge=b"5930784187532178"
    log("DRY RUN; no socket opened")
    for c,s,p in [(0x0036,0,SOURCE_VERSION),(0x0029,0x019e,auth_response(challenge)),(0x001e,1,b""),(0x0058,2,b'{"sourceName":"Mac mini"}'),(0x0058,3,b'{"isSameAccount":0}'),(0x0034,4,b""),(0x000e,5,b""),(0x001c,6,b""),(0x0014,7,b"")]:
        log(f"planned 0x{c:04x} {NAMES.get(c,'?')} seq={s} payload_len={len(p)}")
    log("No 0x0040 SetPlaySource, 0x0000 Open, RTSP or media frames are included.")
def run(host,port,confirm,observe,source_name):
    if host!=HOST_DEFAULT or confirm!=HOST_DEFAULT or port!=PORT:
        log(f"REFUSED: must explicitly target {HOST_DEFAULT}:{PORT} with --confirm-host {HOST_DEFAULT}"); return 2
    try:
        log(f"Connecting to {host}:{port}; bounded bootstrap only")
        with socket.create_connection((host,port),timeout=4) as s:
            s.settimeout(4); log(f"TCP connected local={s.getsockname()} remote={s.getpeername()}")
            c,seq,challenge=receive(s,4)
            if c!=0x0028 or len(challenge) not in (16,17) or not challenge.isdigit(): raise ValueError("unexpected initial challenge; no writes sent")
            s.sendall(frame(0x0036,0,SOURCE_VERSION)); log("TX 0x0036 seq=0 source version")
            s.sendall(frame(0x0029,seq,auth_response(challenge))); log(f"TX 0x0029 seq={seq} legacy HMAC-SHA1 response")
            s.sendall(frame(0x001e,1)); log("TX 0x001e seq=1 GetDeviceInfo")
            got_version=False; got_info=False; got_source_ack=False; source_sent=False
            deadline=time.monotonic()+observe
            while time.monotonic()<deadline and not (got_version and got_info and got_source_ack):
                try: c,rs,p=receive(s,min(1.0,max(.1,deadline-time.monotonic())))
                except socket.timeout: continue
                except EOFError as e: log(f"peer closed: {e}"); break
                if c==0x0037 and rs==0: got_version=True
                if c==0x001f and rs==1: got_info=True
                if c==0x0059 and rs==2: got_source_ack=True
                if got_version and not source_sent:
                    payload=json.dumps({"sourceName":source_name},separators=(",",":")).encode()
                    s.sendall(frame(0x0058,2,payload)); source_sent=True
                    log("TX 0x0058 seq=2 sourceName metadata")
            if not got_version: log("No 0x0037 ACK; stopping."); return 1
            if not got_info: log("No 0x001f ACK; stopping before next initialization stage."); return 1
            if not got_source_ack:
                end=time.monotonic()+2
                while time.monotonic()<end and not got_source_ack:
                    try: c,rs,p=receive(s,min(.5,max(.1,end-time.monotonic())))
                    except socket.timeout: continue
                    except EOFError: break
                    got_source_ack |= c==0x0059 and rs==2
            if not got_source_ack: log("No 0x0059 seq=2; stopping before account/mirror initialization."); return 1
            s.sendall(frame(0x0058,3,b'{"isSameAccount":0}')); log("TX 0x0058 seq=3 isSameAccount=0")
            s.sendall(frame(0x0034,4)); log("TX 0x0034 seq=4 GetMirrorMode")
            s.sendall(frame(0x000e,5)); s.sendall(frame(0x001c,6)); s.sendall(frame(0x0014,7))
            log("TX read-only queries 0x000e/0x001c/0x0014; no playback commands")
            end=time.monotonic()+observe
            while time.monotonic()<end:
                try: receive(s,min(1.0,max(.1,end-time.monotonic())))
                except socket.timeout: continue
                except EOFError as e: log(f"peer closed during observation: {e}"); break
            log("Probe complete; no SetPlaySource/Open/RTSP/media/audio sent.")
            return 0
    except (OSError,ValueError,EOFError) as e: log(f"Probe failed: {e}"); return 1

def main():
    p=argparse.ArgumentParser(description="Bounded L15A MiPlay legacy bootstrap v2")
    p.add_argument("--connect",action="store_true",help="connect to L15A; default dry-run")
    p.add_argument("--host",default=HOST_DEFAULT); p.add_argument("--port",type=int,default=PORT)
    p.add_argument("--confirm-host",default=""); p.add_argument("--observe",type=float,default=5)
    p.add_argument("--source-name",default="Mac mini")
    a=p.parse_args()
    if a.observe<=0: p.error("--observe must be positive")
    if not a.connect:
        dry_run(); print(f"\nExplicit command: python3 miplay_client_v2.py --connect --confirm-host {HOST_DEFAULT}"); return 0
    return run(a.host,a.port,a.confirm_host,a.observe,a.source_name)
if __name__=="__main__": sys.exit(main())
