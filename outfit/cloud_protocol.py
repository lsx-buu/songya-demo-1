"""Codec for Volcengine's original (pre-3.0) Realtime S2S binary protocol.

The protocol uses big-endian integers.  This module deliberately contains no
WebSocket or audio-device code; it only validates and packs one binary frame.
"""
from __future__ import annotations

import gzip
import json
import struct
import zlib
from dataclasses import dataclass

MAX_PAYLOAD = 4 * 1024 * 1024
_HEADER = 0x11
_JSON = 0x10
_RAW = 0x00
_CONNECT_EVENTS = {1, 2}
_SERVER_CONNECT_EVENTS = {50, 51, 52}


class ProtocolError(ValueError):
    """Raised when a frame is malformed or violates protocol limits."""


@dataclass(frozen=True)
class Frame:
    event: int | None
    session_id: str
    payload: dict | bytes
    message_type: int
    sequence: int | None = None
    error_code: int | None = None
    connection_id: str = ""


def _u32(value: int) -> bytes:
    if not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
        raise ProtocolError("协议字段超出uint32范围")
    return struct.pack(">I", value)


def _id(value: str, label: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{label}不能为空")
    raw = value.encode("utf-8")
    if len(raw) > 0xFFFFFFFF:
        raise ProtocolError(f"{label}过长")
    return _u32(len(raw)) + raw


def _json_payload(payload: dict | bytes) -> tuple[bytes, int]:
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return bytes(payload), _RAW
    if not isinstance(payload, dict):
        raise ProtocolError("JSON帧payload必须是对象")
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), _JSON
    except (TypeError, ValueError) as exc:
        raise ProtocolError("JSON payload不可序列化") from exc


def encode_event(event: int, payload: dict | bytes, session_id: str = "", *, message_type: int | None = None) -> bytes:
    """Encode a client event or an audio-only event.

    Events 1 and 2 are connection-level and carry no identifier.  All other
    events in the session range (100+) carry a four-byte length-prefixed
    session id.  Event 200 is the raw PCM TaskRequest and defaults to type 2.
    """
    if not isinstance(event, int) or not 0 <= event <= 0xFFFFFFFF:
        raise ProtocolError("event必须是uint32")
    if event in _CONNECT_EVENTS:
        if session_id:
            raise ProtocolError("连接事件不能携带session_id")
    elif event >= 100 and not session_id:
        raise ProtocolError("会话事件必须携带session_id")
    body, inferred = _json_payload(payload)
    if message_type is None:
        message_type = 2 if event == 200 or isinstance(payload, (bytes, bytearray, memoryview)) else 1
    if message_type not in (1, 2):
        raise ProtocolError("客户端消息类型只能是1(JSON)或2(音频)")
    if message_type == 2 and not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ProtocolError("音频消息payload必须是bytes")
    if message_type == 1 and inferred != _JSON:
        raise ProtocolError("JSON消息payload必须是对象")
    if len(body) > MAX_PAYLOAD:
        raise ProtocolError("payload超过4MiB限制")
    flags = 0x04  # event present; no sequence
    header = bytes((_HEADER, (message_type << 4) | flags, _JSON if message_type == 1 else _RAW, 0))
    optional = _u32(event)
    if event >= 100:
        optional += _id(session_id, "session_id")
    return header + optional + _u32(len(body)) + body


def _read_u32(data: bytes, offset: int, label: str) -> tuple[int, int]:
    if offset + 4 > len(data):
        raise ProtocolError(f"帧缺少{label}")
    return struct.unpack_from(">I", data, offset)[0], offset + 4


def _read_id(data: bytes, offset: int, label: str) -> tuple[str, int]:
    length, offset = _read_u32(data, offset, f"{label}长度")
    if length > MAX_PAYLOAD or offset + length > len(data):
        raise ProtocolError(f"{label}截断或过长")
    try:
        return data[offset:offset + length].decode("utf-8"), offset + length
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"{label}不是有效UTF-8") from exc


def _inflate(raw: bytes) -> bytes:
    try:
        dec = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
        out = dec.decompress(raw, MAX_PAYLOAD + 1)
        if len(out) > MAX_PAYLOAD or dec.unconsumed_tail or dec.unused_data or not dec.eof:
            raise ProtocolError("gzip payload超过限制或不完整")
        out += dec.flush(MAX_PAYLOAD + 1 - len(out))
    except ProtocolError:
        raise
    except zlib.error as exc:
        raise ProtocolError("gzip payload无效") from exc
    if len(out) > MAX_PAYLOAD:
        raise ProtocolError("解压后的payload超过4MiB限制")
    return out


def decode_frame(data: bytes) -> Frame:
    """Decode exactly one protocol frame, rejecting truncation and leftovers."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtocolError("帧必须是bytes")
    data = bytes(data)
    if len(data) < 8:
        raise ProtocolError("帧过短")
    if data[0] != _HEADER or data[3] != 0:
        raise ProtocolError("无效协议头")
    message_type, flags = data[1] >> 4, data[1] & 0x0F
    if flags & 0x08:
        raise ProtocolError('未知的消息标志')
    serialization, compression = data[2] >> 4, data[2] & 0x0F
    if message_type not in (1, 2, 9, 11, 15):
        raise ProtocolError("未知消息类型")
    if serialization not in (0, 1) or compression not in (0, 1):
        raise ProtocolError("不支持的序列化或压缩方式")
    offset = 4
    sequence = None
    if flags & 0x03:
        if flags & 0x03 == 1:
            value, offset = _read_u32(data, offset, "sequence")
            sequence = value
        elif flags & 0x03 == 3:
            value, offset = _read_u32(data, offset, "sequence")
            sequence = struct.unpack(">i", struct.pack(">I", value))[0]
        # 0b0010 marks a final packet without a sequence field.
    error_code = None
    if message_type == 15:
        # Error frames in the wild exist both with and without the event flag.
        # The documented form carries uint32 error code before the event.
        error_code, offset = _read_u32(data, offset, "error_code")
    event = None
    if flags & 0x04:
        event, offset = _read_u32(data, offset, "event")
    connection_id = ""
    session_id = ""
    if message_type == 15:
        # Error payloads use the optional error code/event fields but carry no
        # connection/session identifier in the legacy gateway protocol.
        pass
    elif event in _SERVER_CONNECT_EVENTS:
        connection_id, offset = _read_id(data, offset, "connection_id")
    elif event is not None and event not in _CONNECT_EVENTS and event >= 100:
        session_id, offset = _read_id(data, offset, "session_id")
    size, offset = _read_u32(data, offset, "payload长度")
    if size > MAX_PAYLOAD or offset + size != len(data):
        raise ProtocolError("payload截断、过大或帧包含残留字节")
    raw = data[offset:offset + size]
    if compression == 1:
        raw = _inflate(raw)
    if message_type in (2, 11):
        payload: dict | bytes = raw
    elif serialization != 1:
        raise ProtocolError("JSON消息必须使用JSON序列化")
    else:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("JSON payload无效") from exc
        if not isinstance(payload, dict):
            raise ProtocolError("JSON payload必须是对象")
    return Frame(event, session_id, payload, message_type, sequence, error_code, connection_id)
