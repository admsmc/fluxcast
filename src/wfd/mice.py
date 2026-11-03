"""MS-MICE (Miracast over Infrastructure) message codec and Source client.

Implements the wire protocol from [MS-MICE] section 2.2 and the minimal
Source-side sequence: connect to the Sink's TCP port 7250, announce
SOURCE_READY (section 2.2.1) so the Sink connects back to our RTSP
control port, and send STOP_PROJECTION (section 2.2.2) at teardown. The
optional DTLS and PIN phases (sections 2.2.3-2.2.6) are not implemented
in this first cut; Microsoft's receiver-implementation guidance makes
only SOURCE_READY and STOP_PROJECTION mandatory for a Source.

Message framing (section 2.2): a 2-byte big-endian total size, a 1-byte
protocol version (0x01), a 1-byte command, then a TLV array. TLVs
(section 2.2.7) are Type (1 byte), Length (2 bytes big-endian), Value.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from typing import Optional

# Commands (MS-MICE section 2.2).
MICE_SOURCE_READY = 0x01
MICE_STOP_PROJECTION = 0x02
MICE_SECURITY_HANDSHAKE = 0x03
MICE_SESSION_REQUEST = 0x04
MICE_PIN_CHALLENGE = 0x05
MICE_PIN_RESPONSE = 0x06

# TLV types (MS-MICE section 2.2.7).
TLV_FRIENDLY_NAME = 0x00
TLV_RTSP_PORT = 0x02
TLV_SOURCE_ID = 0x03
TLV_SECURITY_TOKEN = 0x04
TLV_SECURITY_OPTIONS = 0x05
TLV_PIN_CHALLENGE = 0x06
TLV_PIN_RESPONSE_REASON = 0x07

MICE_PROTOCOL_VERSION = 0x01
MICE_HEADER = struct.Struct("!HBB")  # size, version, command
MICE_TLV_HEADER = struct.Struct("!BH")  # type, length
MICE_SINK_PORT = 7250

_RECV_SIZE = 4096


@dataclass
class MiceMessage:
    """One decoded MS-MICE message."""

    command: int
    tlvs: list[tuple[int, bytes]]

    def tlv(self, tlv_type: int) -> Optional[bytes]:
        """First value for a TLV type, or None when absent (any order)."""
        for t, value in self.tlvs:
            if t == tlv_type:
                return value
        return None


def _pack_tlv(tlv_type: int, value: bytes) -> bytes:
    if len(value) > 0xFFFF:
        raise ValueError(f"TLV 0x{tlv_type:02x} value exceeds 65535 bytes")
    return MICE_TLV_HEADER.pack(tlv_type, len(value)) + value


def encode_mice_message(command: int, tlvs: list[tuple[int, bytes]]) -> bytes:
    """Encode one message; tlvs are (type, raw value) in any order."""
    body = b"".join(_pack_tlv(t, v) for t, v in tlvs)
    size = MICE_HEADER.size + len(body)
    if size > 0xFFFF:
        raise ValueError("MS-MICE message exceeds 65535 bytes")
    return MICE_HEADER.pack(size, MICE_PROTOCOL_VERSION, command) + body


def decode_mice_message(data: bytes) -> MiceMessage:
    """Decode one message from the start of data; trailing bytes after
    the size-delimited message are ignored (callers frame on Size)."""
    if len(data) < MICE_HEADER.size:
        raise ValueError("truncated MS-MICE header")
    size, version, command = MICE_HEADER.unpack_from(data, 0)
    if version != MICE_PROTOCOL_VERSION:
        raise ValueError(f"unsupported MS-MICE version 0x{version:02x}")
    if size < MICE_HEADER.size or size > len(data):
        raise ValueError(f"MS-MICE size {size} out of range")
    tlvs: list[tuple[int, bytes]] = []
    offset = MICE_HEADER.size
    end = size
    while offset < end:
        if offset + MICE_TLV_HEADER.size > end:
            raise ValueError("truncated MS-MICE TLV header")
        tlv_type, length = MICE_TLV_HEADER.unpack_from(data, offset)
        offset += MICE_TLV_HEADER.size
        if offset + length > end:
            raise ValueError("truncated MS-MICE TLV value")
        tlvs.append((tlv_type, data[offset:offset + length]))
        offset += length
    return MiceMessage(command=command, tlvs=tlvs)


def encode_source_ready(
    friendly_name: str, rtsp_port: int, source_id: int
) -> bytes:
    """SOURCE_READY (section 2.2.1): FRIENDLY_NAME, RTSP_PORT, SOURCE_ID.

    RTSP_PORT is a 2-byte big-endian value (section 2.2.7.2); SOURCE_ID
    is a 4-byte big-endian identifier (section 2.2.7.3).
    """
    if not 0 < rtsp_port <= 0xFFFF:
        raise ValueError(f"RTSP port {rtsp_port} out of range")
    if not 0 <= source_id <= 0xFFFFFFFF:
        raise ValueError(f"source id {source_id} out of range")
    return encode_mice_message(
        MICE_SOURCE_READY,
        [
            (TLV_FRIENDLY_NAME, friendly_name.encode("utf-8")),
            (TLV_RTSP_PORT, struct.pack("!H", rtsp_port)),
            (TLV_SOURCE_ID, struct.pack("!I", source_id)),
        ],
    )


def encode_stop_projection(source_id: int) -> bytes:
    """STOP_PROJECTION (section 2.2.2): identifies the session's source."""
    return encode_mice_message(
        MICE_STOP_PROJECTION,
        [(TLV_SOURCE_ID, struct.pack("!I", source_id))],
    )


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    chunks = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(min(_RECV_SIZE, remaining))
        if not chunk:
            raise ConnectionError("sink closed the MS-MICE socket")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_mice_message(sock: socket.socket) -> MiceMessage:
    """Read one Size-delimited message from a connected socket."""
    header = _recv_exact(sock, MICE_HEADER.size)
    size = MICE_HEADER.unpack(header)[0]
    body_len = size - MICE_HEADER.size
    if body_len < 0:
        raise ValueError(f"nonsense MS-MICE size {size}")
    body = _recv_exact(sock, body_len) if body_len else b""
    return decode_mice_message(header + body)


def mice_connect(
    host: str,
    rtsp_port: int,
    friendly_name: str,
    source_id: int,
    sink_port: int = MICE_SINK_PORT,
    timeout: float = 5.0,
) -> socket.socket:
    """Open the 7250 session and tell the sink to call our RTSP port.

    Per section 3.2 (Miracast Connection Complete), a failure here means
    the caller should fall back to standard Miracast; that policy lives
    in session code, not here. The returned socket must be closed by the
    caller after STOP_PROJECTION.
    """
    sock = socket.create_connection((host, sink_port), timeout=timeout)
    try:
        sock.sendall(encode_source_ready(friendly_name, rtsp_port, source_id))
    except OSError:
        sock.close()
        raise
    return sock


def mice_stop(sock: socket.socket, source_id: int) -> None:
    """Graceful teardown (section 2.2.2); never raises."""
    try:
        sock.sendall(encode_stop_projection(source_id))
    except OSError:
        pass
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
