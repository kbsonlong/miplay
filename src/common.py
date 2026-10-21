"""Clock and bounded event logging shared by the V14 implementation."""
from datetime import datetime
import time

def log(s):
    print(f"[{datetime.now().astimezone().isoformat(timespec='milliseconds')}] {s}", flush=True)

def monotonic_us():
    return time.monotonic_ns()//1000

