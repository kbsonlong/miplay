"""Tests for the streaming TTS pipeline: ADTS parsing, RTP state continuity,
HTTP API shape, and (opt-in, networked) the real edge-tts -> ADTS chain."""
import json
import os
import subprocess
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from src.media.audio import iter_adts, send_adts_stream, RtpStreamState
from src.tts import server as tts_server
from src.tts.priming import send_with_priming, silence_frames
from src.tts.speaker import SpeakerService

HAVE_FFMPEG = subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0


class BytesSocket:
    def __init__(self):
        self.written = []
    def settimeout(self, t):
        pass
    def sendall(self, value):
        self.written.append(value)


def reader_for(data: bytes):
    """read(n) callable over a fixed buffer, mimicking a pipe then EOF."""
    state = {"buf": bytearray(data)}
    def read(n):
        out = bytes(state["buf"][:n])
        del state["buf"][:n]
        return out
    return read


def real_adts_frames(n=6):
    """Generate n real AAC-LC 48kHz stereo ADTS frames via ffmpeg silence."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
           "-t", str(n * 1024 / 48000 + 0.1),
           "-ac", "2", "-ar", "48000", "-c:a", "aac", "-profile:a", "aac_low",
           "-f", "adts", "pipe:1"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    return list(iter_adts(reader_for(out)))


def rtp_of(wire):
    assert wire[:1] == b"$"
    rtp = wire[4:]
    seq = int.from_bytes(rtp[2:4], "big")
    ts = int.from_bytes(rtp[4:8], "big")
    return seq, ts


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg required")
class TestIterAdts(unittest.TestCase):
    def test_parses_ffmpeg_output(self):
        frames = real_adts_frames()
        self.assertGreaterEqual(len(frames), 6)
        for f in frames:
            self.assertTrue(f[0] == 0xFF and (f[1] & 0xF0) == 0xF0)
            flen = ((f[3] & 3) << 11) | (f[4] << 3) | (f[5] >> 5)
            self.assertEqual(flen, len(f))
            # AAC-LC(1) / 48kHz(3) / stereo(2)
            self.assertEqual(f[2] >> 6, 1)
            self.assertEqual((f[2] >> 2) & 15, 3)
            self.assertEqual(((f[2] & 1) << 2) | (f[3] >> 6), 2)

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            list(iter_adts(reader_for(b"\x00" * 64)))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg required")
class TestStreamStateContinuity(unittest.TestCase):
    def test_two_utterances_share_seq_and_advance_pts(self):
        frames = real_adts_frames()
        ready = threading.Event(); ready.set()
        stop = threading.Event()
        # A real session sets time_offset_us = monotonic_us() at PLAY time, so
        # derive the test offset from this machine's clock the same way; using
        # the historical V14 constant would land in a different 2^32 wrap.
        from src.common import monotonic_us
        offset_us = monotonic_us()
        state = RtpStreamState()

        s1 = BytesSocket()
        send_adts_stream(iter(frames), s1, stop, ready, ready, offset_us, state)
        s2 = BytesSocket()
        # keep the second utterance short so pacing sleeps stay small
        send_adts_stream(iter(frames[:3]), s2, stop, ready, ready, offset_us, state)

        self.assertEqual(state.utterance, 2)
        first_seq, first_ts = rtp_of(s1.written[0])
        self.assertEqual(first_seq, 0)
        self.assertEqual(first_ts, offset_us * 90000 // 1_000_000 & 0xFFFFFFFF)
        last_seq1 = rtp_of(s1.written[-1])[0]
        first_seq2, second_ts = rtp_of(s2.written[0])
        # second utterance continues the RTP sequence space
        self.assertEqual(first_seq2, (last_seq1 + 1) & 0xFFFF)
        # and its PTS resyncs to the wall clock: ahead of the first utterance's
        # origin by roughly the elapsed time (a fraction of a second here),
        # measured modulo the 2^32 PTS wrap.
        delta = (second_ts - first_ts) & 0xFFFFFFFF
        self.assertGreater(delta, 0)
        self.assertLess(delta, 5 * 90000)


class TestSpeakerServiceOffline(unittest.TestCase):
    def test_submit_and_stop_without_worker(self):
        svc = SpeakerService(speaker_host="192.0.2.99")  # TEST-NET, never dialed
        job = svc.submit("测试", voice="zh-CN-YunxiNeural")
        self.assertEqual(job.status, "queued")
        self.assertIn(job.id, svc.jobs)
        cleared, had_current = svc.stop_all()
        self.assertEqual(cleared, 1)
        self.assertFalse(had_current)
        self.assertEqual(job.status, "stopped")
        self.assertEqual(svc.status()["session"], "down")

    def test_worker_retries_once_when_channel_gone_before_any_frame(self):
        svc = SpeakerService(speaker_host="192.0.2.99")
        opens = []
        sent = {"n": 0}

        class FakeSession:
            def __init__(self, speaker_host=None, bind_ip=None,
                         heartbeat_interval=0, chime=True):
                opens.append(self)
                self.media_seen = True
                self.calls = 0
            def open(self):
                pass
            def healthy(self):
                return True
            def has_media_channel(self):
                return False  # always torn down -> _ensure_session re-opens
            def close(self):
                pass
            def speak(self, job):
                self.calls += 1
                if self.calls == 1 and sent["n"] == 0:
                    sent["n"] += 1
                    from src.tts.speaker import MediaChannelError
                    raise MediaChannelError("reverse media channel #3 did not arrive")
                job.frames = 5

        import src.tts.speaker as spk
        with patch.object(spk, "SpeakerSession", FakeSession):
            svc.start()
            try:
                job = svc.submit("重试验证")
                deadline = time.monotonic() + 5
                while not job.finished_at and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(job.status, "done")
                self.assertEqual(job.frames, 5)
                self.assertEqual(len(opens), 2)  # initial + retry re-open
            finally:
                svc.shutdown()

    def test_worker_no_retry_after_partial_frames(self):
        from src.tts.speaker import MediaChannelError
        svc = SpeakerService(speaker_host="192.0.2.99")
        opens = []

        class FakeSession:
            def __init__(self, speaker_host=None, bind_ip=None,
                         heartbeat_interval=0, chime=True):
                opens.append(self)
                self.media_seen = True
            def open(self):
                pass
            def healthy(self):
                return True
            def has_media_channel(self):
                return False
            def close(self):
                pass
            def speak(self, job):
                job.frames = 40  # half an utterance already heard
                raise MediaChannelError("teardown mid-utterance")

        import src.tts.speaker as spk
        with patch.object(spk, "SpeakerSession", FakeSession):
            svc.start()
            try:
                job = svc.submit("不重放半句")
                deadline = time.monotonic() + 5
                while not job.finished_at and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(job.status, "error")
                self.assertEqual(len(opens), 1)  # no second bootstrap
            finally:
                svc.shutdown()


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg required")
class TestPriming(unittest.TestCase):
    def _wires(self, sock):
        seqs = []
        pts_list = []
        for w in sock.written:
            assert w[:1] == b"$"
            rtp = w[4:]
            seqs.append(int.from_bytes(rtp[2:4], "big"))
            pts_list.append(int.from_bytes(rtp[4:8], "big"))
        return seqs, pts_list

    def test_silence_frames_profile(self):
        frames = silence_frames(1.0)
        self.assertGreater(len(frames), 40)
        for f in frames:
            self.assertEqual(f[2] >> 6, 1)          # AAC-LC
            self.assertEqual((f[2] >> 2) & 15, 3)   # 48 kHz
            self.assertEqual(((f[2] & 1) << 2) | (f[3] >> 6), 2)  # stereo

    def test_warm_start_sends_priming_then_speech(self):
        frames = real_adts_frames()
        ready = threading.Event(); ready.set()
        stop = threading.Event()
        state = RtpStreamState()
        offset_us = 1792586533_607510  # any monotonic-domain value works
        sock = BytesSocket()
        send_with_priming(iter(frames), sock, stop, ready, ready,
                          offset_us, state)
        seqs, pts_list = self._wires(sock)
        self.assertEqual(state.utterance, 1)
        # RTP sequence strictly continuous across silence+speech
        for a, b in zip(seqs, seqs[1:]):
            self.assertEqual(b, (a + 1) & 0xFFFF)
        # PTS advances in exact 1920 ticks per AU, starting at the origin
        self.assertEqual(pts_list[0], offset_us * 90000 // 1_000_000 & 0xFFFFFFFF)
        for a, b in zip(pts_list, pts_list[1:]):
            self.assertEqual((b - a) & 0xFFFFFFFF, 1920)
        # more AUs on the wire than speech alone (priming + margin included)
        self.assertGreater(len(seqs), len(frames))

    def test_cold_start_waits_for_readiness_before_speech(self):
        frames = real_adts_frames(8)
        ready = threading.Event()
        stop = threading.Event()
        state = RtpStreamState()
        sock = BytesSocket()
        t = threading.Timer(0.6, ready.set)
        t.start()
        try:
            send_with_priming(iter(frames), sock, stop, ready, ready,
                              1792586533_607510, state, ready_timeout=10.0)
        finally:
            t.cancel()
        seqs, pts_list = self._wires(sock)
        total_aus = (pts_list[-1] - pts_list[0]) // 1920 + 1
        # readiness arrived ~0.6 s in: >= 0.6s worth of silence AUs, plus
        # margin, plus all speech frames
        self.assertGreaterEqual(total_aus, int(0.55 / (1024 / 48000)) + len(frames))
        self.assertEqual(state.utterance, 1)

    def test_never_ready_times_out_and_cleans_generator(self):
        ready = threading.Event()  # never set
        stop = threading.Event()
        closed = {"flag": False}

        def speech():
            try:
                for f in real_adts_frames(4):
                    yield f
            finally:
                closed["flag"] = True

        sock = BytesSocket()
        with self.assertRaises(TimeoutError):
            send_with_priming(speech(), sock, stop, ready, ready,
                              1792586533_607510, RtpStreamState(),
                              ready_timeout=0.8)
        self.assertTrue(closed["flag"])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg required")
class TestEngineRobustness(unittest.TestCase):
    def test_stop_wakes_blocked_reader(self):
        from src.tts.engine import stream_tts_adts
        stop = threading.Event()

        def hang_forever(text, voice, rate, write):
            time.sleep(30)  # simulates cloud TTS hang; never writes

        with patch("src.tts.engine.pump_edge_tts", hang_forever):
            gen = stream_tts_adts("x", stop_event=stop, stall_timeout=60)
            t = threading.Timer(0.6, stop.set)
            t.start()
            t0 = time.monotonic()
            frames = list(gen)  # must return after stop, not hang 30 s
            elapsed = time.monotonic() - t0
        self.assertEqual(frames, [])
        self.assertLess(elapsed, 5)

    def test_stall_watchdog_flushes_and_raises(self):
        from src.tts.engine import stream_tts_adts

        def hang_forever(text, voice, rate, write):
            time.sleep(30)

        with patch("src.tts.engine.pump_edge_tts", hang_forever):
            with self.assertRaisesRegex(RuntimeError, "stalled"):
                list(stream_tts_adts("x", stall_timeout=1.5))


class TestHttpApi(unittest.TestCase):
    def _serve(self):
        svc = SpeakerService(speaker_host="192.0.2.99")
        tts_server.Handler.service = svc
        tts_server.Handler.token = ""
        httpd = tts_server.ThreadingHTTPServer(("127.0.0.1", 0), tts_server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return svc, httpd

    def test_endpoints(self):
        svc, httpd = self._serve()
        port = httpd.server_address[1]
        base = f"http://127.0.0.1:{port}"
        try:
            with urllib.request.urlopen(base + "/health") as r:
                self.assertEqual(r.status, 200)
                self.assertEqual(json.loads(r.read())["status"], "ok")
            with urllib.request.urlopen(base + "/voices") as r:
                voices = json.loads(r.read())["voices"]
                self.assertTrue(any(v["id"] == "zh-CN-XiaoxiaoNeural" for v in voices))

            req = urllib.request.Request(
                base + "/tts", data=json.dumps(
                    {"text": "你好音箱", "voice": "zh-CN-YunxiNeural"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req) as r:
                self.assertEqual(r.status, 202)
                job = json.loads(r.read())
            with urllib.request.urlopen(f"{base}/jobs/{job['id']}") as r:
                view = json.loads(r.read())
                self.assertEqual(view["status"], "queued")

            # bad payloads
            for body, expect in (({"text": ""}, 400), ({"text": "x", "rate": "fast"}, 400)):
                req = urllib.request.Request(
                    base + "/tts", data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                try:
                    urllib.request.urlopen(req)
                    self.fail(f"{body} should have failed")
                except urllib.error.HTTPError as e:
                    self.assertEqual(e.code, expect)

            req = urllib.request.Request(base + "/stop", data=b"{}", method="POST")
            with urllib.request.urlopen(req) as r:
                stopped = json.loads(r.read())
            self.assertEqual(stopped["cleared_queued"], 1)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_token_required_when_configured(self):
        svc = SpeakerService(speaker_host="192.0.2.99")
        tts_server.Handler.service = svc
        tts_server.Handler.token = "s3cret"
        httpd = tts_server.ThreadingHTTPServer(("127.0.0.1", 0), tts_server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            req = urllib.request.Request(base + "/tts", data=b'{"text":"x"}', method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 401)
            req = urllib.request.Request(base + "/tts", data=b'{"text":"x"}',
                                         headers={"X-Token": "s3cret"}, method="POST")
            with urllib.request.urlopen(req) as r:
                self.assertEqual(r.status, 202)
        finally:
            httpd.shutdown()
            httpd.server_close()


@unittest.skipUnless(os.environ.get("MIPLAY_TTS_LIVE") == "1",
                     "set MIPLAY_TTS_LIVE=1 (needs network) to run")
class TestLiveTtsChain(unittest.TestCase):
    def test_stream_first_frame_before_synth_completes(self):
        from src.tts.engine import stream_tts_adts
        text = ("这是流式语音合成链路的自动化验证，内容长度足以超过几秒钟，"
                "用来确认首帧在合成完成之前就已经产出。")
        t0 = time.monotonic()
        first = None
        n = 0
        for _frame in stream_tts_adts(text):
            if first is None:
                first = time.monotonic() - t0
            n += 1
        total = time.monotonic() - t0
        audio = n * 1024 / 48000
        self.assertGreater(n, 100)
        self.assertLess(first, 5.0)
        self.assertLess(total, audio)  # chain must be streaming, not batch


if __name__ == "__main__":
    unittest.main()
