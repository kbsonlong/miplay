---
name: miplay-device-reverse-engineering
version: 1.0
author: kbsonlong
license: MIT
description: "Use when discovering or reverse-engineering a new Xiaomi MiPlay speaker on the LAN."
tags: [miplay, l15a, l16b, discovery, mdns, tcpdump, rtsp, audio]
---

# Xiaomi MiPlay speaker discovery and protocol investigation

## Purpose

Use this skill when a new Xiaomi smart speaker appears on the LAN and must be:

- discovered safely;
- identified as MiPlay, AirPlay/RAOP, or an unrelated device;
- compared with the verified L15A implementation;
- reverse-engineered through the Legacy control, RTSP, TimerService, and AAC media layers;
- validated with a real test utterance;
- documented with a reproducible PCAP and logs.

This is an experimental reverse-engineering workflow for devices the operator owns or is authorized to test. MiPlay is not a public interoperable standard. Do not expose TCP/8899, RTSP, or media listeners outside the trusted LAN.

## Known verified baseline

Repository: `~/miplay` → `github.com/kbsonlong/miplay`

Runtime:

- Python: `~/.pyenv/shims/python3` (edge-tts is installed there)
- FFmpeg: `/opt/homebrew/bin/ffmpeg`
- Historical verified L15A case: one private-LAN address, firmware `1.92.16`
- Historical verified source case: one private-LAN address on macOS
- MiPlay control: speaker TCP `8899`
- Reverse RTSP/media listener: source TCP `7274`
- TimerService: source UDP `36524`

Never copy historical IPs into a new Agent or device investigation. The
Agent must derive its own source interfaces, routes, multicast interface, and
candidate LAN CIDRs at runtime.

The L15A audio path is verified end-to-end. The L16B path was subsequently verified end-to-end with a short test utterance, but do not generalize that result to every Xiaomi model or firmware.

## Safety and authorization gates

Before a real-device test:

1. Confirm the target IP/model with the user.
2. Run discovery and read-only challenge/DeviceInfo probes first.
3. Keep the known-good L15A baseline available.
4. Bind reverse listeners to the correct source LAN IP and verify ports are free.
5. Do not send `SetPlaySource`, `Open`, `SetMediaInfo`, or audio until the user explicitly authorizes a physical playback test.
6. Never store or publish DeviceInfo payloads containing account IDs, device IDs, house IDs, serials, or Bluetooth addresses without redaction.
7. Stop temporary probe servers and remove temporary PCAP/audio files after the test unless they are intentionally retained as private evidence.

A successful control handshake is not proof of audio compatibility. A job returning `done` is not proof the user heard the complete sentence; always ask for an audible confirmation.

## Phase 1: Discover the LAN device

### 1. Derive local interfaces, routes, and LAN CIDRs

```bash
ifconfig
route -n get default
route -n get 224.0.0.251
netstat -rn -f inet
```

Use the default route and interface addresses/netmasks to identify local
candidate LANs. Prefer the route to `224.0.0.251` for the mDNS interface.
Do not assume `/24`: preserve the actual netmask/CIDR and scan only the
resulting private/link-local LAN candidates. Exclude loopback, multicast,
point-to-point, VPN, and public interfaces unless explicitly scoped.

The project derives these networks automatically:

```python
from src.discovery import discovery_networks, local_ips, local_networks
print(local_ips())
print(discovery_networks())
print(local_networks())
```

### 2. Browse mDNS/Bonjour without sending control traffic

Use macOS Bonjour tools:

```bash
dns-sd -B _services._dns-sd._udp local
dns-sd -B _airplay._tcp local
dns-sd -B _raop._tcp local
dns-sd -B _miio._udp local
dns-sd -B _googlecast._tcp local
dns-sd -B _cast._tcp local
dns-sd -B _sonos._tcp local
dns-sd -B _dlna._tcp local
```

Resolve a discovered instance (replace placeholders with the observed data):

```bash
dns-sd -L '<instance-name>' _airplay._tcp local
dns-sd -L '<raop-instance-name>' _raop._tcp local
dscacheutil -q host -a name <discovered-host>.local
```

Record instance, hostname, IP, port, model, firmware, MAC/device ID, and TXT keys. mDNS is useful but model-dependent: the verified L16B broadcasts `_airplay._tcp` and `_raop._tcp` on port 7000; the verified L15A did not expose a useful standard mDNS speaker service. mDNS absence is not proof that a speaker is absent.

### 3. Probe MiPlay TCP/8899

The project CLI is:

```bash
cd ~/miplay
~/.pyenv/shims/python3 -m src.discovery --timeout 0.8
```

The command derives all eligible local LAN CIDRs automatically. Use
`--base <CIDR-or-prefix>` only for a deliberate, user-approved override.

The scanner:

- concurrently connects to TCP port 8899 on every derived LAN candidate;
- sends a minimal `$`-framed probe;
- parses the first frame;
- skips local IPs;
- classifies by protocol shape, not merely an open port;
- for newer candidates performs challenge/HMAC/version/DeviceInfo confirmation.

The MiPlay frame header is:

```text
'$' (1 byte)
command (big-endian uint16)
sequence (big-endian uint16)
payload length (big-endian uint32)
payload
```

Equivalent Python framing:

```python
b'$' + struct.pack('>HHI', command, sequence, len(payload)) + payload
```

Observed classifications:

```text
L15A: 0x0028 challenge, 16/17 ASCII decimal digits -> speaker (verified media path)
L16B: 0x0028 challenge, 12/13 ASCII decimal digits -> miplay-candidate
miplay-br local bridge: 14 ASCII digits -> miplay-like, do not select
other response: unknown
```

Do not assume challenge length is a universal Xiaomi standard. It is an observed firmware/model fingerprint. New firmware may use another length; preserve the raw frame and investigate before changing the classifier.

### 4. Query model information safely

Use the read-only DeviceInfo probe:

```python
from src.discovery import probe_device_info
print(probe_device_info('<discovered-speaker-ip>'))
```

The probe performs only:

```text
RX 0x0028 challenge
TX 0x0036 SourceVersion
TX 0x0029 legacy HMAC-SHA1
TX 0x001e GetDeviceInfo
RX 0x0037 version
RX 0x001f DeviceInfo
```

It must stop before `SetPlaySource` (`0x0040`) and `Open` (`0x0000`). Redact sensitive fields in shared reports.

## Phase 2: Capture before interpreting

### 1. Capture command

Use the route-selected interface and classic PCAP format:

```bash
cd ~/miplay
sudo ./examples/capture.sh /tmp/miplay-device.pcap
```

The script expands to:

```bash
HOST=<discovered-speaker-ip>
IFACE="$(route -n get "$HOST" | awk '/interface:/{print $2; exit}')"
sudo tcpdump -i "$IFACE" -nn -s 0 -U -w /tmp/miplay-device.pcap \
  "host $HOST and (tcp port 8899 or tcp port 7274 or udp port 36524)"
```

For mDNS/AirPlay discovery, use a separate capture filter:

```bash
sudo tcpdump -i "$IFACE" -nn -s 0 -U -w /tmp/miplay-mdns.pcap \
  "host $HOST or (udp port 5353) or (tcp port 7000)"
```

Use `-s 0` to avoid truncating payloads and `-U` for packet-buffered writes. PCAPs can contain private identifiers and audio; keep them local and delete them when no longer needed.

### 2. Stop at meaningful protocol boundaries

Capture these separate experiments rather than one ambiguous long run:

1. fresh TCP/8899 connection: challenge/auth/version/DeviceInfo;
2. read-only status queries;
3. optional playback refresh and heartbeat;
4. `SetPlaySource/Open` with reverse listeners ready;
5. RTSP until `TIME_OFFSET`;
6. `SetMediaInfo` and a very short known test audio;
7. teardown/idle behavior.

Write down the exact timestamp and command sequence for each phase.

## Phase 3: L15A-style protocol ladder

### Control handshake

Use the existing implementation as the reference, but do not assume response command codes are identical:

```text
RX 0x0028 challenge
TX 0x0036 SourceVersion = b'1.0.1123012\\0'
TX 0x0029 HMAC-SHA1
TX 0x001e GetDeviceInfo seq=1
RX 0x0037 version seq=0
TX 0x0058 sourceName seq=2
RX 0x0059 sourceName ACK
RX 0x001f DeviceInfo seq=1
```

Legacy HMAC implementation:

```python
key = hashlib.md5(b'0.0.0.0').hexdigest().encode('ascii')
hmac.new(key, challenge, hashlib.sha1).hexdigest().encode('ascii')
```

Preserve the challenge bytes exactly. Do not normalize or convert them to an integer before HMAC.

### Read-only status and refresh

The L15A sequence uses:

```text
TX 0x0058 seq=3 {"isSameAccount":0}
TX 0x0034 seq=4
TX 0x000e seq=5
TX 0x001c seq=6
TX 0x0014 seq=7
TX 0x0058 seq=8 sourceName
TX 0x001e seq=9
TX 0x0058 seq=10 {"isSameAccount":0}
TX 0x0034 seq=11
TX 0x001a seq=12 heartbeat
RX 0x001b seq=12 heartbeat ACK
```

Observed L16B differences:

```text
L16B responds to 0x000e with 0x000f
L16B responds to 0x001c with 0x001d
```

Treat these as model/firmware response variants. Log the actual `(command, sequence, payload)` instead of rejecting a device solely because the response command differs.

### Open and reverse channels

Before `Open`, bind:

```text
TCP 7274
UDP 36524
```

Then send:

```text
TX 0x0040 seq=13 SetPlaySource
TX 0x0000 seq=14 wfd://<source-ip>:7274?mirrorMode=1\\0
```

The receiver dials back to source TCP/7274:

```text
reverse #1: RTSP control
reverse #2: auxiliary/held channel
reverse #3: media TCP destination
```

Channel number is observed by accept order on current devices; do not assume it is universally stable without a PCAP/log check.

### TimerService

Receiver sends UDP requests to source UDP/36524. Current implementation replies with 40 bytes:

```text
copy request bytes
bytes [16:24] = send monotonic_us, little-endian uint64
bytes [24:32] = receive monotonic_us, little-endian uint64
```

A working UDP timer exchange is necessary but does not prove media playback readiness.

### RTSP/WFD sequence

Observed L15A and L16B sequence:

```text
source -> receiver: OPTIONS * RTSP/1.0
receiver -> source: 200 OK
receiver -> source: OPTIONS * RTSP/1.0
source -> receiver: 200 OK
source -> receiver: GET_PARAMETER capabilities
receiver -> source: 200 OK (AAC capability)
source -> receiver: SET_PARAMETER selected AAC-only parameters
receiver -> source: 200 OK
source -> receiver: SET_PARAMETER wfd_trigger_method: SETUP
receiver -> source: SETUP
source -> receiver: 200 OK
receiver -> source: PLAY
source -> receiver: 200 PLAY + TIME_OFFSET
receiver -> source: 200 TIME_OFFSET
```

Required selected capability body:

```text
wfd_audio_codecs: AAC 00000001 00
wfd_client_rtp_ports: RTP/AVP/TCP;interleaved mode=play
wfd_presentation_URL: rtsp://<source-ip>/wfd1.0/streamid=0 none
```

Declare `rtsp_ready` only after `PLAY` and acknowledged `TIME_OFFSET`, not merely after `SETUP` or `PLAY 200`.

## Phase 4: Send a test utterance

Use a temporary server bound to the target IP:

```bash
cd ~/miplay
~/.pyenv/shims/python3 -m src.tts.server \
  --listen 127.0.0.1:9902 \
  --speaker <discovered-speaker-ip> \
  --bind-ip <source-ip-derived-from-route>
```

Submit a short, unambiguous sentence:

```bash
curl -s -X POST http://127.0.0.1:9902/tts \
  -H 'Content-Type: application/json' \
  -d '{"text":"这是新音箱的测试语音，第一句到此结束。","blocking":true}'
```

Require all of these before claiming success:

```text
job status = done
SET_MEDIA_INFO logged
first-audiopcm=1
state=2
PRIME/AUDIO sent speech AUs > 0
user confirms the whole sentence was heard
```

The current audio path is:

```text
TTS MP3 stream
  -> FFmpeg AAC-LC 48 kHz stereo ADTS
  -> PES/MPEG-TS
  -> RTP PT=33 over reverse TCP #3
```

Do not add FFmpeg input latency flags such as `-fflags nobuffer`, `-flags low_delay`, `-probesize`, or `-analyzeduration` to the pipe-input command. They were experimentally shown to discard 0.4–1.0 seconds from the MP3 front. The exact symptom was Chinese speech losing the first two characters; removing those flags restored source-length output and complete audible playback.

The receiver may tear reverse channel #3 down after playback (`state=3`). Track `media_seen`; if an established session loses #3, re-bootstrap. Retry only if zero speech frames were sent; never replay an utterance that was already partially heard.

## Phase 5: Analyze the PCAP

### Offline tool

```bash
cd ~/miplay
python3 tools/inspect_pcap.py /tmp/miplay-device.pcap
python3 tools/inspect_pcap.py /tmp/miplay-device.pcap --extract-ts /tmp/device.ts
ffprobe -v error -show_streams /tmp/device.ts
```

`inspect_pcap.py` is standard-library-only and handles classic Ethernet IPv4 PCAP with common TCP retransmissions. It reconstructs TCP payloads, identifies MiPlay control frames, parses custom `$` RTP framing, and reports RTP/TS/PCR/PTS statistics. It does not support PCAPNG, 802.11 monitor captures, IP fragmentation, or arbitrary sequence-number wraps.

### Manual tcpdump filters

```bash
# All MiPlay/WFD traffic
sudo tcpdump -i "$IFACE" -nn -s 0 -vv \
  "host $HOST and (tcp port 8899 or tcp port 7274 or udp port 36524)"

# Control only
sudo tcpdump -i "$IFACE" -nn -X -s 0 "host $HOST and tcp port 8899"

# RTSP only
sudo tcpdump -i "$IFACE" -nn -A -s 0 "host $HOST and tcp port 7274"

# Timer only
sudo tcpdump -i "$IFACE" -nn -X "host $HOST and udp port 36524"

# mDNS/AirPlay discovery
sudo tcpdump -i "$IFACE" -nn -vv -s 0 "udp port 5353 or tcp port 7000"
```

### What to compare in a new-device PCAP

1. Does the first receiver frame use `$` and command `0x0028`?
2. Challenge length and exact bytes; preserve them for HMAC.
3. Does `0x0037` acknowledge the source version?
4. Does `0x001f` contain model/device fields?
5. Which response commands acknowledge `0x000e` and `0x001c`?
6. Does heartbeat return `0x001b`?
7. Does `Open` cause reverse TCP #1/#2/#3?
8. Does the receiver send TimerService UDP packets?
9. Does RTSP reach `SETUP`, `PLAY`, and `TIME_OFFSET`?
10. Does `SetMediaInfo` precede media?
11. Are RTP packets `$` + 24-bit big-endian length + RTP PT=33?
12. Are AAC ADTS, PAT/PMT/PCR, PTS, continuity counters, and pacing valid?
13. What notifications mark decoder readiness and playback completion?

Never infer missing bytes from a truncated tcpdump display; capture with `-s 0` and analyze the PCAP.

## Failure classification

| Symptom | First checks |
|---|---|
| No mDNS record | Device may not implement Bonjour; continue TCP/8899 discovery |
| Port 8899 open, unknown frame | Save raw bytes; not enough evidence to call it MiPlay |
| Challenge accepted but no version ACK | Check source version, HMAC key/bytes, sequence, and PCAP |
| DeviceInfo works, no reverse channel | Verify source IP/route, listeners bound before Open, firewall |
| Reverse #1 only | Wait for SETUP trigger; inspect RTSP response body and channel order |
| Timer UDP absent | Wrong advertised source IP/port or firewall |
| RTSP stops before PLAY | Capability body mismatch, transport mismatch, or firmware variant |
| `done` but no sound | Check `SetMediaInfo`, `first-audiopcm`, `state=2`, media channel #3, PCAP RTP/PTS |
| First words missing | Transcode exact MP3 through exact FFmpeg command and run `silencedetect`; do not initially blame receiver |
| Audio stops after one utterance | Receiver likely tore #3 down after `state=3`; re-bootstrap session |
| Second attempt repeats half a sentence | Do not retry after nonzero speech frames; only retry zero-frame failures |

## Evidence and handoff checklist

A new device investigation is complete only when the report contains:

- model, firmware, IP, mDNS names/services if any;
- raw first challenge length and a redacted DeviceInfo summary;
- command/sequence response differences versus L15A;
- PCAP path, capture filter, interface, and whether it is classic PCAP or PCAPNG;
- `inspect_pcap.py` output and, if extracted, `ffprobe` output;
- RTSP/TimerService milestones;
- exact test sentence and job JSON;
- user audible confirmation, not merely `done`;
- explicit list of unverified items and firmware/network scope.

Update `CHANGELOG.md` and the relevant `README.md` section after a new model is confirmed. Add protocol fixtures and offline tests before changing automatic discovery or media-selection policy.