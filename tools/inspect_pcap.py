#!/usr/bin/env python3
"""Inspect a classic Ethernet/IPv4 PCAP of an L15A WFD session.

Stdlib-only; no packets transmitted. Handles common retransmissions by sorting
per-flow TCP sequence numbers. Does not support PCAPNG, 802.11 monitor captures,
fragmented IP, or arbitrarily long seq-number wraps. Contains no L15A secrets.
"""
import argparse
import collections
import json
import re
import socket
import struct
from pathlib import Path


def read_pcap(path):
    blob=Path(path).read_bytes()
    magic=blob[:4]
    if magic==b'\xd4\xc3\xb2\xa1': endian='<'
    elif magic==b'\xa1\xb2\xc3\xd4': endian='>'
    elif magic==b'\x4d\x3c\xb2\xa1': endian='<'  # nanosecond pcap
    elif magic==b'\xa1\xb2\x3c\x4d': endian='>'
    else: raise ValueError('Expected classic PCAP, not PCAPNG or unknown format')
    if len(blob)<24: raise ValueError('Truncated PCAP global header')
    _,_,_,_,_,network=struct.unpack_from(endian+'HHIIII',blob,4)
    if network!=1: raise ValueError(f'Only Ethernet DLT_EN10MB (1) supported, got {network}')
    pos=24
    while pos+16<=len(blob):
        sec,usec,incl,original=struct.unpack_from(endian+'IIII',blob,pos)
        pos+=16
        if pos+incl>len(blob): raise ValueError('Truncated PCAP packet')
        packet=blob[pos:pos+incl]
        pos+=incl
        yield packet


def tcp_segment(ethernet):
    if len(ethernet)<14: return None
    kind=int.from_bytes(ethernet[12:14],'big'); at=14
    for _ in range(2):
        if kind not in (0x8100,0x88a8) : break
        if len(ethernet)<at+4:return None
        kind=int.from_bytes(ethernet[at+2:at+4],'big');at+=4
    if kind!=0x0800 or len(ethernet)<at+20:return None
    ip=ethernet[at:]
    if ip[0]>>4!=4:return None
    ihl=(ip[0]&15)*4
    if ihl<20 or len(ip)<ihl:return None
    frag=int.from_bytes(ip[6:8],'big')
    if frag&0x1fff:return None
    if ip[9]!=6:return None
    total=min(int.from_bytes(ip[2:4],'big'),len(ip))
    tcp=ip[ihl:total]
    if len(tcp)<20:return None
    sport,dport,seq=struct.unpack_from('>HHI',tcp,0)
    hl=(tcp[12]>>4)*4
    if hl<20 or len(tcp)<hl:return None
    payload=tcp[hl:]
    if not payload:return None
    src=socket.inet_ntoa(ip[12:16]);dst=socket.inet_ntoa(ip[16:20])
    return (src,sport,dst,dport),seq,payload


def reassemble(segments):
    # Retains first-seen bytes at duplicate seq positions, trims retransmissions.
    output=bytearray(); expected=None; gaps=0
    for seq,payload in sorted(segments,key=lambda t:t[0]):
        if expected is None: expected=seq
        if seq>expected:
            gaps+=seq-expected
            # A non-contiguous gap invalidates framing; stop at first gap.
            break
        trim=max(0,expected-seq)
        if trim<len(payload):
            chunk=payload[trim:]
            output+=chunk
            expected+=len(chunk)
    return bytes(output),gaps


def ts_pid(packet):
    return ((packet[1]&0x1f)<<8)|packet[2]


def ts_payload_offset(p):
    adapt=(p[3]>>4)&3
    if adapt in (0,2): return 188
    if adapt==1:return 4
    return 5+p[4]


def decode_pts(five):
    b=five
    return (((b[0]>>1)&7)<<30)|(b[1]<<22)|((b[2]>>1)<<15)|(b[3]<<7)|(b[4]>>1)


def decode_pcr(six):
    return (six[0]<<25)|(six[1]<<17)|(six[2]<<9)|(six[3]<<1)|(six[4]>>7)


def analyze_media(wire):
    offset=0;frames=0;ts_count=0;markers=0;first_rtp=None
    first_pts=None;first_pcr=None
    total_au=0;ts_dump=bytearray()
    while offset+4<=len(wire) and wire[offset]==0x24:
        n=int.from_bytes(wire[offset+1:offset+4],'big')
        if n<12 or offset+4+n>len(wire):break
        rtp=wire[offset+4:offset+4+n]
        if rtp[0]!=0x80 or (rtp[1]&0x7f)!=33 or (len(rtp)-12)%188: break
        timestamp=int.from_bytes(rtp[4:8],'big')
        if first_rtp is None:first_rtp=timestamp
        if rtp[1]&0x80:markers+=1
        frames+=1
        ts=rtp[12:]
        ts_dump.extend(ts)
        for j in range(0,len(ts),188):
            pkt=ts[j:j+188]
            if len(pkt)!=188 or pkt[0]!=0x47:
                return None
            ts_count+=1
            pid=ts_pid(pkt)
            if pid==0x1100 and pkt[1]&0x40:
                total_au+=1
                o=ts_payload_offset(pkt)
                if first_pts is None and pkt[o:o+4]==b'\x00\x00\x01\xc0' and len(pkt)>=o+14:
                    first_pts=decode_pts(pkt[o+9:o+14])
            if pid==0x1000 and first_pcr is None and pkt[3]&0x20 and pkt[5]&0x10:
                first_pcr=decode_pcr(pkt[6:12])
        offset+=4+n
    if frames<2:return None
    return {
        'rtp_frames':frames,'rtp_marker_frames':markers,
        'mpegts_packets':ts_count,'audio_pes_starts':total_au,
        'first_rtp_timestamp':first_rtp,'first_pes_pts':first_pts,
        'first_pcr_base':first_pcr,
        'tcp_frame_bytes':offset,'trailing_bytes':len(wire)-offset,
    },bytes(ts_dump)


def inspect(path):
    flows=collections.defaultdict(list)
    count=0
    for packet in read_pcap(path):
        count+=1
        rec=tcp_segment(packet)
        if rec:
            key,seq,payload=rec
            flows[key].append((seq,payload))
    media_candidates=[]; offsets=[]
    for flow,parts in flows.items():
        data,gaps=reassemble(parts)
        for m in re.finditer(rb'(?im)^TimeOffset:\s*([0-9]+)',data):
            offsets.append(int(m.group(1)))
        parsed=analyze_media(data)
        if parsed:
            info,ts=parsed
            media_candidates.append((info['rtp_frames'],flow,info,ts,gaps))
    result={'pcap_packets':count,'tcp_flows':len(flows), 'rtsp_time_offsets':sorted(set(offsets))}
    if media_candidates:
        _,flow,info,ts,gaps=max(media_candidates,key=lambda x:x[0])
        result.update({'media_flow':f'{flow[0]}:{flow[1]} -> {flow[2]}:{flow[3]}',
                       'media':info,'media_gaps_bytes':gaps})
        return result,ts
    return result,b''


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('pcap',help='classic ethernet PCAP; not PCAPNG')
    ap.add_argument('--extract-ts',metavar='OUTPUT.ts',help='optional extracted transport stream (contains original audio)')
    args=ap.parse_args()
    result,ts=inspect(args.pcap)
    print(json.dumps(result,indent=2,ensure_ascii=False))
    if args.extract_ts:
        if not ts:raise SystemExit('No MiPlay media flow detected')
        Path(args.extract_ts).write_bytes(ts)
        print(f'Wrote {len(ts)} bytes of MPEG-TS to {args.extract_ts}')

if __name__=='__main__':main()
