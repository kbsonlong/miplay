"""Command-line entry point. Defaults to DRY RUN; Open requires explicit opt-in."""
import argparse
from pathlib import Path
import sys
from .common import log
from .control.client import HOST, CONTROL_PORT, RTSP_PORT, TIMER_PORT, run

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
    p.add_argument("--trigger-open",action="store_true",help="explicitly send SetPlaySource/Open after readiness checks")
    p.add_argument("--audio-file", help="local WAV/AIFF/MP3/etc file; requires --trigger-open and ffmpeg")
    a=p.parse_args()
    if a.audio_file and not a.trigger_open:
        p.error("--audio-file requires --trigger-open")
    if a.audio_file and not Path(a.audio_file).is_file():
        p.error(f"audio file not found: {a.audio_file}")
    if a.observe<0 or a.hold<0: p.error("observe/hold must be nonnegative")
    if not a.connect:
        log("DRY RUN; no sockets opened")
        log(f"will bind TCP {a.bind_ip}:{a.rtsp_port} and UDP {a.bind_ip}:{a.timer_port}")
        log("No SetPlaySource/Open is sent unless --connect --confirm-host 192.168.3.4 --trigger-open is specified.")
        log("RTSP is implemented; optional AAC/RTP sender is experimental and requires --audio-file.")
        return 0
    return run(a)

if __name__ == '__main__':
    sys.exit(main())
