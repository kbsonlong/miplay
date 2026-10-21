"""PCAP/media decoder self-tests without capturing network traffic."""
import importlib.util
import struct
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from src.media.mpegts import make_pat, make_pmt, make_pcr_packet, pes_to_ts
from src.media.audio import send_adts_as_miplay_rtp
from tools.inspect_pcap import analyze_media, decode_pts, decode_pcr, ts_pid


class WireSink:
    def __init__(self):self.buffers=[]
    def settimeout(self,t):pass
    def sendall(self,b):self.buffers.append(b)


class TestPcapDecoder(unittest.TestCase):
    def test_media_extract(self):
        adts=b'\xff\xf1\x4c\x80\x01\x1f\xfc' + b'B'*80
        pes,_=pes_to_ts(adts,90000,0)
        ts=make_pat(1)+make_pmt(1)+make_pcr_packet(100000,0)+b''.join(pes)
        rtp=bytes([0x80,0xa1])+struct.pack('>HII',1,90000,0xdeadbeef)+ts
        frame=b'$'+len(rtp).to_bytes(3,'big')+rtp
        stats,raw=analyze_media(frame*2)
        self.assertEqual(stats['rtp_frames'],2)
        self.assertEqual(stats['mpegts_packets'],len(ts)//188*2)
        self.assertEqual(stats['first_rtp_timestamp'],90000)
        self.assertEqual(stats['first_pes_pts'],90000)
        self.assertEqual(stats['first_pcr_base'],100000)
        self.assertEqual(raw,ts*2)
    def test_pts_pcr_base(self):
        from src.media.mpegts import pts_bytes,pcr_bytes
        self.assertEqual(decode_pts(pts_bytes(3767220630)),3767220630)
        self.assertEqual(decode_pcr(pcr_bytes(8062202057,43)),8062202057)
    def test_pmt_audio_pid(self):
        pmt=make_pmt(1)
        self.assertEqual(ts_pid(pmt),0x100)


class TestBaselineParity(unittest.TestCase):
    def test_modular_wire_matches_verified_v14(self):
        """V14 and modular sender must emit byte-identical deterministic media."""
        path=Path(__file__).resolve().parents[1]/'miplay_client_v14.py'
        spec=importlib.util.spec_from_file_location('v14_unmodified_baseline',path)
        baseline=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(baseline)

        adts=b'\xff\xf1\x4c\x80\x01\x1f\xfc' + b'Q'*128
        ready=threading.Event();ready.set()
        def encode(func,module):
            sock=WireSink()
            with patch.object(module,'adts_frames',return_value=iter([adts]*12)):
                with patch('time.monotonic_ns',return_value=566798454289000):
                    func('synthetic_input.wav',sock,threading.Event(),ready,ready,566798454289)
            return sock.buffers
        a=encode(baseline.send_adts_as_miplay_rtp,baseline)
        import src.media.audio as modular
        b=encode(send_adts_as_miplay_rtp,modular)
        self.assertEqual(a,b)
        self.assertGreater(len(a),10)


if __name__=='__main__':unittest.main()
