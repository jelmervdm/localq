"""Pure-Python MQTT 3.1.1 wire packet codec for broker and client operation."""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass, field
from enum import IntEnum


class PacketType(IntEnum):
    """MQTT 3.1.1 control packet types."""

    CONNECT = 1
    CONNACK = 2
    PUBLISH = 3
    PUBACK = 4
    SUBSCRIBE = 8
    SUBACK = 9
    UNSUBSCRIBE = 10
    UNSUBACK = 11
    PINGREQ = 12
    PINGRESP = 13
    DISCONNECT = 14


class MqttCodecError(ValueError):
    """Raised when an incoming MQTT wire frame is malformed or truncated."""


@dataclass(slots=True)
class MqttConnectPacket:
    """Decoded MQTT ``CONNECT`` packet."""

    client_id: str
    keepalive: int = 60
    clean_session: bool = True
    username: str | None = None
    password: str | None = None
    will_topic: str | None = None
    will_message: bytes | None = None
    protocol_level: int = 4


@dataclass(slots=True)
class MqttPublishPacket:
    """Decoded MQTT ``PUBLISH`` packet."""

    topic: str
    payload: bytes
    qos: int = 0
    retain: bool = False
    dup: bool = False
    packet_id: int | None = None


@dataclass(slots=True)
class MqttSubscribePacket:
    """Decoded MQTT ``SUBSCRIBE`` packet."""

    packet_id: int
    topics: list[tuple[str, int]] = field(default_factory=list)


def encode_remaining_length(length: int) -> bytes:
    """Encode an MQTT variable-length remaining length integer (1 to 4 bytes).

    Args:
        length: Non-negative payload + variable header byte length.

    Returns:
        Encoded remaining-length bytes.
    """
    if length < 0 or length > 268_435_455:
        raise MqttCodecError(f"MQTT remaining length out of range: {length}")

    out = bytearray()
    while True:
        encoded_byte = length % 128
        length //= 128
        if length > 0:
            encoded_byte |= 0x80
        out.append(encoded_byte)
        if length == 0:
            break
    return bytes(out)


def encode_utf8_string(value: str) -> bytes:
    """Encode a length-prefixed UTF-8 string per MQTT 3.1.1 Section 1.5.3."""
    raw = value.encode("utf-8")
    return struct.pack("!H", len(raw)) + raw


def decode_utf8_string(data: memoryview, offset: int) -> tuple[str, int]:
    """Decode a length-prefixed UTF-8 string from ``data`` starting at ``offset``."""
    if offset + 2 > len(data):
        raise MqttCodecError("Truncated UTF-8 length prefix")
    length = struct.unpack_from("!H", data, offset)[0]
    offset += 2
    if offset + length > len(data):
        raise MqttCodecError("Truncated UTF-8 string payload")
    text = bytes(data[offset : offset + length]).decode("utf-8", errors="replace")
    return text, offset + length


def decode_bytes(data: memoryview, offset: int) -> tuple[bytes, int]:
    """Decode a 2-byte length-prefixed binary field from ``data``."""
    if offset + 2 > len(data):
        raise MqttCodecError("Truncated binary length prefix")
    length = struct.unpack_from("!H", data, offset)[0]
    offset += 2
    if offset + length > len(data):
        raise MqttCodecError("Truncated binary payload")
    return bytes(data[offset : offset + length]), offset + length


async def read_mqtt_packet(reader: asyncio.StreamReader) -> tuple[int, int, bytes] | None:
    """Read one complete MQTT control packet from an ``asyncio.StreamReader``.

    Args:
        reader: Stream connected to an MQTT client or broker.

    Returns:
        Tuple of ``(packet_type, flags, body_bytes)``, or ``None`` on clean EOF.
    """
    try:
        first_bytes = await reader.readexactly(1)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise MqttCodecError("Unexpected EOF reading MQTT fixed header") from exc

    header_byte = first_bytes[0]
    packet_type = (header_byte >> 4) & 0x0F
    flags = header_byte & 0x0F

    multiplier = 1
    remaining_length = 0
    for _ in range(4):
        try:
            byte = (await reader.readexactly(1))[0]
        except asyncio.IncompleteReadError as exc:
            raise MqttCodecError("Unexpected EOF reading MQTT remaining length") from exc
        remaining_length += (byte & 0x7F) * multiplier
        if (byte & 0x80) == 0:
            break
        multiplier *= 128
    else:
        raise MqttCodecError("Malformed MQTT remaining length exceeds 4 bytes")

    try:
        body = await reader.readexactly(remaining_length) if remaining_length else b""
    except asyncio.IncompleteReadError as exc:
        raise MqttCodecError("Unexpected EOF reading MQTT packet body") from exc

    return packet_type, flags, body


def encode_connect(
    client_id: str,
    keepalive: int = 60,
    clean_session: bool = True,
    username: str | None = None,
    password: str | None = None,
    will_topic: str | None = None,
    will_message: bytes | None = None,
    will_retain: bool = True,
) -> bytes:
    """Encode an MQTT 3.1.1 ``CONNECT`` packet."""
    flags = 0x02 if clean_session else 0x00
    payload = bytearray(encode_utf8_string(client_id))

    if will_topic is not None and will_message is not None:
        flags |= 0x04
        if will_retain:
            flags |= 0x20
        payload.extend(encode_utf8_string(will_topic))
        payload.extend(struct.pack("!H", len(will_message)))
        payload.extend(will_message)

    if username is not None:
        flags |= 0x80
        payload.extend(encode_utf8_string(username))
    if password is not None:
        flags |= 0x40
        raw_pw = password.encode("utf-8")
        payload.extend(struct.pack("!H", len(raw_pw)))
        payload.extend(raw_pw)

    var_header = encode_utf8_string("MQTT") + bytes([4, flags]) + struct.pack("!H", keepalive)
    body = var_header + bytes(payload)
    return bytes([PacketType.CONNECT << 4]) + encode_remaining_length(len(body)) + body


def decode_connect(body: bytes) -> MqttConnectPacket:
    """Decode an MQTT 3.1.1 (or 3.1) ``CONNECT`` packet body."""
    view = memoryview(body)
    _proto_name, offset = decode_utf8_string(view, 0)
    if offset + 4 > len(view):
        raise MqttCodecError("Truncated CONNECT variable header")

    protocol_level = view[offset]
    flags = view[offset + 1]
    keepalive = struct.unpack_from("!H", view, offset + 2)[0]
    offset += 4

    client_id, offset = decode_utf8_string(view, offset)

    will_topic: str | None = None
    will_message: bytes | None = None
    if flags & 0x04:
        will_topic, offset = decode_utf8_string(view, offset)
        will_message, offset = decode_bytes(view, offset)

    username: str | None = None
    if flags & 0x80:
        username, offset = decode_utf8_string(view, offset)

    password: str | None = None
    if flags & 0x40:
        raw_pw, offset = decode_bytes(view, offset)
        password = raw_pw.decode("utf-8", errors="replace")

    return MqttConnectPacket(
        client_id=client_id,
        keepalive=keepalive,
        clean_session=bool(flags & 0x02),
        username=username,
        password=password,
        will_topic=will_topic,
        will_message=will_message,
        protocol_level=protocol_level,
    )


def encode_connack(session_present: bool = False, return_code: int = 0) -> bytes:
    """Encode an MQTT ``CONNACK`` packet."""
    return bytes([PacketType.CONNACK << 4, 2, 1 if session_present else 0, return_code])


def encode_publish(
    topic: str,
    payload: bytes | str,
    qos: int = 0,
    retain: bool = False,
    dup: bool = False,
    packet_id: int | None = None,
) -> bytes:
    """Encode an MQTT ``PUBLISH`` packet."""
    raw_payload = payload.encode("utf-8") if isinstance(payload, str) else payload
    flags = ((1 if dup else 0) << 3) | ((qos & 0x03) << 1) | (1 if retain else 0)
    body = bytearray(encode_utf8_string(topic))
    if qos > 0:
        if packet_id is None:
            raise ValueError("packet_id is required when qos > 0")
        body.extend(struct.pack("!H", packet_id))
    body.extend(raw_payload)
    return bytes([(PacketType.PUBLISH << 4) | flags]) + encode_remaining_length(len(body)) + bytes(body)


def decode_publish(flags: int, body: bytes) -> MqttPublishPacket:
    """Decode an MQTT ``PUBLISH`` packet body."""
    view = memoryview(body)
    topic, offset = decode_utf8_string(view, 0)
    qos = (flags >> 1) & 0x03
    packet_id: int | None = None
    if qos > 0:
        if offset + 2 > len(view):
            raise MqttCodecError("Truncated PUBLISH packet_id for QoS > 0")
        packet_id = struct.unpack_from("!H", view, offset)[0]
        offset += 2
    payload = bytes(view[offset:])
    return MqttPublishPacket(
        topic=topic,
        payload=payload,
        qos=qos,
        retain=bool(flags & 0x01),
        dup=bool(flags & 0x08),
        packet_id=packet_id,
    )


def encode_puback(packet_id: int) -> bytes:
    """Encode an MQTT ``PUBACK`` packet."""
    return bytes([PacketType.PUBACK << 4, 2]) + struct.pack("!H", packet_id)


def encode_subscribe(packet_id: int, topics: list[tuple[str, int]]) -> bytes:
    """Encode an MQTT ``SUBSCRIBE`` packet."""
    body = bytearray(struct.pack("!H", packet_id))
    for topic_filter, qos in topics:
        body.extend(encode_utf8_string(topic_filter))
        body.append(qos & 0x03)
    return bytes([(PacketType.SUBSCRIBE << 4) | 0x02]) + encode_remaining_length(len(body)) + bytes(body)


def decode_subscribe(body: bytes) -> MqttSubscribePacket:
    """Decode an MQTT ``SUBSCRIBE`` packet body."""
    view = memoryview(body)
    if len(view) < 2:
        raise MqttCodecError("Truncated SUBSCRIBE packet_id")
    packet_id = struct.unpack_from("!H", view, 0)[0]
    offset = 2
    topics: list[tuple[str, int]] = []
    while offset < len(view):
        topic_filter, offset = decode_utf8_string(view, offset)
        if offset >= len(view):
            raise MqttCodecError("Truncated SUBSCRIBE requested QoS")
        topics.append((topic_filter, view[offset] & 0x03))
        offset += 1
    return MqttSubscribePacket(packet_id=packet_id, topics=topics)


def encode_suback(packet_id: int, return_codes: list[int]) -> bytes:
    """Encode an MQTT ``SUBACK`` packet."""
    body = struct.pack("!H", packet_id) + bytes(return_codes)
    return bytes([PacketType.SUBACK << 4]) + encode_remaining_length(len(body)) + body


def encode_pingreq() -> bytes:
    """Encode an MQTT ``PINGREQ`` packet."""
    return bytes([PacketType.PINGREQ << 4, 0])


def encode_pingresp() -> bytes:
    """Encode an MQTT ``PINGRESP`` packet."""
    return bytes([PacketType.PINGRESP << 4, 0])


def encode_disconnect() -> bytes:
    """Encode an MQTT ``DISCONNECT`` packet."""
    return bytes([PacketType.DISCONNECT << 4, 0])
