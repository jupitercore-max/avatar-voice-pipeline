#!/usr/bin/env python3
"""
voice_client.py — Two-way PTT voice client for Ola Radio.

Built from binary disassembly of gwsdptt.framework/gwsdptt (ARM64).
Implements: Opus encode/decode + custom RTP over UDP + PTT mic control over TCP.

PROTOCOL SUMMARY (from disassembly):
  - TCP: HDLC-framed GWPTT for login, join, RequestMic (cmd 0x06), ReleaseMic (cmd 0x07)
  - UDP: Custom RTP (V=3) for voice transport
  - Codec: Opus 16kHz mono, 20ms frames, VOIP mode, 20kbps, VBR off
  - NOTE: Recorded voice messages use MP3 (128kbps via ffmpeg/lame) — different feature
  - XOR obfuscation: nibble-swap of payload length low byte

RTP WIRE FORMAT:
  Byte 0: (V<<6)|P<<5|X<<4|CC  → 0xC0 for voice, 0x40 for start/end markers
  Byte 1: (marker<<7)|(PT&0x7F)
  Bytes 2-3: seq (uint16 BE)
  Bytes 4-7: timestamp (uint32 BE)
  Bytes 8-11: SSRC (uint32 BE)
  + 4-byte custom extension for voice: [0xD0|flag, len&0xFF, len>>8&0xFF, len>>16&0xFF]
  + XOR'd Opus payload

USAGE:
  # Send a test tone
  python3 voice_client.py --tone --duration 2

  # Send a WAV file
  python3 voice_client.py --file audio.wav

  # Live mic → radio
  python3 voice_client.py --live

  # Listen for incoming voice
  python3 voice_client.py --listen

  # Full duplex
  python3 voice_client.py --duplex
"""

from __future__ import annotations

import argparse
from collections import deque
import json
import logging
import os
import socket
import struct
import sys
import threading
import time
import wave
from typing import Optional

# Add parent dir for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ─── Opus Loading (macOS SIP workaround) ─────────────────────────────────────

def _load_opus():
    """Load libopus before importing opuslib (macOS SIP strips DYLD paths)."""
    try:
        import ctypes
        import ctypes.util
        path = ctypes.util.find_library('opus')
        if path:
            ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
            return
        # Try common paths
        for p in ['/opt/homebrew/lib/libopus.dylib', '/usr/local/lib/libopus.dylib',
                   '/usr/lib/libopus.dylib']:
            if os.path.exists(p):
                ctypes.CDLL(p, mode=ctypes.RTLD_GLOBAL)
                return
    except Exception:
        pass

_load_opus()

import opuslib

# Import PTT client for TCP
from ptt_client import (
    PttClient, ProtoEncoder, HDLC_FLAG,
)

# ─── Logging ──────────────────────────────────────────────────────────────────

logger = logging.getLogger("ola_radio.voice")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[VOICE %(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

# ─── Constants (ALL from disassembly) ────────────────────────────────────────

# GWPTT mic commands now imported correctly from ptt_client (0x0006/0x0007)
# Confirmed from disassembly: _cp_request_mic: mov w2, #0x6; _cp_release_mic: mov w2, #0x7

# Voice server
VOICE_SERVER_HOST = "eu-access2c.olaradio.top"
VOICE_SERVER_PORT = 23002

# RTP version (this SDK uses V=3, NOT standard V=2)
RTP_VERSION = 3
RTP_BYTE0_VOICE = (RTP_VERSION << 6)    # 0xC0
RTP_BYTE0_MARKER = 0x40                   # Start/end packets use 0x40

# RTP payload types
RTP_PT_VOICE = 0x78      # 120 - Opus voice data + start/end markers
RTP_PT_HEARTBEAT = 0x62  # 98  - UDP heartbeat

# Special sequence numbers for start/end markers
SEQ_START = 0x018E   # 398 - Start of speech
SEQ_END = 0x018F     # 399 - End of speech

# Voice data seq starts at: type + 0x190 + 1
def voice_seq(frame_index: int) -> int:
    """Compute RTP sequence number for a voice frame."""
    return (frame_index + 0x191) & 0xFFFF

# Opus codec config (from _opus_codec_init disassembly)
# CORRECTED from _ptt_init disassembly:
#   mov w0, #0x3e80  → 16000 Hz sample rate (NOT 8000!)
#   mov w1, #0x4e20  → 20000 bps bitrate
#   mov w2, #0x1     → 1 channel (mono)
OPUS_SAMPLE_RATE = 16000    # 0x3e80 (from ptt_init → opus_codec_init)
OPUS_CHANNELS = 1           # mono
OPUS_APPLICATION = opuslib.APPLICATION_VOIP  # 2048 (0x800)
OPUS_FRAME_SIZE_MS = 20     # 20ms frames
MAX_TALK_DURATION = 60.0    # hard local safety cap; configurable under ptt
OPUS_FRAME_SAMPLES = int(OPUS_SAMPLE_RATE * OPUS_FRAME_SIZE_MS / 1000)  # 320
OPUS_FRAME_BYTES = OPUS_FRAME_SAMPLES * 2  # 640 bytes PCM S16LE
OPUS_BITRATE = 20000        # 0x4e20 = 20000 bps (from ptt_init)

# Extension byte for Opus mode
EXT_OPUS_PREFIX = 0xD0

# UDP heartbeat interval
UDP_HEARTBEAT_INTERVAL = 30.0  # seconds

# Socket registration magic (4 bytes)
UDP_REGISTRATION_MAGIC = bytes([0x4D, 0x40, 0x16, 0x41])

# Receive buffering. Three packets is 60 ms at the protocol's 20 ms frame size.
JITTER_BUFFER_PACKETS = 3
JITTER_MAX_DELAY = 0.060
MISSING_END_TIMEOUT = 0.500
MAX_OPUS_PAYLOAD_BYTES = 4096
MAX_SEQUENCE_GAP = 64

# ─── XOR Obfuscation (from _ptt_rtp_encode at 0x25ce0) ───────────────────────


def xor_key(payload_len: int) -> int:
    """
    Compute XOR key from payload length.

    From disassembly:
      lsl w9, w26, #4        → (length << 4) & 0xF0
      bfxil w9, w26, #4, #4  → insert bits [4:7] of length at [0:3]
      Result = nibble_swap of low byte of length
    """
    low = payload_len & 0xFF
    return ((low << 4) & 0xF0) | ((low >> 4) & 0x0F)


def xor_obfuscate(payload: bytes, length_field: int = None) -> bytes:
    """XOR obfuscate voice payload."""
    if not payload:
        return payload
    key = xor_key(length_field if length_field is not None else len(payload))
    return bytes(b ^ key for b in payload)


def xor_deobfuscate(payload: bytes, length_field: int) -> bytes:
    """De-obfuscate incoming voice payload (same XOR, symmetric)."""
    return xor_obfuscate(payload, length_field)


# ─── RTP Packet Builders ─────────────────────────────────────────────────────


def build_rtp_voice_packet(
    seq: int,
    timestamp: int,
    ssrc: int,
    opus_payload: bytes,
    priority_flag: int = 0,
) -> bytes:
    """
    Build a voice data RTP packet (PT=0x78) with custom extension + XOR'd payload.

    Format from _ptt_rtp_encode:
      [12-byte RTP header][4-byte extension][XOR'd Opus payload]
    """
    # Standard 12-byte RTP header
    header = bytearray(12)
    header[0] = RTP_BYTE0_VOICE  # V=3, P=0, X=0, CC=0 → 0xC0
    header[1] = RTP_PT_VOICE & 0x7F  # PT=120, no marker
    struct.pack_into('>H', header, 2, seq & 0xFFFF)      # sequence (BE)
    struct.pack_into('>I', header, 4, timestamp & 0xFFFFFFFF)  # timestamp (BE)
    struct.pack_into('>I', header, 8, ssrc & 0xFFFFFFFF)       # SSRC (BE)

    # 4-byte custom extension
    payload_len = len(opus_payload)
    ext = bytearray(4)
    ext[0] = (EXT_OPUS_PREFIX | (priority_flag & 0x0F))  # 0xD0 | flag
    ext[1] = payload_len & 0xFF
    ext[2] = (payload_len >> 8) & 0xFF
    ext[3] = (payload_len >> 16) & 0xFF

    # XOR obfuscate payload
    obfuscated = xor_obfuscate(opus_payload, payload_len)

    return bytes(header) + bytes(ext) + obfuscated


def build_rtp_start_packet(
    timestamp: int,
    ssrc: int,
    name: str = "OpenClaw",
) -> bytes:
    """
    Build a start-of-speech marker packet.

    From _ptt_fake_voice_send:
      - byte0 = 0x40 (overwritten, not standard RTP version)
      - PT = 0x78
      - seq = 0x018E
      - After 12-byte header: extension byte + 3-byte length + name padded to ~100 bytes with 0xAC
    """
    header = bytearray(12)
    header[0] = RTP_BYTE0_MARKER  # 0x40
    header[1] = RTP_PT_VOICE & 0x7F
    struct.pack_into('>H', header, 2, SEQ_START)
    struct.pack_into('>I', header, 4, timestamp & 0xFFFFFFFF)
    struct.pack_into('>I', header, 8, ssrc & 0xFFFFFFFF)

    # Extension + name payload (simplified — just name + padding)
    name_bytes = name.encode('utf-8')[:95]
    name_block = bytearray(100)
    name_block[:len(name_bytes)] = name_bytes
    # Fill rest with 0xAC (from disassembly: memset fill byte)
    for i in range(len(name_bytes), 100):
        name_block[i] = 0xAC

    # Extension bytes (4 bytes before name)
    ext = bytearray(4)
    ext[0] = len(name_bytes) & 0xFF
    ext[1] = 0x00
    ext[2] = 0x00
    ext[3] = 0x00

    return bytes(header) + bytes(ext) + bytes(name_block)


def build_rtp_end_packet(
    timestamp: int,
    ssrc: int,
    name: str = "OpenClaw",
) -> bytes:
    """Build an end-of-speech marker packet (seq=0x018F)."""
    pkt = build_rtp_start_packet(timestamp, ssrc, name)
    # Override seq number
    pkt_arr = bytearray(pkt)
    struct.pack_into('>H', pkt_arr, 2, SEQ_END)
    return bytes(pkt_arr)


def build_rtp_heartbeat(ssrc: int, seq: int = 0) -> bytes:
    """
    Build a UDP heartbeat packet (PT=0x62).

    From _cp_rtp_heartbeat:
      RTP header with V=3, PT=0x62, SSRC
      Followed by packed Ptt.Net.HeartBeat protobuf (field 1 = 1)
    """
    header = bytearray(12)
    header[0] = (RTP_VERSION << 6)  # 0xC0
    header[1] = RTP_PT_HEARTBEAT & 0x7F  # PT=98
    struct.pack_into('>H', header, 2, seq & 0xFFFF)
    # Timestamp and SSRC
    struct.pack_into('>I', header, 4, int(time.time()) & 0xFFFFFFFF)
    struct.pack_into('>I', header, 8, ssrc & 0xFFFFFFFF)

    # Heartbeat protobuf: field 1 (varint) = 1
    hb_payload = ProtoEncoder.varint((1 << 3) | 0) + ProtoEncoder.varint(1)  # field 1 = 1

    return bytes(header) + hb_payload


# ─── RTP Packet Parser (for incoming voice) ──────────────────────────────────


def parse_rtp_header(data: bytes) -> dict:
    """Parse a 12-byte RTP header from incoming UDP data."""
    if len(data) < 12:
        return {}

    version = (data[0] >> 6) & 0x03
    padding = (data[0] >> 5) & 0x01
    extension = (data[0] >> 4) & 0x01
    cc = data[0] & 0x0F

    marker = (data[1] >> 7) & 0x01
    pt = data[1] & 0x7F

    seq = struct.unpack('>H', data[2:4])[0]
    timestamp = struct.unpack('>I', data[4:8])[0]
    ssrc = struct.unpack('>I', data[8:12])[0]

    header_len = 12 + (cc * 4)  # CSRC list
    if header_len > len(data):
        return {}

    return {
        'version': version,
        'padding': padding,
        'extension': extension,
        'cc': cc,
        'marker': marker,
        'pt': pt,
        'seq': seq,
        'timestamp': timestamp,
        'ssrc': ssrc,
        'header_len': header_len,
    }


def classify_incoming(rtp: dict) -> str:
    """
    Classify incoming UDP packet from RTP fields.

    From _ptt_process_rx_udp disassembly:
      PT=0x62 → heartbeat (return "heartbeat")
      PT=0x78:
        seq=0x18F → end of speech (return "end")
        seq=0x18E → start of speech (return "start")
        else → voice data (return "voice")
      else → "unknown"
    """
    if rtp['pt'] == RTP_PT_HEARTBEAT:
        return "heartbeat"
    if rtp['pt'] == RTP_PT_VOICE:
        if rtp['seq'] == SEQ_END:
            return "end"
        elif rtp['seq'] == SEQ_START:
            return "start"
        else:
            return "voice"
    return "unknown"


def parse_voice_payload(data: bytes, rtp: Optional[dict] = None) -> Optional[bytes]:
    """Validate and deobfuscate one RTP v3 Opus voice payload."""
    rtp = rtp or parse_rtp_header(data)
    if not rtp or rtp["version"] != RTP_VERSION or rtp["pt"] != RTP_PT_VOICE:
        return None
    if rtp["seq"] in (SEQ_START, SEQ_END):
        return None

    payload = data[rtp["header_len"]:]
    if len(payload) < 4:
        return None
    ext_byte = payload[0]
    if (ext_byte & 0xF0) != EXT_OPUS_PREFIX:
        return None

    payload_len = payload[1] | (payload[2] << 8) | (payload[3] << 16)
    available = len(payload) - 4
    if payload_len <= 0 or payload_len > MAX_OPUS_PAYLOAD_BYTES:
        return None
    if payload_len != available:
        return None
    return xor_deobfuscate(payload[4:4 + payload_len], payload_len)


def _seq_distance(seq: int, base: int) -> int:
    """Signed 16-bit RTP sequence distance from base to seq."""
    distance = (seq - base) & 0xFFFF
    return distance - 0x10000 if distance & 0x8000 else distance


class OpusJitterBuffer:
    """Small RTP sequence buffer with duplicate rejection and Opus FEC/PLC."""

    def __init__(
        self,
        decoder,
        depth: int = JITTER_BUFFER_PACKETS,
        max_delay: float = JITTER_MAX_DELAY,
    ):
        self.decoder = decoder
        self.depth = max(1, depth)
        self.max_delay = max_delay
        self.expected_seq: Optional[int] = None
        self.pending: dict[int, tuple[bytes, float]] = {}
        self._decoded_recent = deque(maxlen=256)
        self._decoded_set: set[int] = set()

    def reset(self):
        self.expected_seq = None
        self.pending.clear()
        self._decoded_recent.clear()
        self._decoded_set.clear()

    def push(self, seq: int, payload: bytes, now: float) -> list[bytes]:
        seq &= 0xFFFF
        if seq in self.pending or seq in self._decoded_set:
            return []
        if self.expected_seq is None:
            self.expected_seq = seq

        distance = _seq_distance(seq, self.expected_seq)
        if distance < 0 or distance > MAX_SEQUENCE_GAP:
            return []
        self.pending[seq] = (payload, now)
        return self.drain(now)

    def drain(self, now: float, force: bool = False) -> list[bytes]:
        output = []
        while self.expected_seq is not None and self.pending:
            current = self.pending.pop(self.expected_seq, None)
            if current is not None:
                pcm = self._decode(current[0])
                if pcm:
                    output.append(pcm)
                self._mark_decoded(self.expected_seq)
                self.expected_seq = (self.expected_seq + 1) & 0xFFFF
                continue

            future = min(
                self.pending,
                key=lambda seq: _seq_distance(seq, self.expected_seq),
            )
            gap = _seq_distance(future, self.expected_seq)
            oldest_arrival = min(arrival for _, arrival in self.pending.values())
            ready = force or len(self.pending) >= self.depth or (
                now - oldest_arrival >= self.max_delay
            )
            if gap <= 0 or not ready:
                break

            # Opus FEC in packet N reconstructs packet N-1. For a wider gap,
            # use decoder PLC until the immediately preceding missing frame.
            if gap == 1:
                pcm = self._decode(self.pending[future][0], decode_fec=True)
            else:
                pcm = self._decode(b"")
            if pcm:
                output.append(pcm)
            self._mark_decoded(self.expected_seq)
            self.expected_seq = (self.expected_seq + 1) & 0xFFFF
        return output

    def _decode(self, payload: bytes, decode_fec: bool = False) -> bytes:
        try:
            return self.decoder.decode(
                payload, OPUS_FRAME_SAMPLES, decode_fec=decode_fec
            )
        except Exception:
            if decode_fec:
                try:
                    return self.decoder.decode(
                        b"", OPUS_FRAME_SAMPLES, decode_fec=False
                    )
                except Exception:
                    pass
            return b""

    def _mark_decoded(self, seq: int):
        if len(self._decoded_recent) == self._decoded_recent.maxlen:
            self._decoded_set.discard(self._decoded_recent[0])
        self._decoded_recent.append(seq)
        self._decoded_set.add(seq)


class VoiceReceivePipeline:
    """Local packet-in/PCM-out receive path used by the live UDP loop and tests."""

    def __init__(
        self,
        decoder,
        missing_end_timeout: float = MISSING_END_TIMEOUT,
        jitter_depth: int = JITTER_BUFFER_PACKETS,
        jitter_max_delay: float = JITTER_MAX_DELAY,
    ):
        self.jitter = OpusJitterBuffer(decoder, jitter_depth, jitter_max_delay)
        self.missing_end_timeout = missing_end_timeout
        self.active = False
        self.ssrc: Optional[int] = None
        self.last_voice_at: Optional[float] = None
        self.pcm_buffer = bytearray()

    def feed_packet(self, data: bytes, now: Optional[float] = None) -> dict:
        now = time.monotonic() if now is None else now
        result = {"type": "malformed", "pcm": [], "completed": None}
        rtp = parse_rtp_header(data)
        if not rtp:
            return result

        packet_type = classify_incoming(rtp)
        result["type"] = packet_type
        if packet_type == "heartbeat":
            if rtp["version"] != RTP_VERSION:
                result["type"] = "malformed"
            return result

        if packet_type == "start":
            if data[0] != RTP_BYTE0_MARKER:
                result["type"] = "malformed"
                return result
            # Flush a previous talk burst if its end marker was lost.
            result["completed"] = self._finish(now) if self.active else None
            self.active = True
            self.ssrc = rtp["ssrc"]
            self.last_voice_at = now
            self.pcm_buffer = bytearray()
            self.jitter.reset()
            return result

        if packet_type == "end":
            if data[0] != RTP_BYTE0_MARKER:
                result["type"] = "malformed"
                return result
            result["completed"] = self._finish(now)
            return result

        if packet_type != "voice":
            return result
        opus_payload = parse_voice_payload(data, rtp)
        if opus_payload is None:
            result["type"] = "malformed"
            return result

        if not self.active:
            self.active = True
            self.ssrc = rtp["ssrc"]
            self.pcm_buffer = bytearray()
            self.jitter.reset()
        elif self.ssrc != rtp["ssrc"]:
            result["type"] = "foreign_ssrc"
            return result

        self.last_voice_at = now
        chunks = self.jitter.push(rtp["seq"], opus_payload, now)
        self._append(chunks)
        result["pcm"] = chunks
        return result

    def poll(self, now: Optional[float] = None) -> Optional[bytes]:
        """Drain delayed jitter packets and flush a burst whose end was lost."""
        now = time.monotonic() if now is None else now
        if not self.active:
            return None
        chunks = self.jitter.drain(now)
        self._append(chunks)
        if (
            self.last_voice_at is not None
            and now - self.last_voice_at >= self.missing_end_timeout
        ):
            return self._finish(now)
        return None

    def _finish(self, now: float) -> Optional[bytes]:
        if not self.active:
            return None
        self._append(self.jitter.drain(now, force=True))
        completed = bytes(self.pcm_buffer) if self.pcm_buffer else None
        self.active = False
        self.ssrc = None
        self.last_voice_at = None
        self.pcm_buffer = bytearray()
        self.jitter.reset()
        return completed

    def _append(self, chunks: list[bytes]):
        for pcm in chunks:
            self.pcm_buffer.extend(pcm)


def convert_pcm_to_mono_s16(
    pcm: bytes,
    sample_width: int,
    channels: int,
    source_rate: int,
    target_rate: int = OPUS_SAMPLE_RATE,
) -> bytes:
    """Convert integer PCM to mono S16LE without the removed ``audioop`` module."""
    if sample_width not in (1, 2, 3, 4):
        raise ValueError(f"unsupported PCM sample width: {sample_width}")
    if channels < 1 or source_rate <= 0 or target_rate <= 0:
        raise ValueError("channels and sample rates must be positive")
    frame_bytes = sample_width * channels
    if len(pcm) % frame_bytes:
        raise ValueError("PCM byte length is not a whole sample frame")

    def sample_at(offset: int) -> int:
        raw = pcm[offset:offset + sample_width]
        if sample_width == 1:
            return (raw[0] - 128) << 8
        if sample_width == 2:
            return int.from_bytes(raw, "little", signed=True)
        if sample_width == 3:
            value = int.from_bytes(raw, "little", signed=False)
            if value & 0x800000:
                value -= 1 << 24
            return value >> 8
        return int.from_bytes(raw, "little", signed=True) >> 16

    mono = []
    for frame_offset in range(0, len(pcm), frame_bytes):
        # Equal factors preserve stereo amplitude: 0.5 * left + 0.5 * right.
        total = sum(
            sample_at(frame_offset + channel * sample_width)
            for channel in range(channels)
        )
        mono.append(total / channels)

    if not mono:
        return b""
    if source_rate != target_rate:
        output_count = max(1, round(len(mono) * target_rate / source_rate))
        resampled = []
        for index in range(output_count):
            source_position = index * source_rate / target_rate
            left = min(int(source_position), len(mono) - 1)
            right = min(left + 1, len(mono) - 1)
            fraction = source_position - left
            resampled.append(mono[left] + (mono[right] - mono[left]) * fraction)
        mono = resampled

    output = bytearray()
    for sample in mono:
        value = max(-32768, min(32767, round(sample)))
        output.extend(struct.pack("<h", value))
    return bytes(output)


# ─── Protobuf Mic Request/Release ────────────────────────────────────────────


def build_request_mic_payload(group_id: int, flag: int = 0) -> bytes:
    """
    Build RequestMic protobuf (from _cp_request_mic disassembly).

    Protobuf fields (struct offsets from disassembly):
      offset 0x28: field for active=1 → proto field 1
      offset 0x2C: group_id → proto field 2
      offset 0x30: combined flag → proto field 3

    cp_request_mic does:
      stp w8, w23, [sp, #0x28]  → [0x28]=1, [0x2C]=group_id
      bfi w21, w22, #24, #8     → combine flag bytes
      stp w8, w21, [sp, #0x30]  → [0x30]=1, [0x34]=combined_flag
    """
    # Simple encoding: field 1 = 1 (active), field 2 = group_id, field 3 = flag
    return (
        ProtoEncoder.uint32_field(1, 1)        # active = true
        + ProtoEncoder.uint32_field(2, group_id)  # group_id
        + ProtoEncoder.uint32_field(3, flag)      # flag/priority
    )


def build_release_mic_payload() -> bytes:
    """
    Build ReleaseMic protobuf (from _cp_release_mic disassembly).

    The function just inits, packs, and sends an empty protobuf message.
    """
    return b""  # Empty protobuf (all default values)


# ─── Voice Client ────────────────────────────────────────────────────────────


class OlaVoiceClient:
    """
    Two-way PTT voice client for Ola Radio.

    Combines:
    - TCP control plane (PttClient for login, join, mic control)
    - UDP voice plane (RTP/Opus for audio send/receive)

    Usage:
        client = OlaVoiceClient()
        client.connect_and_login()
        client.join_group(16092)
        client.send_tone(duration=2.0)
        client.listen_for_voice(timeout=30)
    """

    def __init__(self, config_path: Optional[str] = None):
        # Load config
        self.config = self._load_config(config_path)
        self.group_id = 16092  # JC Dream

        # TCP client (reuse existing PttClient)
        self.ptt = PttClient(config_path=config_path)
        self.ptt.add_event_listener("member_get_mic", self._on_member_get_mic)
        self.ptt.add_event_listener("lost_mic", self._on_lost_mic)

        # UDP socket for voice
        self.udp_sock: Optional[socket.socket] = None
        self.voice_server_ip: Optional[str] = None

        # Opus codec
        self.encoder: Optional[opuslib.Encoder] = None
        self.decoder: Optional[opuslib.Decoder] = None
        self.receive_pipeline: Optional[VoiceReceivePipeline] = None

        # Session state
        self.ssrc = 0
        self.mic_held = False
        self.running = False
        self.receiving_voice = False
        self.pcm_buffer = bytearray()

        # Sequence/timestamp counters
        self.frame_index = 0
        self.timestamp = 0

        # Callbacks
        self.on_voice_received: Optional[callable] = None

        # Audio playback stream (for real-time receive)
        self._audio_stream = None

        # Background threads
        self._udp_recv_thread: Optional[threading.Thread] = None
        self._udp_heartbeat_thread: Optional[threading.Thread] = None
        self._transmitting = False  # Echo suppression flag
        self._floor_revoked = threading.Event()
        self._floor_acquired_at: Optional[float] = None
        self.max_talk_duration = float(
            self.config.get("ptt", {}).get(
                "max_talk_duration", MAX_TALK_DURATION
            )
        )

    def _load_config(self, config_path: Optional[str]) -> dict:
        path = config_path or os.path.join(os.path.dirname(__file__), "config.json")
        with open(path) as f:
            return json.load(f)

    def _init_opus(self):
        """Initialize Opus encoder + decoder (config from disassembly)."""
        self.encoder = opuslib.Encoder(
            OPUS_SAMPLE_RATE, OPUS_CHANNELS, OPUS_APPLICATION
        )
        # NOTE: opuslib CTL setters (bitrate, vbr, lsb_depth) are broken on this
        # libopus version. Encoder defaults (~24-28kbps) are interoperable.
        # The binary uses 20kbps VBR-off, but Opus decoders accept any bitrate.

        self.decoder = opuslib.Decoder(OPUS_SAMPLE_RATE, OPUS_CHANNELS)
        self.receive_pipeline = VoiceReceivePipeline(self.decoder)

        logger.info("Opus initialized: %dHz, %dch, %dbps, %dms frames",
                     OPUS_SAMPLE_RATE, OPUS_CHANNELS, OPUS_BITRATE, OPUS_FRAME_SIZE_MS)

    def _resolve_voice_server(self):
        """Resolve voice server hostname."""
        try:
            self.voice_server_ip = socket.gethostbyname(VOICE_SERVER_HOST)
            logger.info("Voice server: %s (%s:%d)",
                         VOICE_SERVER_HOST, self.voice_server_ip, VOICE_SERVER_PORT)
        except socket.gaierror as e:
            logger.error("Failed to resolve voice server: %s", e)
            raise

    def _create_udp_socket(self):
        """Create UDP socket bound for voice send/receive."""
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Bind to any available port
        self.udp_sock.bind(('0.0.0.0', 0))
        local_port = self.udp_sock.getsockname()[1]
        logger.info("UDP socket bound on port %d", local_port)

        self.udp_sock.settimeout(1.0)  # For responsive receive loop

    # ─── Connection Lifecycle ──────────────────────────────────────────────

    def connect_and_login(self) -> bool:
        """Connect to PTT server, login, and set up voice plane."""
        cfg = self.config
        user = cfg.get("currentUser", cfg.get("credentials", {}))
        ptt = cfg.get("ptt", {})

        login_name = str(user.get("userId", user.get("account", "")))
        ptt_token = ptt.get("token", "")
        ptt_uid = ptt.get("uid", "81997")

        # Connect TCP
        servers = cfg.get("servers", {}).get("ptt", {})
        host = servers.get("voiceServer", "eu-access2c.olaradio.top")
        port = servers.get("voicePort", 23001)

        if not self.ptt.connect():
            return False

        result = self.ptt.login(
            login_name=login_name,
            ptt_token=ptt_token,
            ptt_uid=ptt_uid,
            platform=ptt.get("platform", "linux"),
            device_model=ptt.get("device_model", "CX300"),
        )

        if not result.get("success"):
            logger.error("Login failed: %s", result.get("error"))
            return False
        self.ptt.start_listening(block=False)

        # Extract SSRC from user ID
        try:
            self.ssrc = int(user.get("uid", 81997))
        except (ValueError, TypeError):
            self.ssrc = 81997
        logger.info("SSRC: %d (0x%08X)", self.ssrc, self.ssrc)

        # Setup UDP voice plane
        self._resolve_voice_server()
        self._create_udp_socket()
        self._init_opus()

        # Send UDP registration packet
        self._send_registration()

        logger.info("✓ Voice client ready")
        return True

    def join_group(self, group_id: int) -> bool:
        """Join a PTT group."""
        self.group_id = group_id
        result = self.ptt.join_group(group_id)
        logger.info("Joined group %d: %s", group_id, result.get("success"))
        return result.get("success", False)

    def disconnect(self):
        """Clean up all connections."""
        self.running = False
        if self.mic_held:
            self.release_mic()
        if self.udp_sock:
            try:
                self.udp_sock.close()
            except Exception:
                pass
        self.ptt.disconnect()
        logger.info("Disconnected")

    # ─── UDP Voice Plane ───────────────────────────────────────────────────

    def _send_registration(self):
        """Send registration magic bytes to voice server."""
        if not self.udp_sock or not self.voice_server_ip:
            return
        try:
            self.udp_sock.sendto(
                UDP_REGISTRATION_MAGIC,
                (self.voice_server_ip, VOICE_SERVER_PORT)
            )
            logger.info("Sent UDP registration magic: %s", UDP_REGISTRATION_MAGIC.hex())
        except Exception as e:
            logger.warning("Registration send failed: %s", e)

    def _send_udp_heartbeat(self):
        """Send a UDP voice heartbeat (PT=0x62)."""
        if not self.udp_sock or not self.voice_server_ip:
            return
        pkt = build_rtp_heartbeat(ssrc=self.ssrc, seq=0)
        try:
            self.udp_sock.sendto(pkt, (self.voice_server_ip, VOICE_SERVER_PORT))
            logger.debug("UDP heartbeat sent (%d bytes)", len(pkt))
        except Exception as e:
            logger.warning("UDP heartbeat failed: %s", e)

    def _udp_heartbeat_loop(self):
        """Background UDP heartbeat sender."""
        while self.running:
            time.sleep(UDP_HEARTBEAT_INTERVAL)
            if not self.running:
                break
            self._send_udp_heartbeat()

    # ─── Mic Control ──────────────────────────────────────────────────────

    def request_mic(self, group_id: int = None) -> dict:
        """
        Request the microphone (TCP cmd=0x0006).

        From _cp_request_mic: protobuf with group_id, then packet_encode(cmd=0x06).
        Server should respond with RequestMicAck.
        """
        gid = group_id or self.group_id
        logger.info("→ RequestMic (cmd=0x0006, group=%d)", gid)
        result = self.ptt.request_mic(gid, timeout=5.0)
        if result.get("success"):
            self.mic_held = True
            self._floor_revoked.clear()
            self._floor_acquired_at = time.monotonic()
            logger.info("← Mic granted by server")
        else:
            self.mic_held = False
            self._floor_revoked.set()
            self._floor_acquired_at = None
            logger.warning("RequestMic failed: %s", result.get("error"))
        return result

    def release_mic(self, group_id: int = None) -> dict:
        """
        Release the microphone (TCP cmd=0x0007).

        From _cp_release_mic: empty protobuf, packet_encode(cmd=0x07).
        """
        logger.info("→ ReleaseMic (cmd=0x0007)")
        result = self.ptt.release_mic(timeout=5.0)
        # Local transmission must stop after a release attempt even if the
        # acknowledgement is lost or rejected.
        self.mic_held = False
        self._transmitting = False
        self._floor_revoked.set()
        self._floor_acquired_at = None
        if not result.get("success"):
            logger.warning("ReleaseMic failed: %s", result.get("error"))
        return result

    # ─── Voice Send ───────────────────────────────────────────────────────

    def _revoke_floor(self, reason: str):
        """Immediately disable UDP transmission after a floor revocation."""
        was_active = self.mic_held or self._transmitting
        self.mic_held = False
        self._transmitting = False
        self._floor_revoked.set()
        self._floor_acquired_at = None
        if was_active:
            logger.warning("Floor revoked: %s", reason)

    def _can_transmit(self) -> bool:
        """Return True only while a valid, unexpired floor grant is held."""
        if not self.mic_held or self._floor_revoked.is_set():
            return False
        if (
            self._floor_acquired_at is not None
            and self.max_talk_duration > 0
            and time.monotonic() - self._floor_acquired_at
            >= self.max_talk_duration
        ):
            self._revoke_floor(
                f"maximum talk duration ({self.max_talk_duration:.1f}s) reached"
            )
            return False
        return True

    def send_voice_start(self) -> bool:
        """Send start-of-speech marker."""
        if (
            not self._can_transmit()
            or not self.udp_sock
            or not self.voice_server_ip
        ):
            return False

        pkt = build_rtp_start_packet(
            timestamp=self.timestamp,
            ssrc=self.ssrc,
            name=self.config.get("currentUser", {}).get("nickName", "OpenClaw"),
        )
        try:
            self.udp_sock.sendto(pkt, (self.voice_server_ip, VOICE_SERVER_PORT))
            logger.info("→ Voice START (seq=0x%03X, %d bytes)", SEQ_START, len(pkt))
            return True
        except Exception as e:
            logger.error("Send start failed: %s", e)
            return False

    def send_voice_end(self) -> bool:
        """Send end-of-speech marker."""
        if (
            not self._can_transmit()
            or not self.udp_sock
            or not self.voice_server_ip
        ):
            return False

        pkt = build_rtp_end_packet(
            timestamp=self.timestamp,
            ssrc=self.ssrc,
            name=self.config.get("currentUser", {}).get("nickName", "OpenClaw"),
        )
        try:
            self.udp_sock.sendto(pkt, (self.voice_server_ip, VOICE_SERVER_PORT))
            logger.info("→ Voice END (seq=0x%03X, %d bytes)", SEQ_END, len(pkt))
            return True
        except Exception as e:
            logger.error("Send end failed: %s", e)
            return False

    def send_opus_frame(self, opus_data: bytes, priority: int = 0) -> bool:
        """Send a single Opus frame as an RTP voice packet."""
        if (
            not self._can_transmit()
            or not self.udp_sock
            or not self.voice_server_ip
        ):
            return False

        seq = voice_seq(self.frame_index)
        pkt = build_rtp_voice_packet(
            seq=seq,
            timestamp=self.timestamp,
            ssrc=self.ssrc,
            opus_payload=opus_data,
            priority_flag=priority,
        )

        try:
            self.udp_sock.sendto(pkt, (self.voice_server_ip, VOICE_SERVER_PORT))
        except Exception as e:
            logger.error("Send voice frame failed: %s", e)
            return False

        self.frame_index += 1
        self.timestamp += OPUS_FRAME_SAMPLES
        return True

    def send_pcm_audio(self, pcm_data: bytes, pace: bool = True) -> int:
        """
        Send raw PCM (16-bit, 16kHz, mono) audio as Opus voice frames.

        Args:
            pcm_data: PCM S16LE samples.
            pace: If True, pace frames at 20ms intervals (real-time).
        """
        total_frames = (len(pcm_data) + OPUS_FRAME_BYTES - 1) // OPUS_FRAME_BYTES
        logger.info("Sending %d PCM bytes → %d Opus frames", len(pcm_data), total_frames)
        sent_frames = 0
        next_deadline = time.monotonic()

        for i in range(0, len(pcm_data), OPUS_FRAME_BYTES):
            if not self._can_transmit():
                logger.warning(
                    "Stopped buffered audio after %d/%d frames: floor unavailable",
                    sent_frames, total_frames,
                )
                break
            # Get frame (pad last frame with silence if needed)
            frame = pcm_data[i:i + OPUS_FRAME_BYTES]
            if len(frame) < OPUS_FRAME_BYTES:
                frame = frame + b'\x00' * (OPUS_FRAME_BYTES - len(frame))

            # Encode to Opus
            opus_data = self.encoder.encode(frame, OPUS_FRAME_SAMPLES)

            # Send via UDP
            if not self.send_opus_frame(opus_data):
                break
            sent_frames += 1

            if pace:
                next_deadline += OPUS_FRAME_SIZE_MS / 1000.0
                remaining = next_deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
        return sent_frames

    def send_tone(self, duration: float = 2.0, freq: int = 440):
        """
        Generate and send a sine wave tone.

        Args:
            duration: Duration in seconds.
            freq: Frequency in Hz.
        """
        import math

        num_samples = int(OPUS_SAMPLE_RATE * duration)
        logger.info("Generating %.1fs %dHz tone (%d samples)", duration, freq, num_samples)

        pcm = bytearray()
        for i in range(num_samples):
            sample = int(32767 * 0.3 * math.sin(2 * math.pi * freq * i / OPUS_SAMPLE_RATE))
            pcm.extend(struct.pack('<h', sample))

        # Full PTT voice sequence
        if not self.mic_held:
            mic_result = self.request_mic()
            if not mic_result.get("success"):
                logger.error("Cannot get mic — aborting tone")
                return
            time.sleep(0.2)

        self.frame_index = 0
        self.timestamp = 0

        if not self.send_voice_start():
            logger.error("Cannot start tone — floor unavailable")
            return False
        time.sleep(0.05)
        sent_frames = self.send_pcm_audio(bytes(pcm), pace=True)
        time.sleep(0.05)
        completed = self._can_transmit()
        if completed:
            self.send_voice_end()

        if self.mic_held:
            time.sleep(0.2)
            self.release_mic()

        if completed and sent_frames:
            logger.info("✓ Tone sent (%.1fs @ %dHz)", duration, freq)
        return bool(completed and sent_frames)

    def send_wav_file(self, filepath: str):
        """Send a WAV file as voice."""
        with wave.open(filepath, 'rb') as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            nframes = wf.getnframes()
            pcm_raw = wf.readframes(nframes)

        logger.info("WAV: %dHz, %dch, %d-bit, %.1fs",
                     framerate, channels, sampwidth * 8, nframes / framerate)

        # Equal-factor stereo mix, sample-width conversion, and resampling.
        # This is pure Python so it also works on Python 3.13 (audioop removed).
        pcm = convert_pcm_to_mono_s16(
            pcm_raw,
            sample_width=sampwidth,
            channels=channels,
            source_rate=framerate,
        )

        # Full PTT sequence
        if not self.mic_held:
            mic_result = self.request_mic()
            if not mic_result.get("success"):
                logger.error("Cannot get mic — aborting WAV")
                return False
            time.sleep(0.2)

        self.frame_index = 0
        self.timestamp = 0

        if not self.send_voice_start():
            logger.error("Cannot start WAV — floor unavailable")
            return False
        time.sleep(0.05)
        sent_frames = self.send_pcm_audio(pcm, pace=True)
        time.sleep(0.05)
        completed = self._can_transmit()
        if completed:
            self.send_voice_end()

        if self.mic_held:
            time.sleep(0.2)
            self.release_mic()

        if completed and sent_frames:
            logger.info("✓ WAV sent: %s", filepath)
        return bool(completed and sent_frames)

    # ─── Voice Receive ─────────────────────────────────────────────────────

    def start_receive_loop(self):
        """Start background UDP receiver for incoming voice."""
        self.running = True

        # Start UDP heartbeat
        self._send_udp_heartbeat()
        self._udp_heartbeat_thread = threading.Thread(
            target=self._udp_heartbeat_loop, daemon=True, name="udp-heartbeat"
        )
        self._udp_heartbeat_thread.start()

        # Start receiver
        self._udp_recv_thread = threading.Thread(
            target=self._recv_loop, daemon=True, name="udp-receiver"
        )
        self._udp_recv_thread.start()

        # Start TCP heartbeat (from PttClient)
        self.ptt._running = True
        self.ptt._heartbeat_thread = threading.Thread(
            target=self.ptt._heartbeat_loop, daemon=True, name="tcp-heartbeat"
        )
        self.ptt._heartbeat_thread.start()

        logger.info("Receive loop started (listening for voice on UDP)")

    def _recv_loop(self):
        """Background UDP voice receiver."""
        if not self.receive_pipeline:
            if not self.decoder:
                logger.error("Receive loop requires an initialized Opus decoder")
                return
            self.receive_pipeline = VoiceReceivePipeline(self.decoder)

        while self.running and self.udp_sock:
            try:
                data, addr = self.udp_sock.recvfrom(2048)
            except socket.timeout:
                completed = self.receive_pipeline.poll()
                self._sync_receive_state()
                if completed:
                    logger.info("← Voice timed out without END (%d bytes PCM)",
                                len(completed))
                    self._deliver_received_audio(completed)
                continue
            except OSError:
                if self.running:
                    logger.warning("UDP recv error")
                break

            if self.voice_server_ip and addr[0] != self.voice_server_ip:
                continue

            rtp = parse_rtp_header(data)
            if not rtp:
                continue
            pkt_type = classify_incoming(rtp)
            if self._transmitting and pkt_type == "voice":
                continue

            result = self.receive_pipeline.feed_packet(data)
            self._sync_receive_state()
            if result["type"] == "heartbeat":
                logger.debug("← UDP heartbeat ack")
            elif result["type"] == "start":
                logger.info("← Voice START from SSRC 0x%08X", rtp['ssrc'])
                if result["completed"]:
                    self._deliver_received_audio(result["completed"])
            elif result["type"] == "end":
                completed = result["completed"]
                logger.info("← Voice END from SSRC 0x%08X (%d bytes PCM received)",
                            rtp['ssrc'], len(completed or b""))
                if completed:
                    self._deliver_received_audio(completed)
            elif result["type"] == "voice":
                for pcm in result["pcm"]:
                    if self._audio_stream:
                        try:
                            self._audio_stream.write(pcm)
                        except Exception:
                            pass
            elif result["type"] == "malformed":
                logger.debug("← Rejected malformed UDP voice packet (%d bytes)",
                             len(data))
            elif result["type"] == "foreign_ssrc":
                logger.debug("← Ignored simultaneous voice from SSRC 0x%08X",
                             rtp["ssrc"])
            else:
                logger.debug("← Unknown UDP packet: V=%d PT=%d seq=%d",
                             rtp['version'], rtp['pt'], rtp['seq'])

    def _sync_receive_state(self):
        """Keep legacy public receive fields in sync with the pipeline."""
        if not self.receive_pipeline:
            return
        self.receiving_voice = self.receive_pipeline.active
        self.pcm_buffer = self.receive_pipeline.pcm_buffer

    def _deliver_received_audio(self, pcm: bytes):
        if pcm and self.on_voice_received:
            self.on_voice_received(pcm)

    def _decode_opus_payload(self, data: bytes) -> list:
        """
        Decode Opus payload, handling both single-frame and multi-frame packets.

        Retained for compatibility with callers that pass already-extracted Opus;
        live RTP reception uses VoiceReceivePipeline and one Opus frame per packet.
        """
        if not self.decoder:
            return []

        # If payload is small enough for a single frame, decode directly
        if len(data) <= 80:
            try:
                pcm = self.decoder.decode(data, OPUS_FRAME_SAMPLES)
                return [pcm]
            except Exception:
                return []

        # Try multi-frame decode: scan for length-prefixed sub-frames
        frames = []
        pos = 0
        while pos < len(data):
            # Check if next byte looks like a length prefix
            remaining = len(data) - pos
            if remaining <= 1:
                break

            sub_len = data[pos]
            if sub_len == 0 or sub_len > remaining - 1:
                # Not a length prefix — try decoding rest as single frame
                try:
                    pcm = self.decoder.decode(data[pos:], OPUS_FRAME_SAMPLES)
                    frames.append(pcm)
                except Exception:
                    pass
                break

            # Extract and decode sub-frame
            pos += 1
            sub_data = data[pos:pos + sub_len]
            if len(sub_data) < sub_len:
                break
            try:
                pcm = self.decoder.decode(sub_data, OPUS_FRAME_SAMPLES)
                frames.append(pcm)
            except Exception:
                # This chunk didn't decode — try without length prefix assumption
                break
            pos += sub_len

        # Fallback: try as single frame if nothing decoded
        if not frames:
            try:
                pcm = self.decoder.decode(data, OPUS_FRAME_SAMPLES)
                frames.append(pcm)
            except Exception:
                pass

        return frames

    def listen_for_voice(self, timeout: float = 60.0, playback: bool = True):
        """
        Listen for incoming voice for a specified duration.
        Decoded PCM is saved to file, optionally played in real-time.

        Args:
            timeout: Listen duration in seconds.
            playback: If True, play received voice through speaker in real-time.
        """
        saved_pcm = bytearray()

        # Set up real-time audio playback
        self._audio_stream = None
        if playback:
            try:
                import sounddevice as sd
                self._audio_stream = sd.RawOutputStream(
                    samplerate=OPUS_SAMPLE_RATE,
                    channels=OPUS_CHANNELS,
                    dtype='int16',
                    blocksize=OPUS_FRAME_SAMPLES,
                )
                self._audio_stream.start()
                logger.info("🔊 Real-time playback enabled")
            except Exception as e:
                logger.warning("Playback unavailable (will save to file only): %s", e)
                self._audio_stream = None

        def on_voice(pcm_data):
            saved_pcm.extend(pcm_data)
            logger.info("🎵 Received voice: %d bytes PCM (%.1fs)",
                         len(pcm_data), len(pcm_data) / (OPUS_SAMPLE_RATE * 2))

        self.on_voice_received = on_voice
        self.start_receive_loop()

        logger.info("🎧 Listening for voice for %.0fs...", timeout)
        start = time.time()

        try:
            while time.time() - start < timeout:
                time.sleep(0.5)
        except KeyboardInterrupt:
            logger.info("Stopping listener...")

        self.running = False

        # Clean up playback stream
        if self._audio_stream:
            try:
                self._audio_stream.stop()
                self._audio_stream.close()
            except Exception:
                pass
            self._audio_stream = None

        if saved_pcm:
            # Save received audio
            outfile = os.path.join(os.path.dirname(__file__),
                                    f"received_voice_{int(time.time())}.wav")
            with wave.open(outfile, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(OPUS_SAMPLE_RATE)
                wf.writeframes(bytes(saved_pcm))
            logger.info("✓ Saved received voice to %s (%d bytes)", outfile, len(saved_pcm))
        else:
            logger.info("No voice received during listen period")

        return saved_pcm

    def _on_member_get_mic(self, parsed: dict):
        """Handle a TCP push announcing that another member has the floor."""
        proto = parsed.get("proto", {})
        speaking_uid = proto.get(1)
        if self.mic_held and speaking_uid != self.ssrc:
            self._revoke_floor(
                f"member {speaking_uid or 'unknown'} received the floor"
            )
        logger.info("📢 Member %s got the mic", speaking_uid or "unknown")
        if speaking_uid == self.ssrc:
            return
        self.receiving_voice = True
        self.pcm_buffer = bytearray()

    def _on_lost_mic(self, parsed: dict):
        """Handle a TCP push announcing that the current speaker lost the floor."""
        logger.info("📢 Mic released by other user")
        if self.mic_held or self._transmitting:
            self._revoke_floor("LostMic push received")
        self.receiving_voice = False
        if self.pcm_buffer and self.on_voice_received:
            self.on_voice_received(bytes(self.pcm_buffer))
        self.pcm_buffer = bytearray()

    @staticmethod
    def _extract_proto_string(proto: dict, field_num: int) -> str:
        """Extract a string value from a protobuf field."""
        val = proto.get(field_num)
        if val is None:
            return ""
        if isinstance(val, bytes):
            try:
                return val.decode('utf-8')
            except Exception:
                return val.hex()
        return str(val)

    # ─── Live Mic Mode ─────────────────────────────────────────────────────

    def live_mic(self, push_to_talk: bool = True):
        """
        Live microphone mode: capture audio and send as voice.

        Args:
            push_to_talk: If True, press ENTER to talk, press ENTER to stop.
                          If False, continuous transmit.
        """
        try:
            import sounddevice as sd
        except ImportError:
            logger.error("sounddevice not available: pip install sounddevice")
            return

        self.start_receive_loop()

        if push_to_talk:
            self._live_ptt_loop(sd)
        else:
            self._live_continuous_loop(sd)

    def _live_ptt_loop(self, sd):
        """Push-to-talk: press Enter to start/stop transmitting."""
        logger.info("🎙️  Push-to-talk mode")
        logger.info("   Press ENTER to start talking")
        logger.info("   Press ENTER again to stop")
        logger.info("   Ctrl+C to quit\n")

        transmitting = False
        stream = None

        try:
            while True:
                if not transmitting:
                    # Wait for Enter to start
                    input()
                    logger.info("🔴 TALKING... (Enter to stop)")

                    # Request mic
                    mic_result = self.request_mic()
                    if not mic_result.get("success"):
                        logger.warning("Floor denied; press ENTER to retry")
                        continue
                    time.sleep(0.15)

                    self.frame_index = 0
                    self.timestamp = 0
                    self._transmitting = True
                    self.send_voice_start()

                    # Start audio capture
                    stream = sd.RawInputStream(
                        samplerate=OPUS_SAMPLE_RATE,
                        channels=OPUS_CHANNELS,
                        dtype='int16',
                        blocksize=OPUS_FRAME_SAMPLES,
                    )
                    stream.start()
                    transmitting = True

                    # Start a thread to read Enter key while streaming
                    stop_flag = threading.Event()
                    input_thread = threading.Thread(
                        target=lambda: (input(), stop_flag.set()),
                        daemon=True,
                    )
                    input_thread.start()

                    # Stream audio frames
                    while not stop_flag.is_set() and self._can_transmit():
                        pcm_frame, overflowed = stream.read(OPUS_FRAME_SAMPLES)
                        if overflowed:
                            logger.warning("Audio overflow!")
                        pcm_bytes = pcm_frame.tobytes()
                        if len(pcm_bytes) < OPUS_FRAME_BYTES:
                            pcm_bytes += b'\x00' * (OPUS_FRAME_BYTES - len(pcm_bytes))
                        opus_data = self.encoder.encode(pcm_bytes, OPUS_FRAME_SAMPLES)
                        if not self.send_opus_frame(opus_data):
                            break

                    # Stop transmitting
                    if stream:
                        stream.stop()
                        stream.close()
                        stream = None
                    if self._can_transmit():
                        self.send_voice_end()
                        time.sleep(0.1)
                    if self.mic_held:
                        self.release_mic()
                    self._transmitting = False
                    transmitting = False
                    logger.info("🔇 Stopped talking")
                    logger.info("   Press ENTER to talk again\n")

        except KeyboardInterrupt:
            logger.info("\nStopping live mic...")
        except EOFError:
            logger.info("\nEOF — stopping...")
        finally:
            if stream:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
            if self.mic_held:
                self.release_mic()
            self._transmitting = False
            self.running = False
            self.disconnect()

    def _live_continuous_loop(self, sd):
        """Continuous transmit mode."""
        logger.info("🎙️  Continuous transmit mode (Ctrl+C to stop)\n")

        mic_result = self.request_mic()
        if not mic_result.get("success"):
            logger.error("Floor denied; continuous transmit aborted")
            return
        time.sleep(0.15)

        self.frame_index = 0
        self.timestamp = 0
        self._transmitting = True
        self.send_voice_start()

        stream = sd.RawInputStream(
            samplerate=OPUS_SAMPLE_RATE,
            channels=OPUS_CHANNELS,
            dtype='int16',
            blocksize=OPUS_FRAME_SAMPLES,
        )
        stream.start()

        try:
            while self._can_transmit():
                pcm_frame, overflowed = stream.read(OPUS_FRAME_SAMPLES)
                if overflowed:
                    logger.warning("Audio overflow!")
                pcm_bytes = pcm_frame.tobytes()
                if len(pcm_bytes) < OPUS_FRAME_BYTES:
                    pcm_bytes += b'\x00' * (OPUS_FRAME_BYTES - len(pcm_bytes))
                opus_data = self.encoder.encode(pcm_bytes, OPUS_FRAME_SAMPLES)
                if not self.send_opus_frame(opus_data):
                    break
        except KeyboardInterrupt:
            logger.info("\nStopping...")
        finally:
            stream.stop()
            stream.close()
            if self._can_transmit():
                self.send_voice_end()
                time.sleep(0.1)
            if self.mic_held:
                self.release_mic()
            self._transmitting = False
            self.running = False
            self.disconnect()


# ─── CLI Entry Point ──────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Ola Radio PTT Voice Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default=None, help="Path to config.json")
    parser.add_argument("--group", type=int, default=16092, help="Group ID (default: 16092)")
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    # Test connection + mic request
    sub.add_parser("test", help="Test TCP login + mic request")

    # Send a test tone
    tone = sub.add_parser("tone", help="Send a test tone")
    tone.add_argument("--duration", type=float, default=2.0)
    tone.add_argument("--freq", type=int, default=440)

    # Send a WAV file
    wav = sub.add_parser("wav", help="Send a WAV file")
    wav.add_argument("file", help="Path to WAV file")

    # Listen for incoming voice
    listen = sub.add_parser("listen", help="Listen for incoming voice")
    listen.add_argument("--timeout", type=float, default=60.0)

    # Live mic
    live = sub.add_parser("live", help="Live microphone mode")
    live.add_argument("--continuous", action="store_true", help="Continuous (not push-to-talk)")

    # UDP probe
    probe = sub.add_parser("probe", help="Send UDP probe packets and listen for responses")

    # Local loopback test (no server needed)
    sub.add_parser("loopback", help="Run local loopback tests (no server needed)")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    client = OlaVoiceClient(config_path=args.config)

    if args.command == "test":
        if not client.connect_and_login():
            sys.exit(1)
        client.join_group(args.group)
        time.sleep(0.5)

        # Try mic request
        result = client.request_mic()
        logger.info("Mic request result: %s", json.dumps(result, default=str))

        time.sleep(1.0)

        # Send UDP heartbeat
        client._send_udp_heartbeat()
        time.sleep(1.0)

        # Send a quick voice start → silence frame → end
        client.send_voice_start()
        time.sleep(0.1)

        # Send 5 silence frames
        silence_pcm = b'\x00' * OPUS_FRAME_BYTES
        for _ in range(5):
            opus_frame = client.encoder.encode(silence_pcm, OPUS_FRAME_SAMPLES)
            client.send_opus_frame(opus_frame)
            time.sleep(0.02)

        client.send_voice_end()
        time.sleep(0.2)

        client.release_mic()

        # Listen briefly for any response
        logger.info("Listening 5s for response...")
        client.start_receive_loop()
        time.sleep(5)

        client.disconnect()
        logger.info("✓ Test complete")

    elif args.command == "tone":
        if not client.connect_and_login():
            sys.exit(1)
        client.join_group(args.group)
        time.sleep(0.5)
        client.send_tone(duration=args.duration, freq=args.freq)
        time.sleep(1.0)
        client.disconnect()

    elif args.command == "wav":
        if not client.connect_and_login():
            sys.exit(1)
        client.join_group(args.group)
        time.sleep(0.5)
        client.send_wav_file(args.file)
        time.sleep(1.0)
        client.disconnect()

    elif args.command == "listen":
        if not client.connect_and_login():
            sys.exit(1)
        client.join_group(args.group)
        time.sleep(0.5)
        client.listen_for_voice(timeout=args.timeout)
        client.disconnect()

    elif args.command == "live":
        if not client.connect_and_login():
            sys.exit(1)
        client.join_group(args.group)
        time.sleep(0.5)
        client.live_mic(push_to_talk=not args.continuous)

    elif args.command == "probe":
        if not client.connect_and_login():
            sys.exit(1)
        client.join_group(args.group)
        time.sleep(0.5)

        # Send various UDP probes
        logger.info("=== UDP Probe ===")

        # 1. Registration magic
        client._send_registration()
        time.sleep(1.0)

        # 2. Heartbeat
        client._send_udp_heartbeat()
        time.sleep(1.0)

        # 3. Voice start with no payload
        client.frame_index = 0
        client.timestamp = 0
        client.send_voice_start()
        time.sleep(0.5)

        # 4. One silence frame
        silence = client.encoder.encode(b'\x00' * OPUS_FRAME_BYTES, OPUS_FRAME_SAMPLES)
        client.send_opus_frame(silence)
        time.sleep(0.1)

        # 5. Voice end
        client.send_voice_end()
        time.sleep(1.0)

        # Listen for any responses
        logger.info("Listening 10s for UDP responses...")
        client.running = True
        client._udp_recv_thread = threading.Thread(target=client._recv_loop, daemon=True)
        client._udp_recv_thread.start()
        time.sleep(10)

        client.disconnect()
        logger.info("✓ Probe complete")

    elif args.command == "loopback":
        # Full local loopback test — no server needed
        logger.info("=== Local Loopback Test (no server required) ===\n")

        # Test 1: Opus round-trip
        logger.info("Test 1: Opus encode/decode round-trip")
        import math
        enc = opuslib.Encoder(OPUS_SAMPLE_RATE, OPUS_CHANNELS, OPUS_APPLICATION)
        dec = opuslib.Decoder(OPUS_SAMPLE_RATE, OPUS_CHANNELS)

        # Generate 1 second of 440Hz tone
        num_samples = OPUS_SAMPLE_RATE
        pcm_input = bytearray()
        for i in range(num_samples):
            s = int(32767 * 0.3 * math.sin(2 * math.pi * 440 * i / OPUS_SAMPLE_RATE))
            pcm_input.extend(struct.pack('<h', s))
        pcm_input = bytes(pcm_input)

        frames_in = num_samples // OPUS_FRAME_SAMPLES
        pcm_output = bytearray()
        for i in range(frames_in):
            frame = pcm_input[i*OPUS_FRAME_BYTES:(i+1)*OPUS_FRAME_BYTES]
            opus_frame = enc.encode(frame, OPUS_FRAME_SAMPLES)
            pcm_back = dec.decode(opus_frame, OPUS_FRAME_SAMPLES)
            pcm_output.extend(pcm_back)

        max_diff = max(abs(a - b) for a, b in zip(pcm_input, pcm_output))
        logger.info("  Input:  %d bytes PCM → %d Opus frames", len(pcm_input), frames_in)
        logger.info("  Output: %d bytes PCM reconstructed", len(pcm_output))
        logger.info("  Max sample diff: %d / 32767 (%.1f%%)",
                     max_diff, 100.0 * max_diff / 32767)
        logger.info("  ✅ PASS\n")

        # Test 2: XOR obfuscation
        logger.info("Test 2: XOR obfuscation round-trip")
        test_payloads = [b'\xDE\xAD', b'\xBE\xEF\xCA\xFE', bytes(range(50)), b'\x00' * 80]
        all_pass = True
        for p in test_payloads:
            obf = xor_obfuscate(p)
            deobf = xor_deobfuscate(obf, len(p))
            if deobf != p:
                logger.info("  ❌ FAIL: payload len %d", len(p))
                all_pass = False
        if all_pass:
            logger.info("  All %d payloads round-tripped correctly", len(test_payloads))
            logger.info("  ✅ PASS")
        logger.info("")

        # Test 3: RTP packet build + parse
        logger.info("Test 3: RTP packet build + parse")
        test_ssrc = 81997
        test_seq = 0x20A
        test_ts = 12345
        test_opus = b'\x42' * 40

        pkt = build_rtp_voice_packet(
            seq=test_seq, timestamp=test_ts, ssrc=test_ssrc,
            opus_payload=test_opus,
        )
        rtp_parsed = parse_rtp_header(pkt[:12])
        assert rtp_parsed['version'] == RTP_VERSION, f"Version mismatch: {rtp_parsed['version']}"
        assert rtp_parsed['pt'] == RTP_PT_VOICE, f"PT mismatch: {rtp_parsed['pt']}"
        assert rtp_parsed['seq'] == test_seq, f"Seq mismatch"
        assert rtp_parsed['timestamp'] == test_ts, f"Timestamp mismatch"
        assert rtp_parsed['ssrc'] == test_ssrc, f"SSRC mismatch"
        logger.info("  RTP header: V=%d PT=%d seq=0x%04X ts=%d ssrc=%d",
                     rtp_parsed['version'], rtp_parsed['pt'],
                     rtp_parsed['seq'], rtp_parsed['timestamp'], rtp_parsed['ssrc'])

        # Verify extension + payload
        ext_byte = pkt[12]
        payload_len_field = pkt[13] | (pkt[14] << 8) | (pkt[15] << 16)
        obfuscated = pkt[16:]
        deobfuscated = xor_deobfuscate(obfuscated, payload_len_field)
        assert deobfuscated == test_opus, "Payload round-trip mismatch"
        assert ext_byte & 0xD0 == 0xD0, f"Extension byte wrong: {ext_byte:#x}"
        logger.info("  Extension: 0x%02X, payload_len=%d", ext_byte, payload_len_field)
        logger.info("  Payload de-obfuscated: %d bytes match", len(deobfuscated))
        logger.info("  ✅ PASS\n")

        # Test 4: Start/end markers
        logger.info("Test 4: Start/end marker packets")
        start = build_rtp_start_packet(timestamp=0, ssrc=test_ssrc)
        end = build_rtp_end_packet(timestamp=100, ssrc=test_ssrc)
        start_rtp = parse_rtp_header(start[:12])
        end_rtp = parse_rtp_header(end[:12])
        assert start_rtp['seq'] == SEQ_START, f"Start seq wrong: {start_rtp['seq']}"
        assert end_rtp['seq'] == SEQ_END, f"End seq wrong: {end_rtp['seq']}"
        logger.info("  Start: seq=0x%03X, %d bytes", start_rtp['seq'], len(start))
        logger.info("  End:   seq=0x%03X, %d bytes", end_rtp['seq'], len(end))
        logger.info("  ✅ PASS\n")

        # Test 5: HDLC framing round-trip
        logger.info("Test 5: HDLC framing round-trip")
        from ptt_client import make_packet, parse_packet, CMD_HEARTBEAT, build_heartbeat_payload
        hb_payload = build_heartbeat_payload()
        hb_pkt = make_packet(CMD_HEARTBEAT, hb_payload)
        assert hb_pkt[0] == HDLC_FLAG and hb_pkt[-1] == HDLC_FLAG
        parsed = parse_packet(hb_pkt)
        assert parsed is not None
        assert parsed['cmd'] == CMD_HEARTBEAT
        assert parsed['checksum_valid'] == True
        logger.info("  HDLC frame: %d bytes, cmd=0x%04X, cksum_valid=%s",
                     len(hb_pkt), parsed['cmd'], parsed['checksum_valid'])
        logger.info("  ✅ PASS\n")

        # Test 6: Multi-frame Opus decode
        logger.info("Test 6: Multi-frame Opus payload decoder")
        enc2 = opuslib.Encoder(OPUS_SAMPLE_RATE, OPUS_CHANNELS, OPUS_APPLICATION)
        dec2 = opuslib.Decoder(OPUS_SAMPLE_RATE, OPUS_CHANNELS)

        # Encode 3 frames and concatenate with length prefixes
        multi_data = bytearray()
        for _ in range(3):
            pcm_frame = bytearray()
            for j in range(OPUS_FRAME_SAMPLES):
                s = int(32767 * 0.3 * math.sin(2 * math.pi * 440 * j / OPUS_SAMPLE_RATE))
                pcm_frame.extend(struct.pack('<h', s))
            of = enc2.encode(bytes(pcm_frame), OPUS_FRAME_SAMPLES)
            multi_data.append(len(of))  # length prefix
            multi_data.extend(of)

        # Use the voice client's decoder method (standalone)
        class MiniClient:
            pass
        mc = MiniClient()
        mc.decoder = dec2
        frames = OlaVoiceClient._decode_opus_payload(mc, bytes(multi_data))
        assert len(frames) == 3, f"Expected 3 frames, got {len(frames)}"
        assert all(len(f) == OPUS_FRAME_BYTES for f in frames)
        logger.info("  Multi-frame: %d bytes → %d frames, each %d bytes PCM",
                     len(multi_data), len(frames), len(frames[0]))
        logger.info("  ✅ PASS\n")

        logger.info("=== All loopback tests passed! ===")
        logger.info("Voice pipeline is ready for live transmission.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
