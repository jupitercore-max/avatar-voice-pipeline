import threading
import time

from voice_client import OlaVoiceClient


class FakeUdpSocket:
    def __init__(self):
        self.packets = []

    def sendto(self, packet, address):
        self.packets.append((packet, address))


def _bare_voice_client():
    client = OlaVoiceClient.__new__(OlaVoiceClient)
    client.udp_sock = FakeUdpSocket()
    client.voice_server_ip = "127.0.0.1"
    client.config = {"currentUser": {"nickName": "Local Test"}}
    client.ssrc = 81997
    client.frame_index = 0
    client.timestamp = 0
    client.mic_held = False
    client._transmitting = False
    client._floor_revoked = threading.Event()
    client._floor_acquired_at = None
    client.max_talk_duration = 60.0
    client.receiving_voice = False
    client.pcm_buffer = bytearray()
    client.on_voice_received = None
    return client


def test_udp_voice_requires_valid_floor_grant():
    client = _bare_voice_client()
    assert not client.send_voice_start()
    assert not client.send_opus_frame(b"opus")
    assert client.udp_sock.packets == []

    client.mic_held = True
    client._floor_acquired_at = time.monotonic()
    assert client.send_voice_start()
    assert client.send_opus_frame(b"opus")
    assert len(client.udp_sock.packets) == 2


def test_member_get_mic_push_immediately_stops_transmission():
    client = _bare_voice_client()
    client.mic_held = True
    client._transmitting = True
    client._floor_acquired_at = time.monotonic()

    client._on_member_get_mic({"proto": {1: 12345, 3: 16092}})

    assert not client.mic_held
    assert not client._transmitting
    assert client._floor_revoked.is_set()
    assert not client.send_opus_frame(b"opus")
    assert client.udp_sock.packets == []


def test_max_talk_duration_revokes_floor():
    client = _bare_voice_client()
    client.mic_held = True
    client._transmitting = True
    client.max_talk_duration = 0.01
    client._floor_acquired_at = time.monotonic() - 1

    assert not client.send_opus_frame(b"opus")
    assert not client.mic_held
    assert not client._transmitting
    assert client._floor_revoked.is_set()
    assert client.udp_sock.packets == []
