"""HTTP API for Agent-driven streaming TTS on the L15A speaker.

Stdlib-only server; run with:

    python3 -m src.tts.server --speaker 192.168.3.4 --listen 0.0.0.0:9901

Endpoints:
    GET  /health      service/session/queue snapshot
    GET  /voices      curated voice list (edge-tts)
    POST /tts         {"text": "...", "voice": "...", "rate": "+0%", "blocking": false}
    GET  /jobs/<id>   job status
    POST /stop        interrupt current utterance and clear the queue

LAN-only by design; add --token <secret> to require X-Token on writes.
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..common import log
from .speaker import SpeakerService
from .engine import DEFAULT_VOICE, DEFAULT_RATE

VOICES = [
    {"id": "zh-CN-XiaoxiaoNeural", "lang": "zh-CN", "gender": "female", "note": "default, warm"},
    {"id": "zh-CN-XiaoyiNeural", "lang": "zh-CN", "gender": "female", "note": "lively"},
    {"id": "zh-CN-YunxiNeural", "lang": "zh-CN", "gender": "male", "note": "sunshine"},
    {"id": "zh-CN-YunjianNeural", "lang": "zh-CN", "gender": "male", "note": "narration"},
    {"id": "zh-CN-YunxiaNeural", "lang": "zh-CN", "gender": "male", "note": "child"},
    {"id": "en-US-AriaNeural", "lang": "en-US", "gender": "female", "note": ""},
    {"id": "en-US-GuyNeural", "lang": "en-US", "gender": "male", "note": ""},
]
RATE_RE = re.compile(r"^[+-]\d{1,3}%$")
MAX_TEXT = 2000
BLOCK_TIMEOUT = 300


class Handler(BaseHTTPRequestHandler):
    service: SpeakerService = None  # type: ignore[assignment]
    token: str = ""

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        log(f"HTTP {self.address_string()} {format % args}")

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        return not self.token or self.headers.get("X-Token") == self.token

    # ------------------------------------------------------------------ GET

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            self._json(200, {"status": "ok", **self.service.status()})
        elif path == "/voices":
            self._json(200, {"voices": VOICES, "default": DEFAULT_VOICE})
        elif path == "/discover":
            from ..discovery import scan_lan, default_base
            base = default_base()
            found = scan_lan(base)
            speakers = [d["ip"] for d in found if d["kind"] == "speaker"]
            if speakers:
                previous = self.service.speaker_host
                self.service.speaker_host = speakers[0]
                if previous != speakers[0] and self.service.session:
                    # Force a fresh bootstrap against the new target.
                    self.service.session.close()
                    self.service.session = None
                log(f"HTTP /discover switched speaker {previous} -> {speakers[0]}")
            responders = [{"ip": d["ip"], "kind": d["kind"],
                           "raw_hex": d["raw"].hex()} for d in found]
            self._json(200, {"base": base, "speakers": speakers,
                             "all_responders": responders})
        elif path.startswith("/jobs/"):
            job = self.service.get_job(path.split("/jobs/", 1)[1])
            if job is None:
                self._json(404, {"error": "unknown job"})
            else:
                self._json(200, self.service.job_view(job))
        else:
            self._json(404, {"error": "not found"})

    # ----------------------------------------------------------------- POST

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if not self._authorized():
            self._json(401, {"error": "bad or missing X-Token"})
            return
        if path == "/stop":
            cleared, had_current = self.service.stop_all()
            self._json(200, {"stopped_current": had_current, "cleared_queued": cleared})
            return
        if path != "/tts":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "invalid JSON body"})
            return
        text = str(payload.get("text", "")).strip()
        voice = str(payload.get("voice") or DEFAULT_VOICE)
        rate = str(payload.get("rate") or DEFAULT_RATE)
        blocking = bool(payload.get("blocking", False))
        chime = payload.get("chime")
        if chime is not None and not isinstance(chime, bool):
            self._json(400, {"error": "chime must be true/false"})
            return
        if not text:
            self._json(400, {"error": "text is required"})
            return
        if len(text) > MAX_TEXT:
            self._json(400, {"error": f"text too long (max {MAX_TEXT} chars)"})
            return
        if not RATE_RE.match(rate):
            self._json(400, {"error": "rate must look like +10% / -5%"})
            return
        job = self.service.submit(text, voice=voice, rate=rate, chime=chime)
        log(f"HTTP /tts accepted job={job.id} voice={voice} len={len(text)}")
        if not blocking:
            self._json(202, {"id": job.id, "status": "queued", **self.service.status()})
            return
        deadline = time.monotonic() + BLOCK_TIMEOUT
        while not job.finished_at and time.monotonic() < deadline:
            time.sleep(0.05)
        self._json(200, self.service.job_view(job))


def resolve_speaker(speaker_arg: str) -> str:
    """Resolve --speaker; 'auto' scans the LAN for a genuine L15A."""
    if speaker_arg != "auto":
        return speaker_arg
    from ..discovery import discover_speakers, default_base
    base = default_base()
    ips = discover_speakers(base)
    if not ips:
        raise SystemExit(f"--speaker auto: no L15A speaker found on {base}.0/24")
    if len(ips) > 1:
        log(f"AUTO multiple speakers found {ips}; using {ips[0]}")
    else:
        log(f"AUTO discovered speaker {ips[0]} on {base}.0/24")
    return ips[0]


def serve(listen_host, listen_port, speaker_host, bind_ip, token,
          heartbeat_interval, voice, chime):
    speaker_host = resolve_speaker(speaker_host)
    service = SpeakerService(speaker_host=speaker_host, bind_ip=bind_ip,
                             heartbeat_interval=heartbeat_interval, chime=chime)
    service.start()
    Handler.service = service
    Handler.token = token
    httpd = ThreadingHTTPServer((listen_host, listen_port), Handler)
    log(f"HTTP listening on {listen_host}:{listen_port} "
        f"(speaker={speaker_host}, default voice={voice}, chime={chime})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("HTTP interrupted; shutting down")
    finally:
        service.shutdown()
        httpd.server_close()


def main():
    p = argparse.ArgumentParser(description="MiPlay streaming TTS server for agents")
    p.add_argument("--listen", default="0.0.0.0:9901", help="host:port for the HTTP API")
    p.add_argument("--speaker", default="192.168.3.4",
                   help="L15A MiPlay control address, or 'auto' to discover "
                        "on the LAN at startup")
    p.add_argument("--bind-ip", default="192.168.3.18", help="local IP the speaker dials back to")
    p.add_argument("--token", default="", help="require this X-Token on POST endpoints")
    p.add_argument("--heartbeat", type=float, default=10.0,
                   help="control heartbeat seconds; 0 disables")
    p.add_argument("--chime", action=argparse.BooleanOptionalAction, default=True,
                   help="play a short attention chime before each message "
                        "(recommended: the receiver drops ~0.5s at speech onset)")
    p.add_argument("--voice", default=DEFAULT_VOICE, help="default voice for /tts")
    a = p.parse_args()
    host, _, port = a.listen.rpartition(":")
    if not host or not port.isdigit():
        p.error("--listen must be host:port")
    serve(host, int(port), a.speaker, a.bind_ip, a.token, a.heartbeat,
          a.voice, a.chime)


if __name__ == "__main__":
    main()
