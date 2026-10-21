"""Priming-aware paced sender for TTS utterances.

Why this exists: the receiver DISCARDS audio that arrives before it reports
ready (first-audiopcm=1 + state=2, ~2.4 s after first media on a cold
session). V14 got away with it because a music file's first 213 ms are
expendable; a short TTS notification is not ("测试" vanished in live use).

Strategy, for EVERY utterance (cold or warm):
  Phase A - send paced silence while (a) the receiver becomes ready and
            (b) the TTS chain is already producing its first frame (the
            speech iterator is prefetched on a helper thread in parallel);
  Phase B - a short settle margin of silence frames;
  Phase C - send speech frames paced; PTS/seq/TS counters continue from the
            silence, so the receiver timeline is seamless.

Warm sessions exit Phase A as soon as the first TTS frame is buffered, so
the silence lead-in merely covers TTS first-frame latency (~2 s) instead of
starving the receiver. Silence is inaudible: perceived latency is unchanged,
but no syllable is ever dropped.

Packetization mirrors send_adts_stream in src/media/audio.py (V14-verified
layout: PAT/PMT/PCR cadence, PES/TS, 7-TS RTP fragments, PT 33); the V14
function itself stays untouched and keeps serving file playback.
"""
from __future__ import annotations

import queue
import struct
import subprocess
import threading
import time

from ..common import log, monotonic_us
from ..media.mpegts import make_pat, make_pmt, make_pcr_packet, pes_to_ts
from ..media.audio import iter_adts, RtpStreamState

AU_SEC = 1024 / 48000  # one AAC access unit at 48 kHz

_SILENCE_CACHE: dict[str, list[bytes]] = {}


def _reader_for(data: bytes):
    state = {"buf": bytearray(data)}

    def read(n):
        out = bytes(state["buf"][:n])
        del state["buf"][:n]
        return out

    return read


def _adts_from_lavfi(filter_expr: bytes | str, seconds: float) -> list[bytes]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", filter_expr, "-t", str(seconds),
           "-ac", "2", "-ar", "48000", "-c:a", "aac", "-profile:a", "aac_low",
           "-b:a", "256k", "-f", "adts", "pipe:1"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    return list(iter_adts(_reader_for(out)))


def silence_frames(seconds: float = 3.0) -> list[bytes]:
    """Cached AAC-LC 48 kHz stereo silence ADTS frames (ffmpeg anullsrc)."""
    key = f"silence-{seconds}"
    if key not in _SILENCE_CACHE:
        _SILENCE_CACHE[key] = _adts_from_lavfi(
            f"anullsrc=r=48000:cl=stereo", seconds)
    return _SILENCE_CACHE[key]


CHIME_SECONDS = 0.8


def chime_frames() -> list[bytes]:
    """Cached attention chime: two 0.3 s 660/880 Hz beeps separated by 0.2 s.

    Live evidence (2026-10-21): the receiver stays state=2 through long silent
    priming yet still drops the first ~0.5 s of SPEECH - the playout pipeline
    only joins on non-silent audio and discards the join window. A chime lead
    sacrifices itself to that window so the message survives.
    """
    key = "chime"
    if key not in _SILENCE_CACHE:
        _SILENCE_CACHE[key] = _adts_from_lavfi(
            "aevalsrc=0.28*sin(2*PI*880*t)*between(mod(t\\,0.5)\\,0\\,0.3)"
            "+0.28*sin(2*PI*660*t)*between(mod(t\\,0.5)\\,0.3\\,0.5):d=0.8:s=48000",
            CHIME_SECONDS)
    return _SILENCE_CACHE[key]


class Prefetcher:
    """Pulls an iterator into a bounded queue on a helper thread.

    Gives Phase A something to wait on: the first speech frame is being
    synthesized while silence primes the receiver. Backpressure is natural -
    when the consumer paces at realtime and TTS runs ~5x faster, the queue
    fills and the producer stops pulling, which parks the ffmpeg chain.
    """

    def __init__(self, frames_iter, stop_event, maxsize=32):
        self.it = frames_iter
        self.stop = stop_event
        self.q: queue.Queue = queue.Queue(maxsize=maxsize)
        self.finished = threading.Event()
        self.error = None
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            for f in self.it:
                while True:
                    if self.stop.is_set():
                        return
                    try:
                        self.q.put(f, timeout=0.2)
                        break
                    except queue.Full:
                        continue
            self.finished.set()
        except Exception as exc:  # noqa: BLE001 - surfaced via .error
            self.error = exc

    def start(self):
        self._t.start()

    def has_frame(self):
        return self.q.qsize() > 0

    def next(self, timeout=0.5):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def cancel(self):
        """Close the underlying generator so its cleanup (ffmpeg kill) runs."""
        try:
            self.it.close()  # type: ignore[attr-defined]
        except Exception:
            pass


def send_with_priming(speech_iter, media_sock, stop_event,
                      first_pcm_ready, receiver_playing_ready,
                      time_offset_us, state: RtpStreamState,
                      margin_frames: int = 8, ready_timeout: float = 20.0,
                      lead_frames: list[bytes] | None = None):
    """Send one utterance with silence priming; blocks until finished."""
    if time_offset_us is None:
        raise ValueError("RTSP TIME_OFFSET is unavailable; refusing unaligned media")
    media_sock.settimeout(5)
    sil = silence_frames()
    sil_i = 0

    def next_silence():
        nonlocal sil_i
        f = sil[sil_i % len(sil)]
        sil_i += 1
        return f

    seq = state.seq; cc_audio = state.cc_audio
    cc_pat = state.cc_pat; cc_pmt = state.cc_pmt
    if state.utterance == 0:
        pts = (time_offset_us * 90000 // 1_000_000) & 0xffffffff
    else:
        pts = (monotonic_us() * 90000 // 1_000_000) & 0xffffffff
    total_au = 0
    sent = [0]

    def send_one(frame):
        nonlocal seq, cc_audio, cc_pat, cc_pmt, pts, total_au
        include_tables = (total_au == 0 or total_au >= 13 and (total_au - 13) % 5 == 0)
        packets = []
        if include_tables:
            now_us = time.monotonic_ns() // 1000
            pcr27 = now_us * 27
            live_pcr = (pcr27 // 300) & ((1 << 33) - 1)
            pcr_ext = pcr27 % 300
            packets.extend([make_pat(cc_pat), make_pmt(cc_pmt),
                            make_pcr_packet(live_pcr, pcr_ext)])
            cc_pat = (cc_pat + 1) & 15; cc_pmt = (cc_pmt + 1) & 15
        ts, cc_audio = pes_to_ts(frame, pts, cc_audio)
        packets.extend(ts)
        for off in range(0, len(packets), 7):
            fragment = packets[off:off + 7]
            payload = b"".join(fragment)
            marker = 0x80 if off + len(fragment) >= len(packets) else 0x00
            rtp = bytes([0x80, marker | 33]) + struct.pack(">HII", seq, pts, 0xdeadbeef) + payload
            wire = b"$" + len(rtp).to_bytes(3, "big") + rtp
            media_sock.sendall(wire)
            seq = (seq + 1) & 0xffff; sent[0] += len(wire)
        pts = (pts + 1920) & 0xffffffff
        total_au += 1

    def pace(t0, au):
        target = t0 + au * AU_SEC
        delay = target - time.monotonic()
        if delay > 0:
            stop_event.wait(delay)

    pre = Prefetcher(speech_iter, stop_event)
    pre.start()
    speech_au = 0
    try:
        # Phase A: silence until receiver ready AND first speech frame on hand.
        t0 = time.monotonic(); au = 0
        ready_at_start = first_pcm_ready.is_set() and receiver_playing_ready.is_set()
        while True:
            if stop_event.is_set():
                break
            send_one(next_silence()); au += 1
            pace(t0, au)
            if pre.error is not None:
                raise pre.error
            if (first_pcm_ready.is_set() and receiver_playing_ready.is_set()
                    and (pre.has_frame() or pre.finished.is_set())):
                break
            if time.monotonic() - t0 > ready_timeout:
                raise TimeoutError(
                    f"priming exceeded {ready_timeout:.0f}s "
                    f"(ready={first_pcm_ready.is_set() and receiver_playing_ready.is_set()}, "
                    f"tts_frame={pre.has_frame()})")
        # Phase B: settle margin of silence.
        for _ in range(margin_frames):
            if stop_event.is_set():
                break
            send_one(next_silence()); au += 1
            pace(t0, au)
        # Phase C: lead-in (attention chime) then speech, paced on one origin.
        lead = lead_frames or []
        lead_au = 0
        t1 = time.monotonic()
        for lead_frame in lead:
            if stop_event.is_set():
                break
            send_one(lead_frame); lead_au += 1
            pace(t1, lead_au)
        while True:
            if stop_event.is_set():
                break
            frame = pre.next()
            if frame is None:
                if pre.error is not None:
                    raise pre.error
                if pre.finished.is_set():
                    break
                continue
            send_one(frame); speech_au += 1
            pace(t1, lead_au + speech_au)
        log(f"PRIME sent {au} silence + {lead_au} lead + {speech_au} speech AUs "
            f"/ {sent[0]} wire bytes (utterance {state.utterance + 1}, "
            f"warm_start={ready_at_start})")
    finally:
        state.seq = seq; state.cc_audio = cc_audio
        state.cc_pat = cc_pat; state.cc_pmt = cc_pmt
        state.utterance += 1
        pre.cancel()
