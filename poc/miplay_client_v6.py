#!/usr/bin/env python3
"""MiPlay L15A v5: legacy bootstrap + WFD timer responder + persistent reverse channels.

Default is dry-run. With --connect, it starts TCP/7274 and UDP/36524 services,
then performs the already validated basic control bootstrap and read-only
queries. It still does NOT send SetPlaySource/Open by default.

Use --trigger-open only for an explicit, bounded live experiment. It sends
the captured single-room SetPlaySource and Open frames after listener startup.
This build can negotiate the initial RTSP control session, but does NOT send
AAC/RTP audio yet; it is a transport-validation stage, not a playable client.
"""
import argparse, hashlib, hmac, json, socket, struct, sys, threading, time
from datetime import datetime, timezone

HOST = "192.168.3.4"
CONTROL_PORT = 8899
RTSP_PORT = 7274
TIMER_PORT = 36524
MAGIC = b"$"
SOURCE_VERSION = b"1.0.1123012\x00"
SOURCE_NAME = "Mac mini"
SET_PLAY_SOURCE = b'{"ref_channel":"controlcenter","ref_function":"single_room","ref_content":"music_wangyiyun"}'

def log(s):
    print(f"[{datetime.now().astimezone().isoformat(timespec='milliseconds')}] {s}", flush=True)

def frame(cmd, seq, payload=b""):
    return MAGIC + struct.pack(">HHI", cmd, seq, len(payload)) + payload

def recv_exact(sock, n):
    b=bytearray()
    while len(b)<n:
        part=sock.recv(n-len(b))
        if not part: raise EOFError("peer closed")
        b.extend(part)
    return bytes(b)

def recv_frame(sock):
    h=recv_exact(sock,9)
    if h[:1]!=MAGIC: raise ValueError(f"bad MiPlay magic: {h[:9].hex()}")
    cmd,seq,n=struct.unpack(">HHI",h[1:])
    if n>1024*1024: raise ValueError(f"frame too large: {n}")
    return cmd,seq,recv_exact(sock,n) if n else b""

def legacy_hmac(challenge):
    key=hashlib.md5(b"0.0.0.0").hexdigest().encode("ascii")
    return hmac.new(key,challenge,hashlib.sha1).hexdigest().encode("ascii")

def date_header():
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

def rtsp(start, headers=(), body=b""):
    hs=list(headers)
    if not any(k.lower()=="content-length" for k,v in hs):
        hs.append(("Content-Length",str(len(body))))
    text=start+"\r\n"+"".join(f"{k}: {v}\r\n" for k,v in hs)+"\r\n"
    return text.encode("ascii")+body

def parse_rtsp(buf):
    marker=buf.find(b"\r\n\r\n")
    if marker<0: return None
    head=buf[:marker].decode("latin1")
    lines=head.split("\r\n")
    length=0
    for line in lines[1:]:
        if ":" in line:
            k,v=line.split(":",1)
            if k.lower()=="content-length":
                try: length=max(0,min(65536,int(v.strip())))
                except ValueError: return ("BAD",{},b"",marker+4)
    total=marker+4+length
    if len(buf)<total: return None
    headers={}
    for line in lines[1:]:
        if ":" in line:
            k,v=line.split(":",1); headers[k.strip().lower()]=v.strip()
    return (lines[0],headers,buf[marker+4:total],total)

def monotonic_us():
    return time.monotonic_ns()//1000

class Services:
    def __init__(self, ip, tcp_port, udp_port):
        self.ip,self.tcp_port,self.udp_port=ip,tcp_port,udp_port
        self.stop=threading.Event()
        self.tcp=None; self.udp=None; self.threads=[]
        self.rtsp_ready=threading.Event()
        self.audio_connections=0
        self.connection_lock = threading.Lock()
        self.reverse_sockets = {}

    def start(self):
        self.tcp=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        self.tcp.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        self.tcp.bind((self.ip,self.tcp_port)); self.tcp.listen(8); self.tcp.settimeout(.5)
        self.udp=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        self.udp.bind((self.ip,self.udp_port)); self.udp.settimeout(.5)
        self.threads=[threading.Thread(target=self._tcp_loop,daemon=True),
                      threading.Thread(target=self._udp_loop,daemon=True)]
        for t in self.threads:t.start()
        log(f"READY RTSP TCP {self.ip}:{self.tcp_port}")
        log(f"READY clock UDP {self.ip}:{self.udp_port}")
        log("RTSP/TIMER services are bound before any optional Open command.")

    def _udp_loop(self):
        while not self.stop.is_set():
            try: data,peer=self.udp.recvfrom(65535)
            except socket.timeout: continue
            except OSError:
                if not self.stop.is_set(): log("UDP socket closed unexpectedly")
                break
            if not data: continue
            recv_us=monotonic_us(); send_us=monotonic_us()
            # Match the captured Android/tablet TimerService layout: preserve
            # first 16 bytes, put send time at +16 and receive time at +24.
            out=bytearray(40); out[:min(40,len(data))]=data[:40]
            struct.pack_into("<Q",out,16,send_us)
            struct.pack_into("<Q",out,24,recv_us)
            try:self.udp.sendto(out,peer)
            except OSError as e: log(f"UDP timer reply failed: {e}")
            log(f"TIMER UDP {peer} request={len(data)} reply=40 bytes")

    def _tcp_loop(self):
        while not self.stop.is_set():
            try:c,peer=self.tcp.accept()
            except socket.timeout:continue
            except OSError:
                if not self.stop.is_set():log("TCP listener closed unexpectedly")
                break
            with self.connection_lock:
                self.audio_connections += 1
                n = self.audio_connections
                self.reverse_sockets[n] = c
            log(f"REVERSE TCP connection #{n} from {peer}")
            if n == 1:
                threading.Thread(target=self._rtsp_client,args=(c,peer,n),daemon=True).start()
            else:
                # The captured source keeps the additional reverse TCP channels
                # open for the duration of the session. Do not consume-and-close
                # them before SETUP/PLAY; channel 3 is reserved for future audio.
                threading.Thread(target=self._hold_reverse_channel,args=(c,peer,n),daemon=True).start()

    def _hold_reverse_channel(self, conn, peer, number):
        conn.settimeout(1)
        try:
            while not self.stop.is_set():
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    log(f"REVERSE TCP channel #{number} peer closed")
                    break
                log(f"REVERSE TCP channel #{number} data={len(data)} bytes prefix={data[:32].hex(' ')}")
                if number == 3:
                    log("Channel #3 is reserved for the future audio sender; no audio is sent in this build.")
        except OSError as exc:
            if not self.stop.is_set():
                log(f"REVERSE TCP channel #{number} error: {exc}")
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self.connection_lock:
                self.reverse_sockets.pop(number, None)

    def _rtsp_client(self,conn,peer,n):
        conn.settimeout(8)
        session_id=str(int(time.time())%900000000+100000000)
        # The first reverse TCP connection is RTSP. Other connections are
        # accepted for later audio-channel implementation, but are not decoded
        # as audio in this version.
        if n>1:
            self._hold_reverse_channel(conn, peer, n)
            return
        buf=bytearray()
        source_options_sent=False
        receiver_options_seen=False
        caps_sent=False
        selected_sent=False
        setup_trigger_sent=False
        setup_seen=False
        play_seen=False
        time_offset_sent=False
        try:
            # Captured source initiates OPTIONS on the receiver-opened socket.
            packed_ip=int.from_bytes(socket.inet_aton(self.ip),"big")
            msg=rtsp("OPTIONS * RTSP/1.0",[
                ("Date",date_header()),("Server",""),("CSeq","1"),
                ("Require","org.wfa.wfd1.0"),
                ("wfd_timer_server_port",f"{packed_ip}:{self.udp_port}")])
            conn.sendall(msg); source_options_sent=True
            log(f"RTSP TX OPTIONS CSeq=1 timer={packed_ip}:{self.udp_port}")
            end=time.monotonic()+35
            while not self.stop.is_set() and time.monotonic()<end:
                try: part=conn.recv(8192)
                except socket.timeout: continue
                if not part: log("RTSP peer closed"); break
                buf.extend(part)
                while True:
                    parsed=parse_rtsp(buf)
                    if parsed is None: break
                    start,headers,body,used=parsed; del buf[:used]
                    cseq=headers.get("cseq","?")
                    log(f"RTSP RX {start!r} CSeq={cseq} body={len(body)}")
                    if start.startswith("RTSP/1.0 200"):
                        # Responses to source-originated RTSP requests.
                        if cseq == "1":
                            log("RTSP observed 200 for source OPTIONS")
                        elif cseq == "2":
                            body_text = body.decode("latin1", errors="replace")
                            if ("wfd_audio_codecs: AAC 00000001 00" not in body_text or
                                "RTP/AVP/TCP;interleaved mode=play" not in body_text):
                                log("RTSP capability response does not match AAC/interleaved profile; closing session")
                                return
                            if not selected_sent:
                                selected_sent = True
                                bodyp = (f"wfd_audio_codecs: AAC 00000001 00\r\n"
                                         f"wfd_client_rtp_ports: RTP/AVP/TCP;interleaved mode=play\r\n"
                                         f"wfd_presentation_URL: rtsp://{self.ip}/wfd1.0/streamid=0 none\r\n").encode()
                                conn.sendall(rtsp("SET_PARAMETER rtsp://localhost/wfd1.0 RTSP/1.0",[
                                    ("Date",date_header()),("Server",""),("CSeq","3"),
                                    ("Content-Type","text/parameters")],bodyp))
                                log("RTSP TX selected AAC-only parameters CSeq=3")
                        elif cseq == "3" and selected_sent and not setup_trigger_sent:
                            setup_trigger_sent = True
                            trigger_body = b"wfd_trigger_method: SETUP\r\n"
                            conn.sendall(rtsp("SET_PARAMETER rtsp://localhost/wfd1.0 RTSP/1.0",[
                                ("Date",date_header()),("Server",""),("CSeq","4"),
                                ("Content-Type","text/parameters")],trigger_body))
                            log("RTSP TX SET_PARAMETER SETUP trigger CSeq=4")
                        elif cseq == "4":
                            log("RTSP SETUP trigger acknowledged; awaiting receiver SETUP")
                        elif cseq == "5" and time_offset_sent:
                            log("RTSP TIME_OFFSET acknowledged; control handshake ready (audio not implemented)")
                        continue
                    if start=="OPTIONS * RTSP/1.0":
                        receiver_options_seen=True
                        conn.sendall(rtsp("RTSP/1.0 200 OK",[
                            ("Date",date_header()),("Server",""),("CSeq",cseq),
                            ("Public","org.wfa.wfd1.0, SETUP, TEARDOWN, PLAY, PAUSE, GET_PARAMETER, SET_PARAMETER")]))
                        log("RTSP TX 200 OPTIONS")
                        if source_options_sent and not caps_sent:
                            caps_sent=True
                            bodyq=("wfd_video_formats\r\nwfd_audio_codecs\r\nwfd_client_rtp_ports\r\n"
                                   "wfd_tcp_enable\r\nwfd_tcp_multi_session_enable\r\nwfd_image_enable\r\n"
                                   "wfd_dynamic_video_enable\r\nwfd_standby_resume_capability\r\n"
                                   "wfd_video_bitrate\r\nwfd_current_video_info\r\n").encode()
                            conn.sendall(rtsp("GET_PARAMETER rtsp://localhost/wfd1.0 RTSP/1.0",[
                                ("Date",date_header()),("Server",""),("CSeq","2"),
                                ("Content-Type","text/parameters")],bodyq))
                            log("RTSP TX GET_PARAMETER CSeq=2 capabilities")
                    elif start.startswith("RTSP/1.0 200"):
                        pass
                    elif start.startswith("RTSP/"):
                        # Ignore RTSP responses already handled above.
                        continue
                    elif start.startswith("GET_PARAMETER"):
                        # Receiver capability query response.
                        caps=("wfd_audio_codecs: AAC 00000001 00\r\n"
                              "wfd_video_formats: none\r\n"
                              "wfd_video_enctype: none\r\nwfd_video_gamuttype: none\r\n"
                              "wfd_video_bitrate: none\r\nwfd_current_video_info: none\r\n"
                              "wfd_client_rtp_ports: RTP/AVP/TCP;interleaved mode=play\r\n"
                              "miplay_support_image: none\r\nwfd_standby_resume_capability: supported\r\n"
                              "device_info: -1 -1 -1 -1 -1 -1 -1\r\n").encode()
                        conn.sendall(rtsp("RTSP/1.0 200 OK",[
                            ("Date",date_header()),("Server",""),("CSeq",cseq)],caps))
                        log("RTSP TX capability response")
                        if not selected_sent:
                            selected_sent=True
                            bodyp=(f"wfd_audio_codecs: AAC 00000001 00\r\n"
                                   f"wfd_client_rtp_ports: RTP/AVP/TCP;interleaved mode=play\r\n"
                                   f"wfd_presentation_URL: rtsp://{self.ip}/wfd1.0/streamid=0 none\r\n").encode()
                            conn.sendall(rtsp("SET_PARAMETER rtsp://localhost/wfd1.0 RTSP/1.0",[
                                ("Date",date_header()),("Server",""),("CSeq","3"),
                                ("Content-Type","text/parameters")],bodyp))
                            log("RTSP TX selected AAC-only parameters CSeq=3")
                    elif start.startswith("SET_PARAMETER"):
                        if b"wfd_trigger_method: SETUP" in body:
                            conn.sendall(rtsp("RTSP/1.0 200 OK",[
                                ("Date",date_header()),("Server",""),("CSeq",cseq)]))
                            setup_trigger_sent=True
                            log("RTSP TX 200 SETUP trigger")
                        else:
                            conn.sendall(rtsp("RTSP/1.0 200 OK",[
                                ("Date",date_header()),("Server",""),("CSeq",cseq)]))
                            log("RTSP TX 200 SET_PARAMETER")
                    elif start.startswith("SETUP "):
                        setup_seen=True
                        transport=headers.get("transport","RTP/AVP/TCP;interleaved=0-1")
                        conn.sendall(rtsp("RTSP/1.0 200 OK",[
                            ("Date",date_header()),("Server",""),("CSeq",cseq),
                            ("Session",session_id+";timeout=60"),
                            ("Transport",transport if transport.endswith(";") else transport+";")]))
                        log(f"RTSP TX 200 SETUP Session={session_id}")
                    elif start.startswith("PLAY "):
                        play_seen=True
                        conn.sendall(rtsp("RTSP/1.0 200 OK",[
                            ("Date",date_header()),("Server",""),("CSeq",cseq),
                            ("Session",session_id+";timeout=60"),("Range","npt=now-")]))
                        conn.sendall(rtsp("TIME_OFFSET rtsp://localhost/wfd1.0 RTSP/1.0",[
                            ("Date",date_header()),("Server",""),("CSeq","5"),
                            ("Content-Type","text/parameters"),("TimeOffset",str(monotonic_us()))]))
                        time_offset_sent=True
                        log("RTSP TX 200 PLAY and TIME_OFFSET CSeq=5")
                    elif start.startswith("TIME_OFFSET "):
                        conn.sendall(rtsp("RTSP/1.0 200 OK",[
                            ("Date",date_header()),("Server",""),("CSeq",cseq)]))
                        log("RTSP TX 200 TIME_OFFSET")
                    elif start.startswith("TEARDOWN "):
                        conn.sendall(rtsp("RTSP/1.0 200 OK",[
                            ("Date",date_header()),("Server",""),("CSeq",cseq)]))
                        log("RTSP teardown acknowledged")
                        return
                    else:
                        conn.sendall(rtsp("RTSP/1.0 501 Not Implemented",[
                            ("Date",date_header()),("Server",""),("CSeq",cseq)]))
                        log(f"RTSP unsupported request: {start}")
            log("RTSP session ended; this build has no AAC/RTP sender.")
        except (OSError, ValueError) as e:
            log(f"RTSP session error: {e}")
        finally:
            try: conn.close()
            except OSError: pass

    def close(self):
        self.stop.set()
        for s in (self.tcp,self.udp):
            if s:
                try:s.close()
                except OSError:pass
        with self.connection_lock:
            sockets = list(self.reverse_sockets.values())
            self.reverse_sockets.clear()
        for s in sockets:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

def run(a):
    if a.host!=HOST or a.confirm_host!=HOST or a.control_port!=CONTROL_PORT:
        log(f"REFUSED: target must be exactly {HOST}:{CONTROL_PORT} and --confirm-host {HOST}")
        return 2
    services=Services(a.bind_ip,a.rtsp_port,a.timer_port)
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
            log("Open sent. RTSP/UDP service is diagnostic and AAC/RTP audio is not implemented.")
            time.sleep(a.hold)
            return 0
    except (OSError,ValueError,EOFError) as e:
        log(f"Probe failed: {e}"); return 1
    finally:
        services.close()
        if hasattr(run,"sent_src"): delattr(run,"sent_src")

def main():
    p=argparse.ArgumentParser(description="L15A MiPlay v4 RTSP/clock transport validation")
    p.add_argument("--connect",action="store_true")
    p.add_argument("--host",default=HOST)
    p.add_argument("--control-port",type=int,default=CONTROL_PORT)
    p.add_argument("--confirm-host",default="")
    p.add_argument("--bind-ip",default="192.168.3.18")
    p.add_argument("--rtsp-port",type=int,default=RTSP_PORT)
    p.add_argument("--timer-port",type=int,default=TIMER_PORT)
    p.add_argument("--observe",type=float,default=4)
    p.add_argument("--hold",type=float,default=20)
    p.add_argument("--trigger-open",action="store_true",help="explicitly send SetPlaySource/Open after readiness checks; no audio is sent")
    a=p.parse_args()
    if a.observe<0 or a.hold<0: p.error("observe/hold must be nonnegative")
    if not a.connect:
        log("DRY RUN; no sockets opened")
        log(f"will bind TCP {a.bind_ip}:{a.rtsp_port} and UDP {a.bind_ip}:{a.timer_port}")
        log("No SetPlaySource/Open is sent unless --connect --confirm-host 192.168.3.4 --trigger-open is specified.")
        log("This version negotiates RTSP only; AAC/RTP sender is not implemented.")
        return 0
    return run(a)

if __name__=="__main__":
    sys.exit(main())
