"""Long-running MiPlay speaker session: bootstrap, heartbeat, queued TTS.

The bootstrap below is a faithful transcription of the device-verified V14
sequence in src/control/client.py (challenge -> auth -> device info ->
sourceName -> account context -> playback refresh -> heartbeat check ->
SetPlaySource -> Open), restructured for a long-lived service:

* the control socket stays open with a periodic heartbeat thread;
* successive utterances reuse the same reverse media channel #3 and share
  RTP seq / PTS / TS continuity counters via RtpStreamState;
* a dead session (peer close, control error) is detected and transparently
  re-bootstrapped on the next speak request.
"""
from __future__ import annotations

import json
import queue
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field

from ..common import log
from ..control.wire import frame, recv_frame, legacy_hmac
from ..control.client import (HOST, CONTROL_PORT, RTSP_PORT, TIMER_PORT,
                              SOURCE_VERSION, SOURCE_NAME, SET_PLAY_SOURCE,
                              monitor_post_open_control)
from ..media.audio import send_adts_stream, RtpStreamState
from .engine import stream_tts_adts, DEFAULT_VOICE, DEFAULT_RATE


@dataclass
class Job:
    id: str
    text: str
    voice: str = DEFAULT_VOICE
    rate: str = DEFAULT_RATE
    status: str = "queued"  # queued | playing | done | stopped | error
    error: str = ""
    enqueued_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    frames: int = 0
    stop: threading.Event = field(default_factory=threading.Event)


class MediaChannelError(RuntimeError):
    """The reverse media channel #3 is gone or never arrived.

    The receiver tears its reverse TCP channels down some time after playback
    ends (observed: state=3 notify, then FIN on #3), so a session whose media
    channel disappeared must be fully re-opened (SetPlaySource + Open) before
    the next utterance.
    """


class SpeakerSession:
    """One MiPlay control/media session against the L15A."""

    def __init__(self, speaker_host, bind_ip, heartbeat_interval=10.0):
        self.speaker_host = speaker_host
        self.bind_ip = bind_ip
        self.control_port = CONTROL_PORT
        self.heartbeat_interval = heartbeat_interval
        self.services = None
        self.control = None
        self.seq = 14  # V14 bootstrap uses seq<=14 (Open); heartbeats continue after
        self.media_info_sent = False
        self.media_seen = False  # True once reverse channel #3 has been observed
        self.state = RtpStreamState()
        self.control_alive = threading.Event()
        self._heartbeat_stop = threading.Event()

    # ------------------------------------------------------------------ boot

    def open(self):
        services = None
        try:
            from ..rtsp.session import Services
            services = Services(self.bind_ip, RTSP_PORT, TIMER_PORT)
            services.start()
            self.services = services
            s = socket.create_connection((self.speaker_host, self.control_port), timeout=4)
        except OSError as exc:
            if services:
                services.close()
            raise RuntimeError(f"cannot reach/bind MiPlay endpoints: {exc}") from exc

        try:
            s.settimeout(3)
            log(f"CONTROL connected local={s.getsockname()}")
            self._bootstrap(s)
            self.control = s
            self.control_alive.set()
            threading.Thread(target=self._monitor_wrap, daemon=True).start()
            if self.heartbeat_interval > 0:
                threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        except BaseException:
            try:
                s.close()
            except OSError:
                pass
            if self.services:
                self.services.close()
            self.services = None
            raise

    def _bootstrap(self, s):
        """V14-verified handshake through Open. Raises RuntimeError on any gate."""
        c, q, p = recv_frame(s)
        log(f"RX 0x{c:04x} seq={q} len={len(p)}")
        if c != 0x28 or len(p) not in (16, 17) or not p.isdigit():
            raise RuntimeError("unexpected challenge")
        s.sendall(frame(0x36, 0, SOURCE_VERSION))
        s.sendall(frame(0x29, q, legacy_hmac(p)))
        s.sendall(frame(0x1e, 1))
        log("TX version + legacy auth + GetDeviceInfo")
        got_v = got_info = got_src = False
        end = time.monotonic() + 5
        while time.monotonic() < end and not (got_v and got_info and got_src):
            s.settimeout(max(.1, min(1, end - time.monotonic())))
            try:
                c, q, p = recv_frame(s)
            except socket.timeout:
                continue
            except EOFError:
                break
            log(f"RX 0x{c:04x} seq={q} len={len(p)}")
            got_v |= c == 0x37 and q == 0
            got_info |= c == 0x1f and q == 1
            if got_v and not got_src:
                s.sendall(frame(0x58, 2, json.dumps(
                    {"sourceName": SOURCE_NAME}, separators=(",", ":")).encode()))
                log("TX 0x0058 seq=2 sourceName")
            got_src |= c == 0x59 and q == 2
        if not (got_v and got_info and got_src):
            raise RuntimeError("bootstrap incomplete (version/deviceinfo/sourceName)")

        s.sendall(frame(0x58, 3, b'{"isSameAccount":0}'))
        s.sendall(frame(0x34, 4)); s.sendall(frame(0x0e, 5))
        s.sendall(frame(0x1c, 6)); s.sendall(frame(0x14, 7))
        log("TX account context + read-only status queries")

        # Playback-time identity refresh, gated on ACKs exactly like V14.
        s.sendall(frame(0x58, 8, json.dumps(
            {"sourceName": SOURCE_NAME}, separators=(",", ":")).encode()))
        s.sendall(frame(0x1e, 9))
        log("TX playback-time refresh sourceName seq=8 + GetDeviceInfo seq=9")
        if not self._wait_acks(s, {(0x59, 8), (0x1f, 9)}, 4):
            raise RuntimeError("missing playback refresh ACKs")
        s.sendall(frame(0x58, 10, b'{"isSameAccount":0}'))
        s.sendall(frame(0x34, 11))
        log("TX isSameAccount seq=10 + GetMirrorMode seq=11")
        if not self._wait_acks(s, {(0x59, 10), (0x35, 11)}, 3):
            raise RuntimeError("missing account/mode ACKs")

        s.sendall(frame(0x1a, 12))
        log("TX heartbeat seq=12")
        try:
            s.settimeout(2)
            c, q, p = recv_frame(s)
            log(f"RX 0x{c:04x} seq={q} len={len(p)}")
            if (c, q) != (0x1b, 12):
                raise RuntimeError("unexpected heartbeat response")
        except (socket.timeout, EOFError) as exc:
            raise RuntimeError(f"heartbeat not verified: {exc}") from exc

        s.sendall(frame(0x40, 13, SET_PLAY_SOURCE))
        log("TX 0x0040 SetPlaySource seq=13")
        open_payload = f"wfd://{self.bind_ip}:{RTSP_PORT}?mirrorMode=1".encode() + b"\x00"
        s.sendall(frame(0x0000, 14, open_payload))
        log("TX 0x0000 Open seq=14; waiting for reverse RTSP/media channels")

    def _wait_acks(self, s, wanted, timeout):
        seen = set()
        end = time.monotonic() + timeout
        while time.monotonic() < end and not wanted.issubset(seen):
            s.settimeout(max(.1, min(.5, end - time.monotonic())))
            try:
                c, q, p = recv_frame(s)
            except socket.timeout:
                continue
            except EOFError:
                break
            log(f"RX 0x{c:04x} seq={q} len={len(p)}")
            seen.add((c, q))
        return wanted.issubset(seen)

    # ------------------------------------------------------------- keepalive

    def _monitor_wrap(self):
        try:
            monitor_post_open_control(self.control, self.services)
        finally:
            self.control_alive.clear()
            log("SESSION control monitor exited; session considered dead")

    def _heartbeat_loop(self):
        missed = 0
        while not self._heartbeat_stop.is_set() and self.control_alive.is_set():
            if self._heartbeat_stop.wait(self.heartbeat_interval):
                break
            try:
                self.seq += 1
                self.control.sendall(frame(0x1a, self.seq))
                missed = 0
            except OSError as exc:
                missed += 1
                log(f"HEARTBEAT send failed ({missed}): {exc}")
                if missed >= 2:
                    self.control_alive.clear()
                    break
        log("HEARTBEAT loop exited")

    # ----------------------------------------------------------------- media

    def has_media_channel(self):
        if self.services is None:
            return False
        with self.services.connection_lock:
            return self.services.reverse_sockets.get(3) is not None

    def wait_media(self, timeout=15.0, stop_event=None):
        """Block until RTSP PLAY/TIME_OFFSET and reverse channel #3 exist.

        Raises MediaChannelError when the channel does not appear in time (the
        receiver tears reverse channels down after playback ends + idle), or
        when stop_event fires first.
        """
        if not self.services.rtsp_ready.wait(timeout):
            raise MediaChannelError("RTSP did not reach PLAY/TIME_OFFSET readiness")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise MediaChannelError("interrupted before media channel")
            if not self.control_alive.is_set():
                raise MediaChannelError("control connection died before media channel")
            if self.has_media_channel():
                self.media_seen = True
                return self._media_socket()
            time.sleep(0.05)
        raise MediaChannelError("reverse media channel #3 did not arrive")

    def _media_socket(self):
        with self.services.connection_lock:
            sock = self.services.reverse_sockets.get(3)
        if sock is None:
            raise MediaChannelError("media channel #3 disappeared")
        return sock

    def _send_media_info_once(self, text):
        if self.media_info_sent or not self.control:
            return
        info = {
            "mArtist": "Agent",
            "mAlbum": "",
            "mTitle": text[:40] or "TTS",
            "mDuration": 2147483647,
            "id": "",
            "mCoverUrl": "",
            "status": 0,
            "volume": 24,
            "mArt": "",
            "mSourceName": SOURCE_NAME,
            "mDeviceState": 2,
        }
        payload = json.dumps(info, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.seq += 1
        self.control.sendall(frame(0x0012, self.seq, payload))
        self.media_info_sent = True
        self.services.media_info_ready.set()
        log(f"TX 0x0012 SET_MEDIA_INFO seq={self.seq} payload_len={len(payload)}")

    def speak(self, job):
        """Stream one Job's TTS audio to the speaker; blocks until finished."""
        media_sock = self.wait_media(stop_event=job.stop)
        self._send_media_info_once(job.text)

        def counting(frames):
            for f in frames:
                job.frames += 1
                yield f

        try:
            send_adts_stream(
                counting(stream_tts_adts(job.text, job.voice, job.rate,
                                         stop_event=job.stop)),
                media_sock, job.stop,
                self.services.first_pcm_ready, self.services.receiver_playing_ready,
                self.services.time_offset_us, state=self.state)
        except ValueError as exc:
            if not job.stop.is_set():
                raise
            # /stop mid-frame leaves a partial ADTS tail; it is discardable.
            log(f"AUDIO interrupted; discarding partial tail ({exc})")

    # ----------------------------------------------------------------- close

    def close(self):
        self._heartbeat_stop.set()
        self.control_alive.clear()
        if self.control:
            try:
                self.control.close()
            except OSError:
                pass
            self.control = None
        if self.services:
            self.services.close()
            self.services = None

    def healthy(self):
        return (self.control_alive.is_set() and self.services is not None
                and not self.services.stop.is_set())


class SpeakerService:
    """Queue-driven facade: submit text jobs, one utterance plays at a time."""

    def __init__(self, speaker_host=HOST, bind_ip="192.168.3.18",
                 heartbeat_interval=10.0):
        self.speaker_host = speaker_host
        self.bind_ip = bind_ip
        self.heartbeat_interval = heartbeat_interval
        self.session = None
        self.jobs: dict[str, Job] = {}
        self.jobs_lock = threading.Lock()
        self.current: Job | None = None
        self._queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._worker.start()
        log(f"SERVICE worker started (speaker={self.speaker_host} bind={self.bind_ip})")

    def submit(self, text, voice=DEFAULT_VOICE, rate=DEFAULT_RATE) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], text=text, voice=voice, rate=rate)
        with self.jobs_lock:
            self.jobs[job.id] = job
            if len(self.jobs) > 100:
                for jid in list(self.jobs)[:-100]:
                    self.jobs.pop(jid, None)
        self._queue.put(job)
        return job

    def stop_all(self):
        cleared = 0
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                break
            job.status = "stopped"
            job.finished_at = time.time()
            cleared += 1
        cur = self.current
        if cur is not None:
            cur.stop.set()
        return cleared, cur is not None

    def status(self):
        return {
            "speaker": self.speaker_host,
            "session": "up" if (self.session and self.session.healthy()) else "down",
            "playing": self.current.id if self.current else None,
            "playing_preview": (self.current.text[:60] if self.current else None),
            "queue": self._queue.qsize(),
        }

    def get_job(self, job_id):
        with self.jobs_lock:
            return self.jobs.get(job_id)

    def job_view(self, job):
        return {"id": job.id, "status": job.status, "error": job.error,
                "frames": job.frames, "text": job.text[:120],
                "enqueued_at": job.enqueued_at, "started_at": job.started_at,
                "finished_at": job.finished_at}

    def _ensure_session(self, need_media=False):
        if self.session and self.session.healthy():
            if not need_media:
                return self.session
            # An established session whose media channel is gone must be
            # re-opened; a fresh session (media not yet seen) is fine and its
            # channels are awaited in wait_media().
            if self.session.media_seen and not self.session.has_media_channel():
                log("SESSION media channel #3 gone after playback; re-bootstrapping")
            else:
                return self.session
        if self.session:
            log("SESSION stale; closing before re-bootstrap")
            self.session.close()
            self.session = None
        sess = SpeakerSession(self.speaker_host, self.bind_ip,
                              heartbeat_interval=self.heartbeat_interval)
        sess.open()
        self.session = sess
        log("SESSION bootstrapped")
        return sess

    def _loop(self):
        while True:
            job = self._queue.get()
            if job is None:
                break
            if job.status == "stopped":
                continue
            self.current = job
            job.status = "playing"
            job.started_at = time.time()
            for attempt in (1, 2):
                try:
                    sess = self._ensure_session(need_media=True)
                    sess.speak(job)
                    job.status = "stopped" if job.stop.is_set() else "done"
                    break
                except Exception as exc:  # noqa: BLE001 - worker must survive
                    log(f"TTS job {job.id} attempt {attempt} failed: {exc}")
                    if self.session:
                        self.session.close()
                        self.session = None
                    if job.stop.is_set():
                        job.status = "stopped"
                        break
                    # Retry once when nothing was sent yet (channel teardown,
                    # bootstrap race); never replay a half-heard utterance.
                    if attempt == 1 and job.frames == 0:
                        log(f"TTS job {job.id} retrying on a fresh session")
                        continue
                    job.status = "error"
                    job.error = str(exc)[:300]
                    break
            job.finished_at = time.time()
            self.current = None
            log(f"JOB {job.id} -> {job.status} ({job.frames} frames)")

    def shutdown(self):
        self.stop_all()
        self._queue.put(None)
        self._worker.join(timeout=5)
        if self.session:
            self.session.close()
