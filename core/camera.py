"""Camera client for A1 / A1 mini (JPEG frames over TLS, port 6000).

Protocol (unofficial, [VERIFY] on real printer):
  - TLS connect to <ip>:6000 (self-signed cert, verification disabled).
  - Send 80-byte auth packet: <IIII 0x40, 0x3000, 0, 0> + username[32] + access_code[32].
  - Then the printer sends repeated frames: 16-byte header (first 3 bytes = payload size,
    little-endian) followed by a JPEG payload (FFD8FF... FFD9).
"""
from __future__ import annotations

import logging
import socket
import ssl
import struct
import threading
import time
from typing import Callable

from .models import PrinterProfile

log = logging.getLogger(__name__)

CAMERA_PORT = 6000
JPEG_SOI = b"\xff\xd8\xff"
JPEG_EOI = b"\xff\xd9"
MAX_FRAME_BYTES = 4_000_000
FRAME_TIMEOUT_S = 10.0
BACKOFF_S = (1, 2, 5, 10)


class ProtocolError(Exception):
    """Stream is out of sync: the connection must be re-established."""


def build_auth_packet(username: str, access_code: str) -> bytes:
    pkt = struct.pack("<IIII", 0x40, 0x3000, 0, 0)
    pkt += username.encode().ljust(32, b"\x00")[:32]
    pkt += access_code.encode().ljust(32, b"\x00")[:32]
    assert len(pkt) == 80
    return pkt


def read_frame(recv_exact: Callable[[int], bytes]) -> bytes | None:
    """Read one frame. Returns None for a corrupt JPEG (stream stays aligned),
    raises ProtocolError if the header looks wrong (stream desynced)."""
    header = recv_exact(16)
    size = int.from_bytes(header[:3], "little")
    if not 0 < size <= MAX_FRAME_BYTES:
        raise ProtocolError(f"Bad frame size in header: {size}")
    payload = recv_exact(size)
    if not payload.startswith(JPEG_SOI) or not payload.endswith(JPEG_EOI):
        log.warning("Dropped corrupt JPEG frame (%d bytes)", size)
        return None
    return payload


class CameraStream:
    """Background thread that keeps a camera connection alive and stores the latest frame."""

    def __init__(self, profile: PrinterProfile, port: int = CAMERA_PORT,
                 on_frame: Callable[[bytes], None] | None = None) -> None:
        self.profile = profile
        self.port = port
        self.on_frame = on_frame
        self.latest: bytes | None = None
        self.last_frame_at: float = 0.0
        self.frame_count = 0
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._got_frame = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- lifecycle ----
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return  # only one stream per printer
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"cam-{self.profile.name}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def wait_for_frame(self, timeout: float) -> bool:
        return self._got_frame.wait(timeout)

    @property
    def seconds_since_frame(self) -> float:
        return time.time() - self.last_frame_at if self.last_frame_at else float("inf")

    # ---- internals ----
    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                self._session()
                attempt = 0
            except (OSError, ProtocolError, ssl.SSLError) as e:
                if self._stop.is_set():
                    break  # intentional shutdown, not an error
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("Camera %s: %s", self.profile.name, self.last_error)
            delay = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
            attempt += 1
            if self._stop.wait(delay):
                break

    def _session(self) -> None:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((self.profile.ip, self.port), timeout=5)
        with ctx.wrap_socket(raw, server_hostname=self.profile.ip) as sock:
            sock.settimeout(FRAME_TIMEOUT_S)
            sock.sendall(build_auth_packet("bblp", self.profile.access_code))

            def recv_exact(n: int) -> bytes:
                buf = bytearray()
                while len(buf) < n:
                    if self._stop.is_set():
                        raise OSError("stopped")
                    chunk = sock.recv(n - len(buf))
                    if not chunk:
                        raise ConnectionError("Camera closed the connection")
                    buf += chunk
                return bytes(buf)

            while not self._stop.is_set():
                frame = read_frame(recv_exact)
                if frame is None:
                    continue
                self.latest = frame
                self.last_frame_at = time.time()
                self.frame_count += 1
                self.last_error = None
                self._got_frame.set()
                if self.on_frame:
                    try:
                        self.on_frame(frame)
                    except Exception:  # noqa: BLE001
                        log.exception("on_frame handler failed")
