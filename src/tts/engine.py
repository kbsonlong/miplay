"""Streaming TTS engine: edge-tts MP3 chunks -> FFmpeg -> AAC-LC ADTS frames.

The chain is fully streaming: as soon as edge-tts emits its first MP3 chunks,
FFmpeg decodes and re-encodes them to ADTS, and the generator yields frames
before the whole utterance has been synthesized. The speaker therefore starts
playing roughly one network round-trip + encode latency after the request.

Robustness: a stop_event makes the stdout reader wake within ~0.2 s instead of
blocking on FFmpeg, and a stall watchdog closes FFmpeg stdin when edge-tts
produces no bytes for stall_timeout seconds (cloud TTS hang -> partial audio
plus an error, never a wedged worker thread).
"""
import asyncio
import select
import subprocess
import threading
import time

from ..common import log
from ..media.audio import iter_adts

# Low-latency decode of the MP3 stream from edge-tts; output profile is fixed
# to AAC-LC 48 kHz stereo 256 kb/s because iter_adts and the receiver both
# require exactly that profile.
FFMPEG_TTS_CMD = [
    "ffmpeg", "-hide_banner", "-loglevel", "error",
    "-fflags", "nobuffer", "-flags", "low_delay", "-probesize", "4096",
    "-analyzeduration", "0", "-f", "mp3", "-i", "pipe:0",
    "-vn", "-ac", "2", "-ar", "48000", "-c:a", "aac", "-profile:a", "aac_low",
    "-b:a", "256k", "-f", "adts", "pipe:1",
]

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_RATE = "+0%"


class _StoppableReader:
    """read(n) over a pipe that also wakes on stop_event or EOF."""

    def __init__(self, pipe, stop_event, poll=0.2):
        self.pipe = pipe
        self.stop_event = stop_event
        self.poll = poll

    def read(self, n):
        while True:
            if self.stop_event is not None and self.stop_event.is_set():
                return b""
            ready, _, _ = select.select([self.pipe], [], [], self.poll)
            if ready:
                chunk = self.pipe.read1(n)
                if not chunk:
                    return b""
                return chunk


def pump_edge_tts(text, voice, rate, write):
    """Blocking: stream edge-tts MP3 chunks into write(bytes)."""
    import edge_tts  # imported lazily so offline tests do not require it

    async def _run():
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio" and chunk["data"]:
                write(chunk["data"])

    asyncio.run(_run())


def stream_tts_adts(text, voice=DEFAULT_VOICE, rate=DEFAULT_RATE,
                    stop_event=None, stall_timeout=30.0):
    """Generator yielding ADTS frames while the TTS is still synthesizing.

    Runs edge-tts on a helper thread that feeds FFmpeg stdin; frames come off
    FFmpeg stdout as they are encoded. If the network TTS fails mid-stream the
    already-encoded prefix is still yielded, then the TTS error is raised.
    A stalled feed (no bytes for stall_timeout) closes stdin the same way so
    the encoded prefix flushes and the caller gets a RuntimeError afterwards.
    """
    proc = subprocess.Popen(FFMPEG_TTS_CMD, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tts_error = {}
    stalled = {}
    progress = {"t": time.monotonic()}
    watchdog_stop = threading.Event()

    def _write(data):
        progress["t"] = time.monotonic()
        proc.stdin.write(data)

    def _tts_thread():
        try:
            pump_edge_tts(text, voice, rate, _write)
        except BrokenPipeError:
            pass  # reader went away (stop/interrupt); nothing more to feed
        except Exception as exc:
            tts_error["error"] = exc
            log(f"TTS engine error: {exc}")
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def _watchdog():
        while not watchdog_stop.wait(1.0):
            if time.monotonic() - progress["t"] > stall_timeout:
                stalled["flag"] = True
                log(f"TTS stalled: no data for {stall_timeout:.0f}s; flushing partial audio")
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                return

    feeder = threading.Thread(target=_tts_thread, daemon=True)
    feeder.start()
    watchdog = threading.Thread(target=_watchdog, daemon=True)
    watchdog.start()
    interrupted = stop_event is not None and stop_event.is_set()
    try:
        yield from iter_adts(_StoppableReader(proc.stdout, stop_event).read)
        interrupted = stop_event is not None and stop_event.is_set()
        if interrupted:
            # Reader hit stop; end FFmpeg promptly instead of waiting for it.
            try:
                proc.stdin.close()
            except OSError:
                pass
            if proc.poll() is None:
                time.sleep(0.1)
                if proc.poll() is None:
                    proc.kill()
        stderr = proc.stderr.read()
        code = proc.wait()
        if not interrupted:
            if "error" in tts_error:
                raise tts_error["error"]
            if stalled:
                raise RuntimeError(f"TTS stalled: no audio data for {stall_timeout:.0f}s")
            if code:
                raise RuntimeError(
                    f"ffmpeg failed ({code}): {stderr.decode(errors='replace')[:400]}")
    finally:
        watchdog_stop.set()
        if proc.poll() is None:
            proc.kill()
        feeder.join(timeout=2)
