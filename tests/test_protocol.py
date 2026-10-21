"""Offline tests: no L15A access and no FFmpeg needed."""
import struct
import threading
import unittest
from unittest.mock import patch

from src.control.wire import frame, recv_frame, legacy_hmac
from src.rtsp.wire import rtsp, parse_rtsp
from src.media.mpegts import (mpeg_crc32, make_pat, make_pmt, make_pcr_packet,
                              pts_bytes, pes_to_ts, pcr_bytes)
from src.media.audio import send_adts_as_miplay_rtp
from src.timer.service import reply_timer
from src.rtsp.session import Services


class BytesSocket:
    def __init__(self, data=b''):
        self.data=data
        self.offset=0
        self.written=[]
    def recv(self, n):
        o=self.data[self.offset:self.offset+n]
        self.offset+=len(o)
        return o
    def settimeout(self, t):
        self.timeout=t
    def sendall(self, value):
        self.written.append(value)


def sections(ts):
    return ts[5:]


def valid_crc_section(ts):
    sec=sections(ts)
    section_length=((sec[1]&15)<<8)|sec[2]
    size=3+section_length
    return mpeg_crc32(sec[:size]) == 0


class TestControl(unittest.TestCase):
    def test_control_frame(self):
        b=frame(0x28,717,b'1234567890123456')
        self.assertEqual(len(b),9+16)
        self.assertEqual(b[:9],b'\x24\x00\x28\x02\xcd\x00\x00\x00\x10')
        self.assertEqual(recv_frame(BytesSocket(b)),(0x28,717,b'1234567890123456'))
    def test_hmac_shape(self):
        sig=legacy_hmac(b'1234567890123456')
        self.assertEqual(len(sig),40)
        self.assertTrue(all(c in b'0123456789abcdef' for c in sig))
    def test_reject_bad_magic(self):
        with self.assertRaises(ValueError):
            recv_frame(BytesSocket(b'\x00'+b'\x00'*8))


class TestRtsp(unittest.TestCase):
    def test_roundtrip_and_fragmented(self):
        message=rtsp('OPTIONS * RTSP/1.0',[('CSeq','1')],b'hi')
        self.assertIsNone(parse_rtsp(bytearray(message[:15])))
        got=parse_rtsp(bytearray(message))
        self.assertEqual(got[0],'OPTIONS * RTSP/1.0')
        self.assertEqual(got[1]['cseq'],'1')
        self.assertEqual(got[2],b'hi')
        self.assertEqual(got[3],len(message))


class TestTransportStream(unittest.TestCase):
    def test_pat(self):
        ts=make_pat(1)
        self.assertEqual(len(ts),188)
        self.assertEqual(ts[:5],b'\x47\x40\x00\x11\x00')
        self.assertTrue(valid_crc_section(ts))
        self.assertEqual(ts[13:17], b'\x00\x01\xe1\x00')
    def test_pmt(self):
        ts=make_pmt(1)
        self.assertEqual(len(ts),188)
        self.assertTrue(valid_crc_section(ts))
        self.assertEqual(ts[13:15],b'\xf0\x00')
        self.assertEqual(ts[17:22],b'\x0f\xf1\x00\xf0\x00')
    def test_pcr(self):
        ts=make_pcr_packet(123456,15)
        self.assertEqual(ts[:6], b'\x47\x50\x00\x20\xb7\x10')
        self.assertEqual(ts[6:12], pcr_bytes(123456,15))
    def test_pes(self):
        data=b'\xff\xf1\x4c\x80\x01\x1f\xfc'+b'X'*230
        payloads,cc=pes_to_ts(data, 0x80000011,0)
        self.assertEqual(len(payloads),2)
        self.assertEqual(cc,2)
        self.assertTrue(all(len(t)==188 and t[0]==0x47 for t in payloads))
        self.assertEqual(payloads[0][1]&0x40,0x40)
        self.assertEqual(payloads[0][4:8],b'\x00\x00\x01\xc0')
        self.assertEqual(len(pts_bytes(0x80000011)),5)


class TestTimer(unittest.TestCase):
    def test_reply_layout(self):
        request=bytes(range(40))
        reply=reply_timer(request,recv_us=1000,send_us=1015)
        self.assertEqual(len(reply),40)
        self.assertEqual(reply[:16],request[:16])
        self.assertEqual(struct.unpack_from('<Q',reply,16)[0],1015)
        self.assertEqual(struct.unpack_from('<Q',reply,24)[0],1000)
        self.assertEqual(reply[32:],request[32:])


class TestMedia(unittest.TestCase):
    def test_rtp_uses_offset_not_zero(self):
        sock=BytesSocket()
        adts=b'\xff\xf1\x4c\x80\x01\x1f\xfc'+b'Z'*40
        time_offset_us=566798454289
        with patch('src.media.audio.adts_frames', return_value=iter([adts]*3)):
            send_adts_as_miplay_rtp('ignored.wav',sock,threading.Event(),
                                   threading.Event(),threading.Event(),time_offset_us)
        self.assertEqual(len(sock.written),3)
        for idx,b in enumerate(sock.written):
            self.assertEqual(b[:1],b'$')
            self.assertEqual(int.from_bytes(b[1:4],'big'),len(b)-4)
            self.assertEqual(b[4],0x80)
            self.assertEqual(b[5]&0x7f,33)
            self.assertEqual(int.from_bytes(b[8:12],'big'),
                             (time_offset_us*90000//1000000 + idx*1920)&0xffffffff)
        self.assertNotEqual(int.from_bytes(sock.written[0][8:12],'big'),0)
    def test_refuses_no_offset(self):
        with self.assertRaises(ValueError):
            send_adts_as_miplay_rtp('ignored',BytesSocket(),threading.Event(),
                                   threading.Event(),threading.Event(),None)
    def test_audio_readiness_after_first_10(self):
        sock=BytesSocket()
        adts=b'\xff\xf1\x4c\x80\x01\x1f\xfc'+b'Z'*50
        good=threading.Event();good.set()
        with patch('src.media.audio.adts_frames',return_value=iter([adts]*11)):
            send_adts_as_miplay_rtp('ignored',sock,threading.Event(),good,good,566798454289)
        self.assertEqual(len(sock.written),11)
    def test_service_import_and_events(self):
        s=Services('127.0.0.1',0,0)
        self.assertFalse(s.play_event.is_set())
        self.assertIsNone(s.time_offset_us)


if __name__=='__main__':
    unittest.main()
