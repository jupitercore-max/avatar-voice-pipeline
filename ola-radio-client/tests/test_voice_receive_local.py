import math
import struct

import opuslib

from voice_client import (
    OPUS_APPLICATION,
    OPUS_CHANNELS,
    OPUS_FRAME_BYTES,
    OPUS_FRAME_SAMPLES,
    OPUS_SAMPLE_RATE,
    VoiceReceivePipeline,
    build_rtp_end_packet,
    build_rtp_start_packet,
    build_rtp_voice_packet,
    convert_pcm_to_mono_s16,
    parse_voice_payload,
    voice_seq,
)


def _tone_frame(frame_index: int, frequency: int = 440) -> bytes:
    pcm = bytearray()
    first_sample = frame_index * OPUS_FRAME_SAMPLES
    for offset in range(OPUS_FRAME_SAMPLES):
        sample = int(
            9000
            * math.sin(
                2 * math.pi * frequency
                * (first_sample + offset)
                / OPUS_SAMPLE_RATE
            )
        )
        pcm.extend(struct.pack("<h", sample))
    return bytes(pcm)


def test_tone_rtp_v3_receive_round_trip_reorders_and_rejects_duplicate():
    """LOCAL ONLY: PCM tone -> Opus -> RTP v3 -> receive pipeline -> PCM."""
    encoder = opuslib.Encoder(
        OPUS_SAMPLE_RATE, OPUS_CHANNELS, OPUS_APPLICATION
    )
    decoder = opuslib.Decoder(OPUS_SAMPLE_RATE, OPUS_CHANNELS)
    receiver = VoiceReceivePipeline(
        decoder,
        missing_end_timeout=0.2,
        jitter_depth=3,
        jitter_max_delay=0.05,
    )
    ssrc = 81997
    packets = []
    for index in range(10):
        opus_frame = encoder.encode(_tone_frame(index), OPUS_FRAME_SAMPLES)
        packets.append(
            build_rtp_voice_packet(
                voice_seq(index),
                index * OPUS_FRAME_SAMPLES,
                ssrc,
                opus_frame,
            )
        )

    receiver.feed_packet(build_rtp_start_packet(0, ssrc), now=0.0)
    # Frame 2 arrives before frame 1, and frame 2 is duplicated.
    order = [0, 2, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    for arrival, index in enumerate(order, start=1):
        result = receiver.feed_packet(packets[index], now=arrival * 0.01)
        assert result["type"] == "voice"
    result = receiver.feed_packet(
        build_rtp_end_packet(10 * OPUS_FRAME_SAMPLES, ssrc), now=0.2
    )

    decoded = result["completed"]
    assert decoded is not None
    assert len(decoded) == 10 * OPUS_FRAME_BYTES
    samples = struct.unpack(f"<{len(decoded) // 2}h", decoded)
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    assert rms > 1000


def test_packet_loss_uses_next_packet_fec_and_missing_end_flushes():
    calls = []

    class FakeDecoder:
        def decode(self, payload, frame_size, decode_fec=False):
            calls.append((payload, decode_fec))
            return bytes([len(calls) & 0xFF]) * OPUS_FRAME_BYTES

    receiver = VoiceReceivePipeline(
        FakeDecoder(),
        missing_end_timeout=0.1,
        jitter_depth=2,
        jitter_max_delay=0.02,
    )
    ssrc = 123
    receiver.feed_packet(build_rtp_start_packet(0, ssrc), now=0.0)
    receiver.feed_packet(
        build_rtp_voice_packet(100, 0, ssrc, b"first"), now=0.01
    )
    # Sequence 101 is lost. Two future frames make the jitter buffer release it.
    receiver.feed_packet(
        build_rtp_voice_packet(102, 640, ssrc, b"fec-source"), now=0.02
    )
    receiver.feed_packet(
        build_rtp_voice_packet(103, 960, ssrc, b"third"), now=0.04
    )

    completed = receiver.poll(now=0.20)
    assert completed is not None
    assert any(payload == b"fec-source" and fec for payload, fec in calls)
    assert len(completed) == 4 * OPUS_FRAME_BYTES


def test_receive_rejects_inconsistent_declared_payload_length():
    packet = bytearray(build_rtp_voice_packet(500, 0, 1, b"valid"))
    packet[13] += 1
    assert parse_voice_payload(bytes(packet)) is None


def test_stereo_pcm_conversion_uses_equal_left_right_factors():
    # Equal and opposite stereo channels should mix to silence.
    stereo = struct.pack("<hhhh", 12000, -12000, -8000, 8000)
    mono = convert_pcm_to_mono_s16(stereo, 2, 2, 16000)
    assert mono == b"\x00\x00\x00\x00"
