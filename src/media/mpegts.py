"""MPEG2-TS AAC muxer, preserving validated V14 wire behavior."""

def mpeg_crc32(data):
    crc = 0xffffffff
    for value in data:
        crc ^= value << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04c11db7) & 0xffffffff if crc & 0x80000000 else (crc << 1) & 0xffffffff
    return crc

def ts_packet(pid, pusi, cc, payload, adaptation=False, flags=0):
    packet = bytearray(b"\xff" * 188)
    packet[0] = 0x47
    packet[1] = (0x40 if pusi else 0) | ((pid >> 8) & 0x1f)
    packet[2] = pid & 0xff
    if len(payload) == 184 and not adaptation:
        packet[3] = 0x10 | (cc & 0x0f)
        packet[4:188] = payload
    else:
        payload_len = len(payload)
        adaptation_len = 183 - payload_len
        packet[3] = 0x30 | (cc & 0x0f)
        packet[4] = adaptation_len
        if adaptation_len:
            packet[5] = flags
            if adaptation_len > 1:
                packet[6:5+adaptation_len] = b"\x00" * (adaptation_len-1)
        packet[5+adaptation_len:5+adaptation_len+payload_len] = payload
    return bytes(packet)

def psi_packet(pid, cc, section):
    # Reference muxer uses payload-only PSI packets and 0xff payload stuffing.
    packet = bytearray(b"\xff" * 188)
    packet[0] = 0x47
    packet[1] = 0x40 | ((pid >> 8) & 0x1f)  # PUSI=1
    packet[2] = pid & 0xff
    packet[3] = 0x10 | (cc & 0x0f)  # payload-only, continuity counter
    packet[4] = 0x00  # pointer_field
    packet[5:5 + len(section)] = section
    return bytes(packet)

def make_pat(cc):
    # MPEG-TS PAT: transport_stream_id, version, section numbers,
    # program_number=1, program_map_PID=0x0100, then CRC32.
    sec = bytearray([
        0x00, 0xb0, 0x0d,
        0x00, 0x01,       # transport_stream_id
        0xc3, 0x00, 0x00, # version/current_next, section, last section
        0x00, 0x01,       # program_number (missing in V7/V8)
        0xe1, 0x00,       # program_map_PID = 0x0100
    ])
    sec += mpeg_crc32(sec).to_bytes(4, "big")
    return psi_packet(0, cc, bytes(sec))

def make_pmt(cc):
    # PCR_PID=0x1000; AAC elementary PID=0x1100.
    sec = bytearray([0x02,0xb0,0x12,0x00,0x01,0xc3,0x00,0x00,0xf0,0x00,0xf0,0x00,
                     0x0f,0xf1,0x00,0xf0,0x00])
    sec += mpeg_crc32(sec).to_bytes(4,"big")
    return psi_packet(0x100,cc,bytes(sec))

def pts_bytes(pts):
    pts &= (1<<33)-1
    return bytes([
        0x21 | ((pts >> 29) & 0x0e),
        (pts >> 22) & 0xff,
        ((pts >> 14) & 0xfe) | 1,
        (pts >> 7) & 0xff,
        ((pts << 1) & 0xfe) | 1
    ])

def pcr_bytes(pcr, extension=0):
    pcr &= (1<<33)-1
    extension &= 0x1ff
    return bytes([(pcr>>25)&255,(pcr>>17)&255,(pcr>>9)&255,(pcr>>1)&255,
                  ((pcr&1)<<7)|0x7e|((extension>>8)&1),extension&255])

def make_pcr_packet(pcr, extension=0):
    packet=bytearray(b"\xff"*188)
    packet[0]=0x47; packet[1]=0x50; packet[2]=0x00; packet[3]=0x20
    packet[4]=183; packet[5]=0x10; packet[6:12]=pcr_bytes(pcr, extension)
    return bytes(packet)

def pes_to_ts(adts, pts, cc_audio):
    # Match the reference sender's 7-byte optional PES header: PTS plus two
    # 0xff stuffing bytes. PES_packet_length counts bytes after its length field.
    pes_len = 3 + 7 + len(adts)
    pes = (b"\x00\x00\x01\xc0" + pes_len.to_bytes(2, "big")
           + b"\x84\x80\x07" + pts_bytes(pts) + b"\xff\xff" + adts)
    out=[]
    pos=0; first=True
    while pos<len(pes):
        payload=pes[pos:pos+184]
        out.append(ts_packet(0x1100,first,cc_audio,payload))
        pos+=len(payload); cc_audio=(cc_audio+1)&15; first=False
    return out,cc_audio

