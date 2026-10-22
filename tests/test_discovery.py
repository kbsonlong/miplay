"""Tests for LAN discovery: frame parsing, classification, scan plumbing."""
import struct
import unittest
from unittest.mock import patch

from src.discovery import (parse_miplay_frame, classify, scan_lan,
                           default_base, local_ips, CONTROL_PORT)

# Real first-response frames captured live on 2026-10-22.
REAL_SPEAKER = b"$\x00(\x05\x14\x00\x00\x00\x10" + b"6124978242927770"  # L15A
REAL_BRIDGE = b"$\x00(\x00\x04\x00\x00\x00\x0e" + b"45896290615472"    # miplay-br


class TestFrameParsing(unittest.TestCase):
    def test_parse_roundtrip(self):
        cmd, seq, payload = parse_miplay_frame(REAL_SPEAKER)
        self.assertEqual(cmd, 0x0028)
        self.assertEqual(seq, 0x0514)
        self.assertEqual(payload, b"6124978242927770")

    def test_parse_rejects(self):
        self.assertIsNone(parse_miplay_frame(b"\x00" * 16))       # no magic
        self.assertIsNone(parse_miplay_frame(b"$\x00"))            # too short
        self.assertIsNone(parse_miplay_frame(b""))


class TestClassify(unittest.TestCase):
    def test_real_speaker_frame(self):
        self.assertEqual(classify(REAL_SPEAKER), "speaker")

    def test_bridge_frame_is_miplay_like_not_speaker(self):
        self.assertEqual(classify(REAL_BRIDGE), "miplay-like")

    def test_sixteen_digit_challenge_boundary(self):
        frame = b"$\x00(\x00\x01\x00\x00\x00\x11" + b"1" * 17   # 17 digits ok
        self.assertEqual(classify(frame), "speaker")
        frame = b"$\x00(\x00\x01\x00\x00\x00\x0f" + b"1" * 15   # 15 digits no
        self.assertEqual(classify(frame), "miplay-like")
        frame = b"$\x00(\x00\x01\x00\x00\x00\x10" + b"a" * 16   # non-digits no
        self.assertEqual(classify(frame), "miplay-like")

    def test_wrong_command(self):
        frame = b"$\x00\x1e\x00\x01\x00\x00\x00\x00"
        self.assertEqual(classify(frame), "unknown")


class TestScanPlumbing(unittest.TestCase):
    def test_scan_skips_local_ips_and_classifies(self):
        fake = {
            "192.168.3.4": REAL_SPEAKER,
            "192.168.3.18": REAL_BRIDGE,     # would be skipped anyway
            "192.168.3.50": REAL_BRIDGE,
        }

        def fake_probe(ip, port, timeout, send_probe):
            self.assertEqual(port, CONTROL_PORT)
            return ip, fake.get(ip)

        with patch("src.discovery._probe", fake_probe), \
             patch("src.discovery.local_ips",
                   return_value=["127.0.0.1", "192.168.3.18"]):
            found = scan_lan("192.168.3", timeout=0.05, workers=8)
        kinds = {d["ip"]: d["kind"] for d in found}
        self.assertEqual(kinds.get("192.168.3.4"), "speaker")
        self.assertEqual(kinds.get("192.168.3.50"), "miplay-like")
        self.assertNotIn("192.168.3.18", kinds)   # local IP skipped
        self.assertNotIn("192.168.3.1", kinds)    # unresponsive -> absent

    def test_default_base_prefers_non_loopback(self):
        with patch("src.discovery.local_ips",
                   return_value=["127.0.0.1", "192.168.3.18"]):
            self.assertEqual(default_base(), "192.168.3")
        with patch("src.discovery.local_ips", return_value=["127.0.0.1"]):
            self.assertEqual(default_base(), "192.168.3")  # documented fallback

    def test_local_ips_returns_strings(self):
        for ip in local_ips():
            self.assertIsInstance(ip, str)


if __name__ == "__main__":
    unittest.main()