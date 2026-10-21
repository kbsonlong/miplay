"""FFmpeg AAC ADTS input and MiPlay WFD/RTP output."""
import subprocess
import struct
import time
from ..common import log
from .mpegts import make_pat, make_pmt, make_pcr_packet, pes_to_ts

def adts_frames(path):
    import subprocess
    cmd=["ffmpeg","-hide_banner","-loglevel","error","-i",path,
         "-vn","-ac","2","-ar","48000","-c:a","aac","-profile:a","aac_low",
         "-b:a","256k","-f","adts","pipe:1"]
    proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    buf=bytearray()
    try:
        while True:
            chunk=proc.stdout.read(4096)
            if not chunk: break
            buf.extend(chunk)
            while len(buf)>=7:
                if buf[0]!=0xff or (buf[1]&0xf6)!=0xf0:
                    raise ValueError("FFmpeg output lost ADTS sync")
                hlen=7 if (buf[1]&1) else 9
                flen=((buf[3]&3)<<11)|(buf[4]<<3)|(buf[5]>>5)
                if flen<hlen: raise ValueError("Invalid ADTS frame length")
                if len(buf)<flen: break
                frame=bytes(buf[:flen]); del buf[:flen]
                # Ensure AAC-LC, 48 kHz, stereo.
                if (frame[2]>>6)!=1 or ((frame[2]>>2)&15)!=3 or (((frame[2]&1)<<2)|(frame[3]>>6))!=2:
                    raise ValueError("Encoded ADTS is not AAC-LC 48kHz stereo")
                yield frame
        if buf: raise ValueError(f"Trailing incomplete ADTS bytes: {len(buf)}")
        stderr=proc.stderr.read()
        code=proc.wait()
        if code: raise RuntimeError(stderr.decode(errors="replace"))
    finally:
        if proc.poll() is None: proc.kill()

def send_adts_as_miplay_rtp(path, media_sock, stop_event,
                            first_pcm_ready, receiver_playing_ready,
                            time_offset_us):
    # Prototype follows public packetizer layout: AAC ADTS -> PES/TS -> RTP PT 33.
    # RTP/PTS origin is derived from the RTSP TIME_OFFSET monotonic timestamp.
    # One timestamp step is 1920 ticks per 1024-sample AAC AU at 48kHz.
    media_sock.settimeout(5)
    seq=0; cc_audio=0; cc_pat=1; cc_pmt=1
    # Keep PTS/RTP in the same monotonic time domain as RTSP TIME_OFFSET/PCR.
    if time_offset_us is None:
        raise ValueError("RTSP TIME_OFFSET is unavailable; refusing unaligned media")
    pts=(time_offset_us * 90000 // 1_000_000) & 0xffffffff
    au_index=0; sent=0
    start=time.monotonic()
    for frame in adts_frames(path):
        if stop_event.is_set(): break
        # Include PAT/PMT/PCR at startup and then periodically.
        include_tables=(au_index==0 or au_index>=13 and (au_index-13)%5==0)
        packets=[]
        if include_tables:
            # PCR is the live monotonic 90 kHz clock, independent of the
            # presentation PTS (which starts at zero for this finite clip).
            now_us = time.monotonic_ns() // 1000
            pcr27 = now_us * 27
            live_pcr = (pcr27 // 300) & ((1 << 33) - 1)
            pcr_ext = pcr27 % 300
            packets.extend([make_pat(cc_pat),make_pmt(cc_pmt),make_pcr_packet(live_pcr,pcr_ext)])
            cc_pat=(cc_pat+1)&15; cc_pmt=(cc_pmt+1)&15
        ts,cc_audio=pes_to_ts(frame,pts,cc_audio)
        packets.extend(ts)
        # MiPlay carries up to seven complete 188-byte TS packets per RTP packet.
        for off in range(0,len(packets),7):
            fragment = packets[off:off+7]
            payload=b"".join(fragment)
            # Match the source trace: marker bit only on the final RTP
            # fragment of each AAC access unit; payload type remains 33.
            marker = 0x80 if off + len(fragment) >= len(packets) else 0x00
            rtp=bytes([0x80,marker | 33])+struct.pack(">HII",seq,pts,0xdeadbeef)+payload
            wire=b"$"+len(rtp).to_bytes(3,"big")+rtp
            media_sock.sendall(wire)
            seq=(seq+1)&0xffff; sent+=len(wire)
        au_index+=1; pts=(pts+1920)&0xffffffff
        # The receiver cannot report decoded PCM until it has received media.
        # Send a short startup runway first, then require the captured readiness
        # pair before continuing with the rest of the clip.
        if au_index == 10:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not stop_event.is_set():
                if first_pcm_ready.is_set() and receiver_playing_ready.is_set():
                    log("AUDIO receiver readiness confirmed after startup runway")
                    break
                stop_event.wait(0.02)
            else:
                raise TimeoutError("L15A did not report first-audiopcm=1 and state=2 after startup media")
            if not (first_pcm_ready.is_set() and receiver_playing_ready.is_set()):
                raise TimeoutError("L15A receiver readiness was not confirmed")
        # Pace by AAC 1024-sample access-unit cadence (21.333 ms).
        target=start+au_index*(1024/48000)
        delay=target-time.monotonic()
        if delay>0: stop_event.wait(delay)
    log(f"AUDIO sent {au_index} AAC access units / {sent} wire bytes")

