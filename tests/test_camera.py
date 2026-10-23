import io
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import time

import pytest

from core.camera import CameraStream, ProtocolError, build_auth_packet, read_frame
from core.models import PrinterProfile

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 100 + b"\xff\xd9"


def frame_bytes(payload: bytes) -> bytes:
    return len(payload).to_bytes(3, "little") + b"\x00" * 13 + payload


def reader(data: bytes):
    buf = io.BytesIO(data)

    def recv_exact(n):
        chunk = buf.read(n)
        if len(chunk) < n:
            raise ConnectionError("eof")
        return chunk
    return recv_exact


def test_auth_packet_layout():
    pkt = build_auth_packet("bblp", "12345678")
    assert len(pkt) == 80
    assert struct.unpack("<IIII", pkt[:16]) == (0x40, 0x3000, 0, 0)
    assert pkt[16:32].rstrip(b"\0") == b"bblp" and pkt[48:56] == b"12345678"


def test_read_valid_frame():
    assert read_frame(reader(frame_bytes(JPEG))) == JPEG


def test_corrupt_jpeg_dropped_but_stream_stays_aligned():
    bad = b"notajpeg" * 5
    rd = reader(frame_bytes(bad) + frame_bytes(JPEG))
    assert read_frame(rd) is None
    assert read_frame(rd) == JPEG


def test_bad_size_is_protocol_error():
    with pytest.raises(ProtocolError):
        read_frame(reader(b"\x00\x00\x00" + b"\x00" * 13))


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not available")
def test_camera_stream_against_fake_printer(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(cert), "-days", "1", "-subj", "/CN=fake"], check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    received = {}

    def serve():
        conn, _ = srv.accept()
        with ctx.wrap_socket(conn, server_side=True) as s:
            received["auth"] = s.recv(80)
            for _ in range(5):
                s.sendall(frame_bytes(JPEG))
                time.sleep(0.05)
            time.sleep(0.5)

    threading.Thread(target=serve, daemon=True).start()
    prof = PrinterProfile(name="fake", ip="127.0.0.1", serial="S", access_code="12345678")
    cam = CameraStream(prof, port=port)
    cam.start()
    assert cam.wait_for_frame(5)
    time.sleep(0.5)
    cam.stop()
    assert cam.latest == JPEG and cam.frame_count >= 3
    assert received["auth"] == build_auth_packet("bblp", "12345678")
