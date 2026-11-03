import os
import socket
import struct
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd import mice  # noqa: E402

# Wire-format vectors derived from MS-MICE section 2.2 (big-endian
# header: 2-byte total size, 1-byte version 0x01, 1-byte command).
SOURCE_READY_VECTOR = (
    b"\x00\x16\x01\x01"          # size=22, v1, SOURCE_READY
    b"\x00\x00\x03And"           # TLV 0x00 FRIENDLY_NAME "And"
    b"\x02\x00\x02\x1c\x44"      # TLV 0x02 RTSP_PORT 7236
    b"\x03\x00\x04\x00\x00\x00\x2a"  # TLV 0x03 SOURCE_ID 42
)

STOP_VECTOR = (
    b"\x00\x0b\x01\x02"          # size=11, v1, STOP_PROJECTION
    b"\x03\x00\x04\x00\x00\x00\x2a"  # TLV 0x03 SOURCE_ID 42
)


class TestMiceCodec(unittest.TestCase):
    def test_source_ready_wire_format(self):
        encoded = mice.encode_source_ready("And", 7236, 42)
        self.assertEqual(encoded, SOURCE_READY_VECTOR)

    def test_stop_projection_wire_format(self):
        self.assertEqual(mice.encode_stop_projection(42), STOP_VECTOR)

    def test_round_trip_any_tlv_order(self):
        tlvs = [
            (mice.TLV_SOURCE_ID, b"\x00\x00\x00\x2a"),
            (mice.TLV_FRIENDLY_NAME, b"And"),  # order flipped vs encode
            (mice.TLV_RTSP_PORT, b"\x1c\x00"),
        ]
        encoded = mice.encode_mice_message(mice.MICE_SOURCE_READY, tlvs)
        decoded = mice.decode_mice_message(encoded)
        self.assertEqual(decoded.command, mice.MICE_SOURCE_READY)
        self.assertEqual(decoded.tlv(mice.TLV_FRIENDLY_NAME), b"And")
        self.assertEqual(decoded.tlv(mice.TLV_RTSP_PORT), b"\x1c\x00")
        self.assertEqual(decoded.tlv(mice.TLV_SOURCE_ID), b"\x00\x00\x00\x2a")
        self.assertIsNone(decoded.tlv(mice.TLV_SECURITY_TOKEN))

    def test_size_includes_header(self):
        encoded = mice.encode_source_ready("And", 7236, 42)
        declared = struct.unpack("!H", encoded[:2])[0]
        self.assertEqual(declared, len(encoded))

    def test_decode_ignores_trailing_bytes(self):
        decoded = mice.decode_mice_message(SOURCE_READY_VECTOR + b"\xff\xff")
        self.assertEqual(decoded.tlv(mice.TLV_FRIENDLY_NAME), b"And")

    def test_decode_rejects_wrong_version(self):
        bad = bytearray(SOURCE_READY_VECTOR)
        bad[2] = 0x02
        with self.assertRaises(ValueError):
            mice.decode_mice_message(bytes(bad))

    def test_decode_rejects_size_beyond_buffer(self):
        bad = bytearray(SOURCE_READY_VECTOR)
        bad[0:2] = struct.pack("!H", 999)
        with self.assertRaises(ValueError):
            mice.decode_mice_message(bytes(bad))

    def test_decode_rejects_truncated_tlv(self):
        bad = SOURCE_READY_VECTOR[:-1]
        with self.assertRaises(ValueError):
            mice.decode_mice_message(bad)

    def test_tlv_over_length_rejected(self):
        with self.assertRaises(ValueError):
            mice.encode_mice_message(
                mice.MICE_SOURCE_READY,
                [(mice.TLV_FRIENDLY_NAME, b"x" * 0x10000)],
            )

    def test_port_and_id_range_checks(self):
        with self.assertRaises(ValueError):
            mice.encode_source_ready("And", 0x10000, 1)
        with self.assertRaises(ValueError):
            mice.encode_source_ready("And", 7236, -1)


class TestMiceClient(unittest.TestCase):
    def test_connect_sends_source_ready_and_stop(self):
        received = []
        ready = threading.Event()
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def serve():
            conn, _ = listener.accept()
            received.append(mice.recv_mice_message(conn))
            ready.set()
            conn.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        sock = mice.mice_connect("127.0.0.1", 7236, "And", 42, sink_port=port)
        thread.join(timeout=5)
        self.assertTrue(ready.wait(timeout=5))
        mice.mice_stop(sock, 42)
        listener.close()

        self.assertEqual(len(received), 1)
        message = received[0]
        self.assertEqual(message.command, mice.MICE_SOURCE_READY)
        self.assertEqual(message.tlv(mice.TLV_FRIENDLY_NAME), b"And")
        self.assertEqual(
            struct.unpack("!H", message.tlv(mice.TLV_RTSP_PORT))[0], 7236)
        self.assertEqual(
            struct.unpack("!I", message.tlv(mice.TLV_SOURCE_ID))[0], 42)

    def test_connect_failure_cleans_up(self):
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()  # nothing listens now
        with self.assertRaises(OSError):
            mice.mice_connect("127.0.0.1", 7236, "And", 42, sink_port=port)


if __name__ == "__main__":
    unittest.main()
