"""Reverse TCP WFD/RTSP session, preserving V14 session behavior."""
import socket
import threading
import struct
import time
from ..common import log, monotonic_us
from ..timer.service import run_udp_loop
from ..media.audio import send_adts_as_miplay_rtp
from .wire import date_header, rtsp, parse_rtsp

class Services:
    def __init__(self, ip, tcp_port, udp_port):
        self.ip,self.tcp_port,self.udp_port=ip,tcp_port,udp_port
        self.stop=threading.Event()
        self.tcp=None; self.udp=None; self.threads=[]
        self.rtsp_ready=threading.Event()
        self.audio_connections=0
        self.connection_lock = threading.Lock()
        self.reverse_sockets = {}
        self.audio_file = None
        self.audio_stop = threading.Event()
        self.audio_started = threading.Event()
        self.audio_thread = None
        self.play_event = threading.Event()
        self.rtsp_ready = threading.Event()
        self.media_info_ready = threading.Event()
        self.first_pcm_ready = threading.Event()
        self.receiver_playing_ready = threading.Event()
        self.time_offset_us = None

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
        run_udp_loop(self)

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
        # The captured legacy session uses channel #2 as an unused auxiliary
        # connection and channel #3 for media. Keep both alive; channel #3 is
        # the destination for the custom 24-bit-length RTP wire frames.
        conn.settimeout(1)
        if number == 3:
            log("Media TCP channel #3 ready; waiting for RTSP PLAY and audio file")
            self._maybe_start_audio()
        try:
            while not self.stop.is_set():
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    log(f"REVERSE TCP channel #{number} peer closed")
                    break
                log(f"REVERSE TCP channel #{number} inbound={len(data)} bytes prefix={data[:32].hex(' ')}")
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

    def _maybe_start_audio(self):
        if (not self.audio_file or not self.play_event.is_set()
                or not self.media_info_ready.is_set()
                or self.audio_started.is_set()):
            return
        with self.connection_lock:
            media_sock = self.reverse_sockets.get(3)
        if media_sock is None:
            return
        self.audio_started.set()
        self.audio_thread = threading.Thread(
            target=self._send_audio_file, args=(media_sock,), daemon=True)
        self.audio_thread.start()

    def _send_audio_file(self, media_sock):
        try:
            send_adts_as_miplay_rtp(
                self.audio_file, media_sock, self.audio_stop,
                self.first_pcm_ready, self.receiver_playing_ready,
                self.time_offset_us)
        except Exception as exc:
            log(f"AUDIO sender failed: {exc}")
        finally:
            log("AUDIO sender finished")

    def _rtsp_client(self,conn,peer,n):
        conn.settimeout(8)
        session_id=str(int(time.time())%900000000+100000000)
        # The first reverse TCP connection is RTSP. Other connections are
        # accepted on separate auxiliary/media sockets.
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
                            self.rtsp_ready.set()
                            log("RTSP TIME_OFFSET acknowledged; post-Open control is ready")
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
                        if not body.strip():
                            conn.sendall(rtsp("RTSP/1.0 200 OK",[
                                ("Date",date_header()),("Server",""),("CSeq",cseq)]))
                            log("RTSP TX empty 200 for keepalive GET_PARAMETER")
                        else:
                            caps=("wfd_audio_codecs: AAC 00000001 00\r\n"
                                  "wfd_video_formats: none\r\nwfd_video_enctype: none\r\nwfd_video_gamuttype: none\r\n"
                                  "wfd_video_bitrate: none\r\nwfd_current_video_info: none\r\n"
                                  "wfd_client_rtp_ports: RTP/AVP/TCP;interleaved mode=play\r\n"
                                  "miplay_support_image: none\r\nwfd_standby_resume_capability: supported\r\n"
                                  "device_info: -1 -1 -1 -1 -1 -1 -1\r\n").encode()
                            conn.sendall(rtsp("RTSP/1.0 200 OK",[
                                ("Date",date_header()),("Server",""),("CSeq",cseq),
                                ("Content-Type","text/parameters")],caps))
                            log("RTSP TX capability response")
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
                        self.time_offset_us = monotonic_us()
                        conn.sendall(rtsp("TIME_OFFSET rtsp://localhost/wfd1.0 RTSP/1.0",[
                            ("Date",date_header()),("Server",""),("CSeq","5"),
                            ("Content-Type","text/parameters"),("TimeOffset",str(self.time_offset_us))]))
                        time_offset_sent=True
                        log("RTSP TX 200 PLAY and TIME_OFFSET CSeq=5")
                        self.play_event.set()
                        self._maybe_start_audio()
                    elif start.startswith("TIME_OFFSET "):
                        conn.sendall(rtsp("RTSP/1.0 200 OK",[
                            ("Date",date_header()),("Server",""),("CSeq",cseq)]))
                        log("RTSP TX 200 TIME_OFFSET")
                    elif start.startswith("VIDEO_LATENCY "):
                        # The public rooted-phone source trace observes repeated
                        # VIDEO_LATENCY telemetry and does not reply to it.
                        # Do not emit 501/200 responses; just record the event.
                        log(f"RTSP ignored VIDEO_LATENCY telemetry CSeq={cseq}")
                    elif start.startswith("TEARDOWN "):
                        conn.sendall(rtsp("RTSP/1.0 200 OK",[
                            ("Date",date_header()),("Server",""),("CSeq",cseq)]))
                        log("RTSP teardown acknowledged")
                        return
                    else:
                        conn.sendall(rtsp("RTSP/1.0 501 Not Implemented",[
                            ("Date",date_header()),("Server",""),("CSeq",cseq)]))
                        log(f"RTSP unsupported request: {start}")
            log("RTSP session ended; media sender may have completed or stopped.")
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

