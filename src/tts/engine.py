"""Streaming TTS engine: edge-tts MP3 chunks -> FFmpeg -> AAC-LC ADTS frames.

The chain is fully streaming: as soon as edge-tts emits its first MP3 chunks,
FFmpeg decodes and re-encodes them to ADTS, and the generator yields frames
before the whole utterance has been synthesized. The speaker therefore starts
playing roughly one network round-trip + encode latency after the request.
"""
import asyncio
import subprocess
import threading

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


def pump_edge_tts(text, voice, rate, write):
    """Blocking: stream edge-tts MP3 chunks into write(bytes)."""
    import edge_tts  # imported lazily so offline tests do not require it

    async def _run():
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio" and chunk["data"]:
                write(chunk["data"])

    asyncio.run(_run())


def stream_tts_adts(text, voice=DEFAULT_VOICE, rate=DEFAULT_RATE):
    """Generator yielding ADTS frames while the TTS is still synthesizing.

    Runs edge-tts on a helper thread that feeds FFmpeg stdin; frames come off
    FFmpeg stdout as they are encoded. If the network TTS fails mid-stream the
    already-encoded prefix is still yielded, then the TTS error is raised.
    """
    proc = subprocess.Popen(FFMPEG_TTS_CMD, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tts_error = {}

    def _tts_thread():
        try:
            pump_edge_tts(text, voice, rate, proc.stdin.write)
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

    feeder = threading.Thread(target=_tts_thread, daemon=True)
    feeder.start()
    try:
        yield from iter_adts(proc.stdout.read)
        stderr = proc.stderr.read()
        code = proc.wait()
        if code:
            raise RuntimeError(
                f"ffmpeg failed ({code}): {stderr.decode(errors='replace')[:400]}")
        if tts_error:
            raise tts_error["error"]
    finally:
        if proc.poll() is None:
            proc.kill()
        feeder.join(timeout=2)
