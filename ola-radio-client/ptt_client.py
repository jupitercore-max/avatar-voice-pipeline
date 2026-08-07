#!/usr/bin/env python3
"""
ptt_client.py — PTT TCP Protocol Client for Ola Radio / GWPTT SDK.

MERGED: Jul 6, 2026 — Correct HDLC wire format from working_client.py merged
into the structured PttClient class. The old 0xAA55 magic framing has been
replaced with the verified HDLC 0x7E framing confirmed via ARM64 disassembly
and live traffic capture.

Implements the binary TCP protocol used by the Ola Radio app for:
  - PTT server login (protobuf-encoded credentials)
  - Group queries and joining
  - Text message sending
  - Real-time message reception (push notifications)
  - Heartbeat / keepalive
  - Automatic reconnection with exponential backoff

Protocol (3 layers):
  TCP → HDLC Frame → GWPTT Packet → Protobuf Payload

Wire format:
  ┌──────┬──────────┬───────────┬─────────────────┬──────────────┬──────┐
  │ 0x7E │ cmd(2LE) │ len(2LE)  │ protobuf_payload│ checksum(2LE)│ 0x7E │
  └──────┴──────────┴───────────┴─────────────────┴──────────────┴──────┘

  - HDLC byte-stuffing: 0x7E → 0x7D 0x5E, 0x7D → 0x7D 0x5D
  - body_len = len(protobuf_payload) + 4
  - checksum = Fletcher-like (mod 56427, sum1 starts at 1)

Commands (GWPTT cmd values, LE on wire):
  0x0001 = Login         (LoginAck = 0x0064)
  0x0003 = Join Group
  0x0004 = Query Groups
  0x000A = Send Text
  0x4100 = Heartbeat      (wire bytes: 00 41, interval: 40s)

Usage:
    from ptt_client import PttClient

    client = PttClient(host="139.95.12.172", port=23001)
    client.connect()
    result = client.login(
        login_name="10074950",
        ptt_token="114952210524",
        ptt_uid="819978xZ",
    )
    if result["success"]:
        client.send_text(group_id=16092, text="Hello!")
        client.start_listening()  # blocks; calls on_message callbacks
"""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
import sys
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Iterable, Optional, Tuple, Union

# ─── Logging ──────────────────────────────────────────────────────────────────

logger = logging.getLogger("ola_radio.ptt")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[PTT %(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# ─── Constants ────────────────────────────────────────────────────────────────

DEFAULT_HOST = "139.95.12.172"
DEFAULT_PORT = 23001
CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 30.0
HEARTBEAT_INTERVAL = 40.0   # seconds between heartbeats (confirmed from live capture)
RECONNECT_DELAY = 2.0       # initial reconnect delay
RECONNECT_MAX_DELAY = 60.0  # max backoff
RECONNECT_MAX_ATTEMPTS = 5

# GWPTT command types (uint16 LE on wire)
CMD_LOGIN = 0x0001
CMD_LOGIN_ACK = 0x0064     # Original LoginAck (legacy)
CMD_LOGIN_ACK_V2 = 0x8100   # Current LoginAck (server uses this as of 2026-07)
CMD_LOGOUT = 0x0002
CMD_JOIN_GROUP = 0x0003
CMD_JOIN_GROUP_ACK = 0x8300
CMD_QUERY_GROUPS = 0x0004
CMD_QUERY_GROUPS_ACK = 0x8400
CMD_GROUP_OPERATE = 0x000F
CMD_GROUP_OPERATE_ACK = 0x8F00
CMD_SEND_TEXT = 0x000A
CMD_HEARTBEAT = 0x4100       # wire bytes: 00 41 (confirmed from capture)
CMD_HEARTBEAT_ACK = 0x4200   # wire bytes: 00 42 (server response)

# Push message types (from protobuf symbols in gwsdptt binary)
CMD_TEXT_ARRIVED = 0x000B    # TextMsgArrived push
CMD_CURRENT_GROUP = 0x000C   # CurrentGroup push
CMD_KICKOUT = 0x0063         # Kickout push

# Additional commands — values confirmed from ARM64 disassembly (Jul 10, 2026):
#   _cp_request_mic: mov w2, #0x6   → CMD_REQUEST_MIC = 0x0006
#   _cp_release_mic: mov w2, #0x7   → CMD_RELEASE_MIC = 0x0007
CMD_REQUEST_MIC = 0x0006     # RequestMic (confirmed from disassembly)
CMD_RELEASE_MIC = 0x0007     # ReleaseMic (confirmed from disassembly)
CMD_REQUEST_MIC_ACK = 0x8600  # Server ACK for RequestMic (mic granted)
CMD_RELEASE_MIC_ACK = 0x8700  # Server ACK for ReleaseMic
CMD_QUERY_MEMBERS = 0x0008   # QueryMembers (estimated; was 0x0007 before mic cmd shift)
CMD_SEND_TEXT_ACK = 0xC300   # Server ACK for SendText (proto field 1 = responder name)
CMD_GENERIC_ACK = 0xFF00     # Generic ACK (proto field 1 = uid echo)
# Push notification cmds for other users' mic state (exact values unconfirmed;
# previously guessed as 0x0005/0x0006 but those conflict with mic request/release)
CMD_MEMBER_GET_MIC = 0x0105  # Push: another member got mic (unconfirmed)
CMD_MEMBER_LOST_MIC = 0x0106 # Push: mic released by member (unconfirmed)
PTT_RESULT_OK = 0             # protobuf result convention used by GWPTT ACKs

# grp_operate_action enum recovered from the official macOS client.
GROUP_OP_CREATE = 0
GROUP_OP_ADD_USER = 1
GROUP_OP_DEL_USER = 2
GROUP_OP_DELETE = 3
GROUP_OP_RENAME = 4
GROUP_OP_EXIT = 5
GROUP_OP_JOIN = 6

CMD_NAMES = {
    0x0001: "LOGIN", 0x0064: "LOGIN_ACK", 0x8100: "LOGIN_ACK_V2",
    0x0002: "LOGOUT",
    0x0003: "JOIN_GROUP", 0x8300: "JOIN_GROUP_ACK",
    0x0004: "QUERY_GROUPS", 0x8400: "QUERY_GROUPS_ACK",
    0x000F: "GROUP_OPERATE", 0x8F00: "GROUP_OPERATE_ACK",
    0x0006: "REQUEST_MIC", 0x0007: "RELEASE_MIC", 0x0008: "QUERY_MEMBERS",
    0x8600: "REQUEST_MIC_ACK", 0x8700: "RELEASE_MIC_ACK",
    0x000A: "SEND_TEXT", 0xC300: "SEND_TEXT_ACK",
    0x4100: "HEARTBEAT", 0x4200: "HEARTBEAT_ACK", 0xFF00: "GENERIC_ACK",
    0x000B: "TEXT_ARRIVED", 0x000C: "CURRENT_GROUP", 0x0063: "KICKOUT",
}

# HDLC framing constants
HDLC_FLAG = 0x7E
HDLC_ESCAPE = 0x7D

# ─── Protobuf Encoder ────────────────────────────────────────────────────────


class ProtoEncoder:
    """Low-level protobuf field encoder."""

    @staticmethod
    def varint(value: int) -> bytes:
        if value < 0:
            value += (1 << 64)
        result = bytearray()
        while value > 0x7F:
            result.append((value & 0x7F) | 0x80)
            value >>= 7
        result.append(value & 0x7F)
        return bytes(result)

    @staticmethod
    def tag(field_num: int, wire_type: int) -> bytes:
        return ProtoEncoder.varint((field_num << 3) | wire_type)

    @classmethod
    def string_field(cls, field_num: int, value: str) -> bytes:
        if not value:
            return b""
        data = value.encode("utf-8")
        return cls.tag(field_num, 2) + cls.varint(len(data)) + data

    @classmethod
    def bytes_field(cls, field_num: int, value: bytes) -> bytes:
        if not value:
            return b""
        return cls.tag(field_num, 2) + cls.varint(len(value)) + value

    @classmethod
    def uint32_field(cls, field_num: int, value: int) -> bytes:
        return cls.tag(field_num, 0) + cls.varint(value)

    @classmethod
    def int64_field(cls, field_num: int, value: int) -> bytes:
        return cls.tag(field_num, 0) + cls.varint(value)

    @classmethod
    def bool_field(cls, field_num: int, value: bool) -> bytes:
        return cls.tag(field_num, 0) + cls.varint(1 if value else 0)


# Shorter functional aliases (match working_client.py style)
def encode_varint(value: int) -> bytes:
    return ProtoEncoder.varint(value)

def encode_string(field_num: int, value: str) -> bytes:
    return ProtoEncoder.string_field(field_num, value)

def encode_uint32(field_num: int, value: int) -> bytes:
    return ProtoEncoder.uint32_field(field_num, value)


# ─── Protobuf Decoder ────────────────────────────────────────────────────────


def decode_varint(data: bytes, pos: int) -> Tuple[int, int]:
    """Decode a varint from data at pos. Returns (value, new_pos)."""
    val = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        val |= (b & 0x7F) << shift
        pos += 1
        shift += 7
        if not (b & 0x80):
            break
    return val, pos


def decode_protobuf(data: bytes) -> dict:
    """Decode protobuf bytes into {field_num: value} dict."""
    fields = {}
    pos = 0
    while pos < len(data):
        tag, pos = decode_varint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            val, pos = decode_varint(data, pos)
            fields[field_num] = val
        elif wire_type == 2:
            length, pos = decode_varint(data, pos)
            fields[field_num] = data[pos:pos + length]
            pos += length
        elif wire_type == 5:
            fields[field_num] = struct.unpack('<I', data[pos:pos + 4])[0]
            pos += 4
        elif wire_type == 1:
            fields[field_num] = struct.unpack('<Q', data[pos:pos + 8])[0]
            pos += 8
        else:
            break
    return fields


class ProtoDecoder:
    """Alternative class-based protobuf decoder (kept for compatibility)."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def read_varint(self) -> int:
        result, self.pos = decode_varint(self.data, self.pos)
        return result

    def read_tag(self) -> Tuple[int, int]:
        tag = self.read_varint()
        return tag >> 3, tag & 0x07

    def read_length_delimited(self) -> bytes:
        length = self.read_varint()
        end = self.pos + length
        data = self.data[self.pos:end]
        self.pos = end
        return data

    def read_string(self) -> str:
        return self.read_length_delimited().decode("utf-8", errors="replace")

    def parse_to_dict(self) -> dict:
        return decode_protobuf(self.data[self.pos:])


# ─── HDLC Framing ────────────────────────────────────────────────────────────


def hdlc_escape(data: bytes) -> bytes:
    """Apply HDLC byte-stuffing: 0x7E→0x7D 0x5E, 0x7D→0x7D 0x5D."""
    out = bytearray()
    for b in data:
        if b == HDLC_FLAG:
            out.extend([HDLC_ESCAPE, HDLC_FLAG ^ 0x20])
        elif b == HDLC_ESCAPE:
            out.extend([HDLC_ESCAPE, HDLC_ESCAPE ^ 0x20])
        else:
            out.append(b)
    return bytes(out)


def hdlc_unescape(data: bytes) -> bytes:
    """Remove HDLC byte-stuffing."""
    out = bytearray()
    i = 0
    while i < len(data):
        if data[i] == HDLC_ESCAPE and i + 1 < len(data):
            out.append(data[i + 1] ^ 0x20)
            i += 2
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


# ─── GWPTT Checksum (Fletcher-like, from ARM64 disassembly) ──────────────────


def gwptt_checksum(data: bytes) -> int:
    """
    Fletcher-like checksum from gwsdptt binary disassembly.

    - Modulus: 56427 (0xDC6B)
    - sum1 starts at 1 (not 0)
    - Returns: (sum2 & 0xFF) << 8 | (sum1 & 0xFF)
    - Computed over body_len bytes (cmd + body_len fields + payload)
    """
    sum1, sum2 = 1, 0
    MOD = 56427
    for b in data:
        sum1 = (sum1 + b) % MOD
        sum2 = (sum2 + sum1) % MOD
    return ((sum2 & 0xFF) << 8) | (sum1 & 0xFF)


# ─── GWPTT Packet Build / Parse ──────────────────────────────────────────────


def make_packet(cmd: int, protobuf_payload: bytes) -> bytes:
    """
    Build HDLC-framed GWPTT packet:
      7E | cmd:2 LE | body_len:2 LE | protobuf_payload | checksum:2 LE | 7E

    Where body_len = len(protobuf_payload) + 4.
    Checksum computed over (cmd + body_len + payload), then HDLC-escaped.
    """
    body_len = (len(protobuf_payload) + 4) & 0xFFFF
    header = struct.pack('<HH', cmd & 0xFFFF, body_len)
    checksum_data = header + protobuf_payload
    checksum = gwptt_checksum(checksum_data) & 0xFFFF
    inner = header + protobuf_payload + struct.pack('<H', checksum)
    return bytes([HDLC_FLAG]) + hdlc_escape(inner) + bytes([HDLC_FLAG])


def parse_packet(data: bytes) -> Optional[dict]:
    """
    Parse a GWPTT response packet (HDLC-framed).
    Expected: 7E | cmd:2 LE | body_len:2 LE | payload | checksum:2 LE | 7E
    """
    if not data or len(data) < 8:
        return None
    if data[0] != HDLC_FLAG or data[-1] != HDLC_FLAG:
        return None

    inner = hdlc_unescape(data[1:-1])
    if len(inner) < 6:
        return None

    cmd = struct.unpack('<H', inner[0:2])[0]
    body_len = struct.unpack('<H', inner[2:4])[0]
    payload_len = max(0, body_len - 4)
    payload = inner[4:4 + payload_len]

    cksum_pos = 4 + payload_len
    checksum = struct.unpack('<H', inner[cksum_pos:cksum_pos + 2])[0] if len(inner) >= cksum_pos + 2 else 0
    expected = gwptt_checksum(inner[0:cksum_pos])

    return {
        'cmd': cmd,
        'body_len': body_len,
        'payload': payload,
        'proto': decode_protobuf(payload) if payload else {},
        'raw_payload': payload,
        'checksum': checksum,
        'checksum_valid': checksum == expected,
        'raw_inner': inner,
    }


def recv_packet(
    sock: socket.socket, timeout: float = 10.0, initial: bytes = b""
) -> Tuple[Optional[bytes], bytes]:
    """
    Receive one complete HDLC-framed GWPTT packet from a TCP stream.
    Since 0x7E is always escaped inside payload, any raw 0x7E in the
    stream is a frame delimiter.
    Returns (packet_bytes, leftover_data).
    """
    sock.settimeout(timeout)
    try:
        data = initial
        while True:
            while len(data) >= 2:
                start = data.find(bytes([HDLC_FLAG]))
                if start == -1:
                    data = b''
                    break
                end = data.find(bytes([HDLC_FLAG]), start + 1)
                if end == -1:
                    data = data[start:]
                    break
                pkt = data[start:end + 1]
                data = data[end + 1:]
                if len(pkt) > 2:
                    return pkt, data
            chunk = sock.recv(4096)
            if not chunk:
                raise EOFError("TCP stream closed")
            data += chunk
    except socket.timeout:
        return None, data
    return None, data


# ─── Protobuf Payload Builders ───────────────────────────────────────────────


def build_login_payload(
    login_name: str,
    ptt_token: str,
    ptt_uid: str,
    platform: str = "linux",
    device_model: str = "CX300",
    imei: str = "",
) -> bytes:
    """
    Build the Ptt.Rr.Login protobuf payload.

    Confirmed field mapping from live traffic capture:
      1: account / login name  (e.g., "10074950")
      2: PTT token             (e.g., "114952210524" — NOT the REST pc-access-token)
      4: platform              ("linux")
      5: device model          ("CX300")
      6: PTT UID               (e.g., "81997" — numeric uid string from capture)
      7: unknown config varint (120 — possibly keepalive interval)
     11: unknown string        ("111111" — possibly XOR-nibble encoded password)
    """
    proto = (
        encode_string(1, login_name)
        + encode_string(2, ptt_token)
        + encode_string(4, platform)
        + encode_string(5, device_model)
        + encode_string(6, ptt_uid)
        + encode_uint32(7, 120)
        + encode_string(11, "111111")
    )
    return proto


def build_login_packet_hardcoded() -> bytes:
    """
    Hardcoded login packet from captured traffic (known-working fallback).

    Raw (64 bytes on wire, 63 TCP payload after HDLC escaping):
      7e 01 00 3b 00 [protobuf fields] 9e f0 7e

    cmd=0x0001, body_len=0x003B (59), checksum=0xF09E
    """
    return bytes.fromhex(
        "7e01003b000a083130303734393530120c313134393532323130353234"
        "22056c696e75782a054358333030320538313939373878"
        "5a063131313131319ef07e"
    )


def build_heartbeat_payload(group_id: int = 81997) -> bytes:
    """
    Build heartbeat protobuf payload.

    Confirmed from live traffic capture:
      Protobuf: field 1 (varint) = numeric UID, field 2 (varint) = 1,
                field 3 (string) = "Other"
      Wire payload hex: 08 cd 80 05 10 01 1a 05 4f 74 68 65 72
    """
    return (
        encode_uint32(1, group_id)
        + encode_uint32(2, 1)
        + encode_string(3, "Other")
    )


def build_send_text_payload(
    group_id: str,
    text: str,
    sender_uid: str,
    sender_userid: str,
    sender_nick: str,
) -> bytes:
    """
    Build SendText protobuf payload.

    Field mapping (from working_client.py):
      1: group_id (string)
      2: text content (string)
      3: sender uid (string)
      4: sender userId (string)
      5: sender nickname (string)
      6: message type (string, "1" = normal)
    """
    return (
        encode_string(1, group_id)
        + encode_string(2, text)
        + encode_string(3, sender_uid)
        + encode_string(4, sender_userid)
        + encode_string(5, sender_nick)
        + encode_string(6, "1")
    )


def build_join_group_payload(group_id: int) -> bytes:
    """Build JoinGroup protobuf payload. Field 1 = group_id (varint)."""
    return encode_uint32(1, group_id)


def build_query_groups_payload(group_id: int, query_type: int = 16) -> bytes:
    """
    Build QueryGroups protobuf payload.
    Field 1 = group_id (varint), Field 2 = type (varint, 16 = list).
    """
    return encode_uint32(1, group_id) + encode_uint32(2, query_type)


def build_group_operate_payload(
    action: int,
    group_id: Optional[int] = None,
    group_name: str = "",
    member_ids: Iterable[int] = (),
) -> bytes:
    """Build the native ``grp_operate_request`` protobuf.

    Recovered from ``gwBuildGroup*`` and ``cp_group_operate`` in the official
    macOS framework.  The wire command is 0x000f and the fields are:
      1: grp_operate_action enum
      2: optional group ID
      3: optional group name
      4: repeated member IDs
    """
    valid_actions = {
        GROUP_OP_CREATE,
        GROUP_OP_ADD_USER,
        GROUP_OP_DEL_USER,
        GROUP_OP_DELETE,
        GROUP_OP_RENAME,
        GROUP_OP_EXIT,
        GROUP_OP_JOIN,
    }
    if action not in valid_actions:
        raise ValueError(f"Unsupported group operation action: {action}")

    if group_id is not None:
        group_id = int(group_id)
        if group_id <= 0:
            raise ValueError("group_id must be positive")

    group_name = group_name.strip()
    members = []
    for member_id in member_ids:
        member_id = int(member_id)
        if member_id <= 0:
            raise ValueError("member IDs must be positive")
        if member_id not in members:
            members.append(member_id)

    if action == GROUP_OP_CREATE and not group_name:
        raise ValueError("create requires group_name")
    if action != GROUP_OP_CREATE and group_id is None:
        raise ValueError("group operation requires group_id")
    if action == GROUP_OP_RENAME and not group_name:
        raise ValueError("rename requires group_name")
    if action in (GROUP_OP_ADD_USER, GROUP_OP_DEL_USER) and not members:
        raise ValueError("member operation requires at least one member ID")

    payload = encode_uint32(1, action)
    if group_id is not None:
        payload += encode_uint32(2, group_id)
    if group_name:
        payload += encode_string(3, group_name)
    for member_id in members:
        payload += encode_uint32(4, member_id)
    return payload


def build_request_mic_payload(group_id: int, flag: int = 0) -> bytes:
    """Build RequestMic: active, group ID, and priority/flag."""
    return (
        encode_uint32(1, 1)
        + encode_uint32(2, group_id)
        + encode_uint32(3, flag)
    )


def build_release_mic_payload() -> bytes:
    """Build the empty ReleaseMic protobuf used by this SDK."""
    return b""


@dataclass
class _PendingResponse:
    """One synchronous request waiting for one of its response commands."""

    commands: Tuple[int, ...]
    event: threading.Event = field(default_factory=threading.Event)
    response: Optional[dict] = None
    error: Optional[str] = None


# ─── PTT Client ──────────────────────────────────────────────────────────────


class PttClient:
    """
    TCP client for the Ola Radio PTT (Push-To-Talk) protocol.

    Uses GWPTT over HDLC framing (confirmed via ARM64 disassembly + live capture).
    Provides login, text messaging, group queries, real-time message listening,
    heartbeat keepalive, and automatic reconnection.

    Example:
        client = PttClient()
        client.connect()
        client.on_text_message = lambda msg: print(f"Got: {msg}")
        result = client.login(
            login_name="10074950",
            ptt_token="114952210524",
            ptt_uid="819978xZ",
        )
        if result["success"]:
            client.send_text("16092", "Hello from Ola Radio!")
            client.start_listening()
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        config_path: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.connected = False
        self.authenticated = False

        # Auth state
        self.login_name: str = ""
        self.ptt_token: str = ""
        self.ptt_uid: str = ""
        self.platform: str = "linux"
        self.device_model: str = "CX300"
        self.imei: str = ""
        self.nickname: str = ""

        # Server info from LoginAck
        self.server_info: dict = {}
        self.session_guid: str = ""

        # Callbacks for push messages
        self.on_text_message: Optional[Callable[[dict], None]] = None
        self.on_member_get_mic: Optional[Callable[[dict], None]] = None
        self.on_lost_mic: Optional[Callable[[dict], None]] = None
        self.on_members_changed: Optional[Callable[[dict], None]] = None
        self.on_kickout: Optional[Callable[[dict], None]] = None
        self.on_disconnect: Optional[Callable[[], None]] = None
        self.on_login_ack: Optional[Callable[[dict], None]] = None
        self._event_listeners: Dict[Union[int, str], list] = defaultdict(list)

        # Background threads
        self._recv_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._running = False
        self._listening = False
        self._recv_buffer = b""

        # The receiver is the sole owner of socket reads. Requests register a
        # pending response before sending, then wait on its Event.
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: Dict[int, Deque[_PendingResponse]] = defaultdict(deque)

        # Heartbeat state
        self._last_heartbeat_ack = time.time()

        # Load config if provided
        self.config = {}
        if config_path:
            self._load_config(config_path)

    def _load_config(self, config_path: str):
        """Load credentials from config.json."""
        try:
            with open(config_path) as f:
                self.config = json.load(f)
        except Exception as e:
            logger.error("Failed to load config: %s", e)

    # ─── Connection Management ──────────────────────────────────────────────

    def connect(self, timeout: float = CONNECT_TIMEOUT) -> bool:
        """Establish TCP connection to the PTT server."""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, "TCP_NODELAY"):
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.connect((self.host, self.port))
            self.connected = True
            self._recv_buffer = b""
            self._start_receiver()
            logger.info("✓ Connected to %s:%d", self.host, self.port)
            return True
        except Exception as e:
            logger.error("Connection failed: %s", e)
            self.connected = False
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None
            return False

    def disconnect(self):
        """Disconnect from the PTT server."""
        was_connected = self.connected
        was_authenticated = self.authenticated
        self._listening = False
        self._running = False
        self.authenticated = False

        if was_connected and self.sock and was_authenticated:
            self._send_packet(CMD_LOGOUT, b"")

        self.connected = False

        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

        self._fail_pending("Disconnected")

        current = threading.current_thread()
        if (self._recv_thread and self._recv_thread.is_alive()
                and self._recv_thread is not current):
            self._recv_thread.join(timeout=2)
        if (self._heartbeat_thread and self._heartbeat_thread.is_alive()
                and self._heartbeat_thread is not current):
            self._heartbeat_thread.join(timeout=2)

        if was_connected:
            logger.info("Disconnected")
            self._emit_event("disconnect", {})
            if self.on_disconnect:
                try:
                    self.on_disconnect()
                except Exception:
                    logger.exception("Disconnect callback failed")

    def reconnect(self, max_attempts: int = RECONNECT_MAX_ATTEMPTS) -> bool:
        """Reconnect with exponential backoff."""
        delay = RECONNECT_DELAY
        for attempt in range(1, max_attempts + 1):
            logger.info("Reconnect attempt %d/%d (delay=%.1fs)", attempt, max_attempts, delay)
            time.sleep(delay)
            self.disconnect()
            if self.connect():
                result = self.login(
                    self.login_name, self.ptt_token, self.ptt_uid,
                    self.platform, self.device_model, self.imei,
                )
                if result.get("success"):
                    return True
            delay = min(delay * 2, RECONNECT_MAX_DELAY)
        logger.error("Reconnection failed after %d attempts", max_attempts)
        return False

    # ─── Packet I/O ──────────────────────────────────────────────────────────

    def _send_packet(self, cmd: int, payload: bytes) -> bool:
        """Build and send an HDLC-framed GWPTT packet."""
        with self._write_lock:
            if not self.connected or not self.sock:
                logger.error("Not connected")
                return False
            try:
                pkt = make_packet(cmd, payload)
                self.sock.sendall(pkt)
                name = CMD_NAMES.get(cmd, f"0x{cmd:04x}")
                logger.debug("Sent %s (%d bytes payload)", name, len(payload))
                return True
            except Exception as e:
                logger.error("Send failed: %s", e)
                self.connected = False
                return False

    def _recv_packet(self, timeout: float = READ_TIMEOUT) -> Optional[dict]:
        """Deprecated: direct reads would race the unified receive loop."""
        raise RuntimeError(
            "Direct TCP reads are disabled; use request() or event listeners"
        )

    def _start_receiver(self):
        """Start the single TCP receive owner, if it is not already running."""
        if not self.connected or not self.sock:
            return
        if self._recv_thread and self._recv_thread.is_alive():
            self._running = True
            return
        self._running = True
        self._recv_thread = threading.Thread(
            target=self._recv_loop, daemon=True, name="ptt-receiver"
        )
        self._recv_thread.start()

    def _register_pending(self, commands: Iterable[int]) -> _PendingResponse:
        pending = _PendingResponse(tuple(dict.fromkeys(commands)))
        if not pending.commands:
            raise ValueError("At least one response command is required")
        with self._pending_lock:
            for cmd in pending.commands:
                self._pending[cmd].append(pending)
        return pending

    def _remove_pending(self, pending: _PendingResponse):
        with self._pending_lock:
            for cmd in pending.commands:
                queue = self._pending.get(cmd)
                if not queue:
                    continue
                try:
                    queue.remove(pending)
                except ValueError:
                    pass
                if not queue:
                    self._pending.pop(cmd, None)

    def _resolve_pending(self, parsed: dict) -> bool:
        cmd = parsed["cmd"]
        with self._pending_lock:
            queue = self._pending.get(cmd)
            if not queue:
                return False
            pending = queue.popleft()
            if not queue:
                self._pending.pop(cmd, None)
            for other_cmd in pending.commands:
                if other_cmd == cmd:
                    continue
                other_queue = self._pending.get(other_cmd)
                if other_queue:
                    try:
                        other_queue.remove(pending)
                    except ValueError:
                        pass
                    if not other_queue:
                        self._pending.pop(other_cmd, None)
            pending.response = parsed
            pending.event.set()
        return True

    def _fail_pending(self, error: str):
        with self._pending_lock:
            unique = {id(p): p for queue in self._pending.values() for p in queue}
            self._pending.clear()
            for pending in unique.values():
                pending.error = error
                pending.event.set()

    def request(
        self,
        cmd: int,
        payload: bytes,
        response_cmds: Iterable[int],
        timeout: float = READ_TIMEOUT,
    ) -> Optional[dict]:
        """Send a command and wait for its correlated response from the reader."""
        if not self.connected:
            return None
        self._start_receiver()
        pending = self._register_pending(response_cmds)
        if not self._send_packet(cmd, payload):
            self._remove_pending(pending)
            return None
        if not pending.event.wait(timeout):
            self._remove_pending(pending)
            return None
        return pending.response

    def add_event_listener(
        self, event: Union[int, str], callback: Callable[[dict], None]
    ):
        """Register a callback for a command number/name or semantic event."""
        with self._pending_lock:
            if callback not in self._event_listeners[event]:
                self._event_listeners[event].append(callback)

    def remove_event_listener(
        self, event: Union[int, str], callback: Callable[[dict], None]
    ):
        """Remove a previously registered event callback."""
        with self._pending_lock:
            listeners = self._event_listeners.get(event, [])
            try:
                listeners.remove(callback)
            except ValueError:
                return
            if not listeners:
                self._event_listeners.pop(event, None)

    def _emit_event(self, event: Union[int, str], data: dict):
        with self._pending_lock:
            listeners = list(self._event_listeners.get(event, ()))
        for callback in listeners:
            try:
                callback(data)
            except Exception:
                logger.exception("Event listener failed for %r", event)

    # ─── High-Level Operations ──────────────────────────────────────────────

    def login(
        self,
        login_name: str,
        ptt_token: str,
        ptt_uid: str,
        platform: str = "linux",
        device_model: str = "CX300",
        imei: str = "",
        use_hardcoded: bool = False,
    ) -> dict:
        """
        Login to the PTT server using protobuf-encoded credentials.

        Sends CMD_LOGIN (0x0001) and waits for LoginAck (0x0064).

        Args:
            login_name: PTT login name / account (e.g., "10074950").
            ptt_token: PTT token (NOT the REST pc-access-token).
            ptt_uid: PTT UID string (e.g., "819978xZ").
            platform: Platform string (default "linux").
            device_model: Device model (default "CX300").
            imei: Device IMEI (optional).
            use_hardcoded: Use captured login packet (for debugging).

        Returns:
            Dict with: success, error, session_guid, server_info, proto_fields.
        """
        if not self.connected and not self.connect():
            return {"success": False, "error": "Connection failed"}

        self.login_name = login_name
        self.ptt_token = ptt_token
        self.ptt_uid = ptt_uid
        self.platform = platform
        self.device_model = device_model
        self.imei = imei

        if use_hardcoded:
            pending = self._register_pending((CMD_LOGIN_ACK, CMD_LOGIN_ACK_V2))
            with self._write_lock:
                try:
                    self.sock.sendall(build_login_packet_hardcoded())
                    logger.info("Sent hardcoded login packet (known-working)")
                except Exception as e:
                    self._remove_pending(pending)
                    return {"success": False, "error": f"Hardcoded send failed: {e}"}
            if not pending.event.wait(15.0):
                self._remove_pending(pending)
                parsed = None
            else:
                parsed = pending.response
        else:
            payload = build_login_payload(
                login_name=login_name,
                ptt_token=ptt_token,
                ptt_uid=ptt_uid,
                platform=platform,
                device_model=device_model,
                imei=imei,
            )
            logger.info("Logging in as %s (uid=%s, device=%s)",
                        login_name, ptt_uid, device_model)
            parsed = self.request(
                CMD_LOGIN, payload, (CMD_LOGIN_ACK, CMD_LOGIN_ACK_V2), timeout=15.0
            )

        # Wait for LoginAck
        if not parsed:
            return {"success": False, "error": "No response from server (timeout)"}

        return self._parse_login_ack(parsed)

    def _parse_login_ack(self, parsed: dict) -> dict:
        """Parse LoginAck response from server."""
        result = {
            "success": False,
            "session_guid": "",
            "server_info": {},
            "error": "",
            "proto_fields": {},
        }

        cmd = parsed['cmd']
        proto = parsed.get('proto', {})
        payload = parsed.get('raw_payload', b'')

        logger.info("LoginAck: cmd=0x%04x, %d bytes payload, checksum_valid=%s",
                    cmd, len(payload), parsed.get('checksum_valid'))

        if cmd not in (CMD_LOGIN_ACK, CMD_LOGIN_ACK_V2):
            logger.warning("Expected LoginAck (0x0064 or 0x8100), got cmd=0x%04x", cmd)
            result["error"] = f"Unexpected response cmd=0x{cmd:04x}"
            result["proto_fields"] = {k: v for k, v in proto.items()}
            return result

        if proto:
            logger.info("LoginAck protobuf fields:")
            for fn, val in sorted(proto.items()):
                if isinstance(val, bytes):
                    try:
                        s = val.decode("utf-8")
                        if s.isprintable():
                            logger.info("  field %d: \"%s\"", fn, s)
                        else:
                            logger.info("  field %d: %s (hex)", fn, val.hex())
                    except (UnicodeDecodeError, ValueError):
                        logger.info("  field %d: %s (hex)", fn, val.hex())
                else:
                    logger.info("  field %d: %d", fn, val)

        result["proto_fields"] = {k: v for k, v in proto.items()}
        result["success"] = True
        self.authenticated = True

        # Extract server addresses and session GUID from LoginAck protobuf
        # Field 13 = session GUID (may be 16 raw bytes OR 32-char hex string)
        # Fields 1-5 = server addresses / user info
        server_info = {}
        for fn, val in proto.items():
            if isinstance(val, bytes):
                try:
                    s = val.decode("utf-8")
                    if s.isprintable():
                        if fn == 13:
                            result["session_guid"] = s
                        server_info[fn] = s
                    else:
                        if fn == 13:
                            result["session_guid"] = val.hex()
                        server_info[fn] = val.hex()
                except (UnicodeDecodeError, ValueError):
                    if fn == 13:
                        result["session_guid"] = val.hex()
                    server_info[fn] = val.hex()
            else:
                server_info[fn] = val

        result["server_info"] = server_info
        self.server_info = server_info
        logger.info("Login successful")
        return result

    def send_text(
        self,
        group_id: str,
        text: str,
        sender_uid: Optional[str] = None,
        sender_userid: Optional[str] = None,
        sender_nick: Optional[str] = None,
    ) -> dict:
        """
        Send a text message to a group via PTT protocol (CMD_SEND_TEXT=0x000A).

        Args:
            group_id: Target group ID as string.
            text: Message text.
            sender_uid: Sender PTT UID (defaults to stored value).
            sender_userid: Sender login name (defaults to stored value).
            sender_nick: Sender nickname (defaults to stored value).

        Returns:
            Dict with: success, error, ack.
        """
        if not self.authenticated:
            return {"success": False, "error": "Not authenticated — call login() first"}

        payload = build_send_text_payload(
            group_id=group_id,
            text=text,
            sender_uid=sender_uid or self.ptt_uid,
            sender_userid=sender_userid or self.login_name,
            sender_nick=sender_nick or self.nickname or "OpenClaw",
        )

        logger.info("→ Sent text to group %s: %s", group_id, text)
        parsed = self.request(
            CMD_SEND_TEXT, payload, (CMD_SEND_TEXT_ACK,), timeout=5.0
        )
        if not parsed:
            return {"success": False, "error": "No ack received (timeout)"}

        name = CMD_NAMES.get(parsed['cmd'], f"0x{parsed['cmd']:04x}")
        logger.info("SendText response: %s", name)

        # Any response from server indicates it processed the packet
        ack_name = CMD_NAMES.get(parsed['cmd'], f"0x{parsed['cmd']:04x}")
        return {
            "success": True,
            "ack": {
                "cmd": parsed['cmd'],
                "cmd_name": ack_name,
                "proto": parsed.get('proto', {}),
            },
        }

    def query_groups(self, group_id: int) -> dict:
        """
        Query group info from the PTT server (CMD_QUERY_GROUPS=0x0004).

        Args:
            group_id: Group ID to query.

        Returns:
            Dict with: success, raw, proto.
        """
        if not self.authenticated:
            return {"success": False, "error": "Not authenticated"}

        payload = build_query_groups_payload(group_id)
        parsed = self.request(
            CMD_QUERY_GROUPS, payload, (CMD_QUERY_GROUPS_ACK,), timeout=5.0
        )
        if not parsed:
            return {"success": False, "error": "No response"}

        return {
            "success": True,
            "cmd": parsed['cmd'],
            "proto": parsed.get('proto', {}),
            "raw": parsed.get('raw_payload', b'').hex()[:128],
        }

    def join_group(self, group_id: int) -> dict:
        """
        Join a PTT group to receive push messages (CMD_JOIN_GROUP=0x0003).

        Note: The server often accepts joins silently (no ACK). This is normal —
        the join is successful even without a response.

        Args:
            group_id: Group ID to join.

        Returns:
            Dict with join result.
        """
        if not self.authenticated:
            return {"success": False, "error": "Not authenticated"}

        payload = build_join_group_payload(group_id)
        logger.info("→ JoinGroup %d", group_id)
        parsed = self.request(
            CMD_JOIN_GROUP, payload, (CMD_JOIN_GROUP_ACK,), timeout=3.0
        )
        if not parsed:
            # Server accepts joins silently — this is normal, not an error
            logger.debug("No join ACK (server accepts silently — this is normal)")
            return {"success": True, "note": "No ACK, server accepts silently"}

        return {
            "success": True,
            "cmd": parsed['cmd'],
            "proto": parsed.get('proto', {}),
        }

    def group_operate(
        self,
        action: int,
        group_id: Optional[int] = None,
        group_name: str = "",
        member_ids: Iterable[int] = (),
        timeout: float = 5.0,
    ) -> dict:
        """Execute a native group mutation and require an explicit ACK."""
        if not self.authenticated:
            return {"success": False, "error": "Not authenticated"}

        payload = build_group_operate_payload(
            action,
            group_id=group_id,
            group_name=group_name,
            member_ids=member_ids,
        )
        parsed = self.request(
            CMD_GROUP_OPERATE,
            payload,
            (CMD_GROUP_OPERATE_ACK,),
            timeout=timeout,
        )
        if not parsed:
            return {"success": False, "error": "No GroupOperateAck (timeout)"}

        proto = parsed.get("proto", {})
        if 1 not in proto:
            return {
                "success": False,
                "error": "Malformed GroupOperateAck (missing result)",
                "cmd": parsed["cmd"],
                "proto": proto,
            }
        result = proto[1]
        response = {
            "success": result == PTT_RESULT_OK,
            "result": result,
            "cmd": parsed["cmd"],
            "proto": proto,
        }
        if 2 in proto:
            response["group_id"] = proto[2]
        if 3 in proto:
            response["action"] = proto[3]
        if not response["success"]:
            response["error"] = f"Group operation rejected (result={result})"
        return response

    def create_group(self, group_name: str, timeout: float = 5.0) -> dict:
        return self.group_operate(
            GROUP_OP_CREATE, group_name=group_name, timeout=timeout
        )

    def add_group_members(
        self, group_id: int, member_ids: Iterable[int], timeout: float = 5.0
    ) -> dict:
        return self.group_operate(
            GROUP_OP_ADD_USER,
            group_id=group_id,
            member_ids=member_ids,
            timeout=timeout,
        )

    def remove_group_members(
        self, group_id: int, member_ids: Iterable[int], timeout: float = 5.0
    ) -> dict:
        return self.group_operate(
            GROUP_OP_DEL_USER,
            group_id=group_id,
            member_ids=member_ids,
            timeout=timeout,
        )

    def delete_group(self, group_id: int, timeout: float = 5.0) -> dict:
        return self.group_operate(
            GROUP_OP_DELETE, group_id=group_id, timeout=timeout
        )

    def rename_group(
        self, group_id: int, group_name: str, timeout: float = 5.0
    ) -> dict:
        return self.group_operate(
            GROUP_OP_RENAME,
            group_id=group_id,
            group_name=group_name,
            timeout=timeout,
        )

    def leave_group(self, group_id: int, timeout: float = 5.0) -> dict:
        """Leave a group through the recovered native pttGroupExit path."""
        return self.group_operate(
            GROUP_OP_EXIT, group_id=group_id, timeout=timeout
        )

    def request_mic(self, group_id: int, flag: int = 0, timeout: float = 5.0) -> dict:
        """Request the floor and synchronously await RequestMicAck."""
        parsed = self.request(
            CMD_REQUEST_MIC,
            build_request_mic_payload(group_id, flag),
            (CMD_REQUEST_MIC_ACK,),
            timeout=timeout,
        )
        if not parsed:
            return {"success": False, "error": "No RequestMicAck (timeout)"}
        proto = parsed.get("proto", {})
        if 1 not in proto:
            return {
                "success": False,
                "error": "Malformed RequestMicAck (missing result)",
                "cmd": parsed["cmd"],
                "proto": proto,
            }
        ack_result = proto[1]
        if ack_result != PTT_RESULT_OK:
            return {
                "success": False,
                "error": f"RequestMic denied (result={ack_result})",
                "result": ack_result,
                "cmd": parsed["cmd"],
                "proto": proto,
            }
        return {
            "success": True,
            "result": ack_result,
            "cmd": parsed["cmd"],
            "proto": proto,
        }

    def release_mic(self, timeout: float = 5.0) -> dict:
        """Release the floor and synchronously await ReleaseMicAck."""
        parsed = self.request(
            CMD_RELEASE_MIC,
            build_release_mic_payload(),
            (CMD_RELEASE_MIC_ACK,),
            timeout=timeout,
        )
        if not parsed:
            return {"success": False, "error": "No ReleaseMicAck (timeout)"}
        proto = parsed.get("proto", {})
        if 1 not in proto:
            return {
                "success": False,
                "error": "Malformed ReleaseMicAck (missing result)",
                "cmd": parsed["cmd"],
                "proto": proto,
            }
        ack_result = proto[1]
        if ack_result != PTT_RESULT_OK:
            return {
                "success": False,
                "error": f"ReleaseMic rejected (result={ack_result})",
                "result": ack_result,
                "cmd": parsed["cmd"],
                "proto": proto,
            }
        return {
            "success": True,
            "result": ack_result,
            "cmd": parsed["cmd"],
            "proto": proto,
        }

    # ─── Heartbeat ───────────────────────────────────────────────────────────

    def _send_heartbeat(self) -> bool:
        """Send a heartbeat packet (CMD_HEARTBEAT=0x4100)."""
        # Extract numeric group ID from ptt_uid if possible, else use default
        try:
            numeric_uid = int("".join(c for c in self.ptt_uid if c.isdigit()))
        except ValueError:
            numeric_uid = 81997

        payload = build_heartbeat_payload(group_id=numeric_uid)
        return self._send_packet(CMD_HEARTBEAT, payload)

    def _heartbeat_loop(self):
        """Background heartbeat thread — sends every 40 seconds."""
        while self._running and self._listening and self.connected:
            time.sleep(HEARTBEAT_INTERVAL)
            if not self._running or not self._listening or not self.connected:
                break
            if not self._send_heartbeat():
                logger.warning("Heartbeat send failed")
                if self.on_disconnect:
                    self.on_disconnect()
                break
            logger.debug("Heartbeat sent (cmd=0x4100)")

    # ─── Message Listener ────────────────────────────────────────────────────

    def start_listening(self, block: bool = True):
        """
        Start the message listener and heartbeat threads.

        Once started, the client will:
          - Send periodic heartbeats (40s interval)
          - Receive and dispatch push messages to callbacks
          - Auto-reconnect on connection loss (if block=True)

        Args:
            block: If True, blocks until disconnect. If False, returns immediately.
        """
        self._start_receiver()
        self._listening = True

        if not self._heartbeat_thread or not self._heartbeat_thread.is_alive():
            # Send initial heartbeat
            self._send_heartbeat()
            logger.info("✓ Initial heartbeat sent")

            # Start heartbeat thread
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, daemon=True, name="ptt-heartbeat"
            )
            self._heartbeat_thread.start()

        if block:
            try:
                while self._running and self._listening and self.connected:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                logger.info("Stopping listener (Ctrl+C)")
                self.disconnect()

    def stop_listening(self):
        """Stop heartbeat/listening mode; the connection reader stays active."""
        self._listening = False
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2)

    def _recv_loop(self):
        """Background receiver thread — reads packets and dispatches to callbacks."""
        if not self.sock:
            return
        self.sock.settimeout(1.0)  # Short timeout for responsive loop
        while self._running and self.connected:
            try:
                pkt_bytes, self._recv_buffer = recv_packet(
                    self.sock, timeout=1.0, initial=self._recv_buffer
                )
                if not pkt_bytes or len(pkt_bytes) <= 2:
                    continue

                parsed = parse_packet(pkt_bytes)
                if parsed:
                    if not self._resolve_pending(parsed):
                        self._dispatch_message(parsed)
                else:
                    logger.debug("Unparseable frame (%d bytes)", len(pkt_bytes))
            except socket.timeout:
                continue
            except (ConnectionResetError, EOFError):
                logger.warning("Connection closed by server")
                self.connected = False
                self._fail_pending("Connection closed by server")
                self._emit_event("disconnect", {})
                if self.on_disconnect:
                    try:
                        self.on_disconnect()
                    except Exception:
                        logger.exception("Disconnect callback failed")
                break
            except Exception as e:
                logger.debug("Recv loop: %s", e)
                if not self.connected:
                    break

    def _dispatch_message(self, parsed: dict):
        """Dispatch a received parsed packet to the appropriate callback."""
        cmd = parsed['cmd']
        proto = parsed.get('proto', {})
        name = CMD_NAMES.get(cmd, f"UNKNOWN(0x{cmd:04x})")
        logger.info("← %s (%d bytes payload, cksum=%s)",
                    name, len(parsed.get('raw_payload', b'')), parsed.get('checksum_valid'))

        # Generic command listeners always see unmatched packets. Response
        # packets are consumed by pending requests before reaching this method.
        self._emit_event(cmd, parsed)
        self._emit_event(name, parsed)

        # Heartbeat ACK (server responds with 0x4200 or 0xFF00)
        if cmd in (CMD_HEARTBEAT_ACK, CMD_HEARTBEAT, CMD_GENERIC_ACK):
            self._last_heartbeat_ack = time.time()
            logger.debug("Heartbeat ACK received (cmd=0x%04x)", cmd)
            return

        # LoginAck (if received during listening)
        if cmd in (CMD_LOGIN_ACK, CMD_LOGIN_ACK_V2):
            if self.on_login_ack:
                self.on_login_ack(proto)
            return

        # Kickout
        if cmd == CMD_KICKOUT:
            logger.warning("Kicked from server: %s", proto)
            if self.on_kickout:
                self.on_kickout(proto)
            self.authenticated = False
            self.disconnect()
            return

        if cmd in (CMD_REQUEST_MIC, CMD_MEMBER_GET_MIC):
            if self.on_member_get_mic:
                self.on_member_get_mic(parsed)
            self._emit_event("member_get_mic", parsed)
            return

        if cmd in (CMD_RELEASE_MIC, CMD_MEMBER_LOST_MIC):
            if self.on_lost_mic:
                self.on_lost_mic(parsed)
            self._emit_event("lost_mic", parsed)
            return

        # Text message arrival (push)
        if cmd == CMD_TEXT_ARRIVED:
            msg_data = {
                "msg_id": proto.get(1, 0),
                "from_uid": proto.get(2, ""),
                "from_name": proto.get(3, ""),
                "group_id": proto.get(4, 0),
                "text": proto.get(5, ""),
                "timestamp": proto.get(6, int(time.time() * 1000)),
                "text_type": proto.get(7, 1),
            }
            logger.info("📨 Text message: %s", msg_data)
            if self.on_text_message:
                self.on_text_message(msg_data)
            return

        # Current group update
        if cmd == CMD_CURRENT_GROUP:
            logger.info("Current group update: %s", proto)
            return

        # SendText ACK (during listening mode)
        if cmd == CMD_SEND_TEXT_ACK:
            responder = proto.get(1, "")
            if isinstance(responder, bytes):
                try:
                    responder = responder.decode("utf-8")
                except Exception:
                    responder = str(responder)
            logger.info("SendText ACK from '%s'", responder)
            return

        # Unknown push type
        logger.debug("Unhandled message %s: %s", name, proto)

    # ─── Context Manager ────────────────────────────────────────────────────

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False


# ─── CLI Entry Point ──────────────────────────────────────────────────────────


def _load_config(config_path: Optional[str] = None) -> dict:
    path = config_path or os.path.join(os.path.dirname(__file__), "config.json")
    with open(path) as f:
        return json.load(f)


def main():
    """Standalone CLI for the PTT client."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Ola Radio PTT TCP Client (GWPTT/HDLC)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"PTT server host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"PTT server port (default: {DEFAULT_PORT})")
    parser.add_argument("--config", default=None, help="Path to config.json")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("test", help="Test TCP connection only")
    sub.add_parser("login", help="Login to PTT server")

    pkt_test = sub.add_parser("test-packets", help="Build and dump packets without sending")
    pkt_test.add_argument("--type", choices=["login", "heartbeat", "text"], default="login")

    send = sub.add_parser("send", help="Send a text message")
    send.add_argument("--group", required=True, help="Group ID")
    send.add_argument("--text", required=True, help="Message text")

    listen = sub.add_parser("listen", help="Listen for incoming messages")
    listen.add_argument("--group", type=int, help="Join group before listening")
    listen.add_argument("--hardcoded-login", action="store_true", help="Use hardcoded login packet")

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # ─── Packet test (no connection needed) ─────────────────────────────────
    if args.command == "test-packets":
        print("=== Packet Format Tests ===\n")
        if args.type == "login":
            pkt = make_packet(CMD_LOGIN, build_login_payload(
                login_name="10074950", ptt_token="114952210524", ptt_uid="819978xZ"))
            _dump_packet(pkt, "Dynamic Login Packet")
            _dump_packet(build_login_packet_hardcoded(), "Hardcoded Login Packet (captured)")
        elif args.type == "heartbeat":
            pkt = make_packet(CMD_HEARTBEAT, build_heartbeat_payload())
            _dump_packet(pkt, "Heartbeat Packet")
        elif args.type == "text":
            payload = build_send_text_payload(
                "16092", "Hello", "819978xZ", "10074950", "LoveDreamer")
            pkt = make_packet(CMD_SEND_TEXT, payload)
            _dump_packet(pkt, "SendText Packet")
        return 0

    # ─── Commands that need a connection ────────────────────────────────────
    config = _load_config(args.config)

    auth_cfg = config.get("auth", {})
    user_cfg = config.get("currentUser", config.get("credentials", {}))
    device_cfg = config.get("device", {})
    ptt_cfg = config.get("ptt", {})

    login_name = str(user_cfg.get("userId", user_cfg.get("account", "")))
    ptt_token = ptt_cfg.get("token", auth_cfg.get("ptt_token", ""))
    ptt_uid = ptt_cfg.get("uid", "")
    platform = ptt_cfg.get("platform", "linux")
    device_model = ptt_cfg.get("device_model", device_cfg.get("name", "CX300"))
    imei = device_cfg.get("imei", "")

    client = PttClient(host=args.host, port=args.port, config_path=args.config)

    if args.command == "test":
        if client.connect(timeout=5):
            print(f"✓ Connected to {args.host}:{args.port}")
            client.disconnect()
            return 0
        else:
            print(f"✗ Connection failed to {args.host}:{args.port}")
            return 1

    elif args.command == "login":
        if not client.connect():
            return 1
        use_hc = hasattr(args, 'hardcoded_login') and args.hardcoded_login
        result = client.login(
            login_name=login_name, ptt_token=ptt_token, ptt_uid=ptt_uid,
            platform=platform, device_model=device_model, imei=imei,
            use_hardcoded=use_hc,
        )
        print(json.dumps(result, indent=2, default=str))
        client.disconnect()
        return 0 if result.get("success") else 1

    elif args.command == "send":
        if not client.connect():
            return 1
        result = client.login(
            login_name=login_name, ptt_token=ptt_token, ptt_uid=ptt_uid,
            platform=platform, device_model=device_model, imei=imei,
        )
        if not result.get("success"):
            print(f"Login failed: {result.get('error')}")
            client.disconnect()
            return 1
        send_result = client.send_text(args.group, args.text)
        print(json.dumps(send_result, indent=2, default=str))
        client.disconnect()
        return 0 if send_result.get("success") else 1

    elif args.command == "listen":
        if not client.connect():
            return 1
        use_hc = args.hardcoded_login
        result = client.login(
            login_name=login_name, ptt_token=ptt_token, ptt_uid=ptt_uid,
            platform=platform, device_model=device_model, imei=imei,
            use_hardcoded=use_hc,
        )
        if not result.get("success"):
            print(f"Login failed: {result.get('error')}")
            client.disconnect()
            return 1

        if args.group:
            join_result = client.join_group(args.group)
            print(f"Join group {args.group}: {join_result.get('success')}")

        def on_msg(msg):
            print(f"\n📨 Message from {msg.get('from_name', msg.get('from_uid', '?'))}: "
                  f"{msg.get('text', '[no text]')}")
            print(f"   Group: {msg.get('group_id', '?')} | Time: {msg.get('timestamp', '?')}")

        client.on_text_message = on_msg

        print("📡 Listening for messages... (Ctrl+C to stop)\n")
        client.start_listening(block=True)
        return 0

    return 0


def _dump_packet(data: bytes, label: str = ""):
    """Pretty-print a GWPTT packet for debugging."""
    if label:
        print(f"\n=== {label} ===")
    print(f"Raw hex ({len(data)} bytes): {data.hex()}")
    parsed = parse_packet(data)
    if parsed:
        print(f"  cmd:         0x{parsed['cmd']:04x} ({parsed['cmd']})")
        print(f"  body_len:    {parsed['body_len']}")
        print(f"  payload:     {len(parsed['payload'])} bytes")
        print(f"  checksum:    0x{parsed['checksum']:04x}")
        print(f"  cksum_valid: {parsed['checksum_valid']}")
        if parsed['payload']:
            print(f"  payload_hex: {parsed['payload'].hex()}")
            proto = parsed.get('proto', {})
            if proto:
                print(f"  protobuf:")
                for fn, val in sorted(proto.items()):
                    if isinstance(val, bytes):
                        try:
                            s = val.decode("utf-8")
                            print(f"    field {fn}: \"{s}\"")
                        except:
                            print(f"    field {fn}: {val.hex()}")
                    else:
                        print(f"    field {fn}: {val}")
    else:
        print("  (failed to parse)")


if __name__ == "__main__":
    sys.exit(main())
