import socket
import threading
import unittest

from ptt_client import (
    CMD_GROUP_OPERATE, CMD_GROUP_OPERATE_ACK, CMD_HEARTBEAT,
    CMD_MEMBER_GET_MIC, CMD_REQUEST_MIC, CMD_REQUEST_MIC_ACK,
    GROUP_OP_ADD_USER, GROUP_OP_CREATE, GROUP_OP_EXIT, GROUP_OP_RENAME,
    PttClient, ProtoEncoder, build_group_operate_payload, decode_protobuf,
    hdlc_escape, hdlc_unescape, make_packet, parse_packet, recv_packet,
)
from voice_client import (
    build_rtp_voice_packet, parse_rtp_header, voice_seq, xor_deobfuscate,
)


class ProtocolTests(unittest.TestCase):
    def test_protobuf_and_hdlc_round_trip(self):
        payload = ProtoEncoder.string_field(1, "~}") + ProtoEncoder.uint32_field(2, 42)
        packet = make_packet(CMD_HEARTBEAT, payload)
        parsed = parse_packet(packet)
        self.assertTrue(parsed["checksum_valid"])
        self.assertEqual(parsed["proto"][1], b"~}")
        self.assertEqual(parsed["proto"][2], 42)
        self.assertEqual(hdlc_unescape(hdlc_escape(b"~}")), b"~}")

    def test_recv_packet_preserves_following_frame(self):
        left, right = socket.socketpair()
        try:
            one = make_packet(1, b"")
            two = make_packet(2, b"")
            right.sendall(one + two)
            frame, remainder = recv_packet(left, timeout=1)
            self.assertEqual(frame, one)
            self.assertEqual(remainder, two)
            frame2, remainder2 = recv_packet(left, timeout=1, initial=remainder)
            self.assertEqual(frame2, two)
            self.assertEqual(remainder2, b"")
        finally:
            left.close()
            right.close()

    def test_voice_packet_round_trip(self):
        opus = bytes(range(40))
        packet = build_rtp_voice_packet(voice_seq(3), 320, 81997, opus)
        header = parse_rtp_header(packet)
        encoded = packet[header["header_len"] + 4:]
        self.assertEqual(xor_deobfuscate(encoded, len(opus)), opus)
        self.assertEqual(header["version"], 3)

    def test_unified_reader_correlates_ack_and_dispatches_push(self):
        client_sock, server_sock = socket.socketpair()
        client = PttClient()
        client.sock = client_sock
        client.connected = True
        push_received = threading.Event()
        pushes = []

        def on_push(parsed):
            pushes.append(parsed)
            push_received.set()

        client.add_event_listener("member_get_mic", on_push)
        client._start_receiver()

        result_holder = {}
        request_thread = threading.Thread(
            target=lambda: result_holder.update(client.request_mic(16092, timeout=1))
        )
        request_thread.start()

        try:
            request_frame, _ = recv_packet(server_sock, timeout=1)
            self.assertEqual(parse_packet(request_frame)["cmd"], CMD_REQUEST_MIC)

            server_sock.sendall(
                make_packet(CMD_MEMBER_GET_MIC, ProtoEncoder.uint32_field(1, 42))
                + make_packet(CMD_REQUEST_MIC_ACK, ProtoEncoder.uint32_field(1, 0))
            )

            request_thread.join(timeout=1)
            self.assertFalse(request_thread.is_alive())
            self.assertTrue(result_holder["success"])
            self.assertEqual(result_holder["cmd"], CMD_REQUEST_MIC_ACK)
            self.assertTrue(push_received.wait(1))
            self.assertEqual(pushes[0]["cmd"], CMD_MEMBER_GET_MIC)
        finally:
            client.disconnect()
            server_sock.close()

    def test_direct_client_reads_are_rejected(self):
        client = PttClient()
        with self.assertRaises(RuntimeError):
            client._recv_packet()

    def test_request_mic_rejects_denied_and_malformed_ack(self):
        for payload, expected_error in (
            (ProtoEncoder.uint32_field(1, 7), "denied"),
            (b"", "missing result"),
        ):
            client_sock, server_sock = socket.socketpair()
            client = PttClient()
            client.sock = client_sock
            client.connected = True
            result_holder = {}
            request_thread = threading.Thread(
                target=lambda: result_holder.update(
                    client.request_mic(16092, timeout=1)
                )
            )
            request_thread.start()
            try:
                recv_packet(server_sock, timeout=1)
                server_sock.sendall(make_packet(CMD_REQUEST_MIC_ACK, payload))
                request_thread.join(timeout=1)
                self.assertFalse(request_thread.is_alive())
                self.assertFalse(result_holder["success"])
                self.assertIn(expected_error, result_holder["error"])
            finally:
                client.disconnect()
                server_sock.close()

    def test_group_operate_payload_matches_recovered_schema(self):
        create = decode_protobuf(
            build_group_operate_payload(GROUP_OP_CREATE, group_name=" JC Dream ")
        )
        self.assertEqual(create, {1: GROUP_OP_CREATE, 3: b"JC Dream"})

        rename = decode_protobuf(
            build_group_operate_payload(
                GROUP_OP_RENAME, group_id=16092, group_name="Radio Lab"
            )
        )
        self.assertEqual(
            rename,
            {1: GROUP_OP_RENAME, 2: 16092, 3: b"Radio Lab"},
        )

        add = build_group_operate_payload(
            GROUP_OP_ADD_USER, group_id=16092, member_ids=[42, 7, 42]
        )
        self.assertEqual(
            add,
            (
                ProtoEncoder.uint32_field(1, GROUP_OP_ADD_USER)
                + ProtoEncoder.uint32_field(2, 16092)
                + ProtoEncoder.uint32_field(4, 42)
                + ProtoEncoder.uint32_field(4, 7)
            ),
        )

        with self.assertRaises(ValueError):
            build_group_operate_payload(GROUP_OP_EXIT)
        with self.assertRaises(ValueError):
            build_group_operate_payload(GROUP_OP_CREATE, group_name=" ")

    def test_native_leave_requires_successful_group_ack(self):
        client_sock, server_sock = socket.socketpair()
        client = PttClient()
        client.sock = client_sock
        client.connected = True
        client.authenticated = True
        result_holder = {}
        request_thread = threading.Thread(
            target=lambda: result_holder.update(
                client.leave_group(16092, timeout=1)
            )
        )
        request_thread.start()
        try:
            frame, _ = recv_packet(server_sock, timeout=1)
            parsed = parse_packet(frame)
            self.assertEqual(parsed["cmd"], CMD_GROUP_OPERATE)
            self.assertEqual(
                parsed["proto"],
                {1: GROUP_OP_EXIT, 2: 16092},
            )
            server_sock.sendall(
                make_packet(
                    CMD_GROUP_OPERATE_ACK,
                    ProtoEncoder.uint32_field(1, 0)
                    + ProtoEncoder.uint32_field(2, 16092)
                    + ProtoEncoder.uint32_field(3, GROUP_OP_EXIT),
                )
            )
            request_thread.join(timeout=1)
            self.assertFalse(request_thread.is_alive())
            self.assertTrue(result_holder["success"])
            self.assertEqual(result_holder["group_id"], 16092)
            self.assertEqual(result_holder["action"], GROUP_OP_EXIT)
        finally:
            client.disconnect()
            server_sock.close()


if __name__ == "__main__":
    unittest.main()
