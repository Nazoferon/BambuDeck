"""FTPS (implicit TLS, port 990) access to the printer's SD card.

Login is 'bblp' + access code. Used to fetch the .3mf of the running job (stage 4).
Unverified on real hardware [VERIFY]: directory layout, LIST format, file locations.
"""
from __future__ import annotations

import ftplib
import ssl
from pathlib import Path

from .models import PrinterProfile


class ImplicitFtpTls(ftplib.FTP_TLS):
    """ftplib has no implicit-TLS mode: wrap the control socket as soon as it is assigned."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._raw_sock = None

    @property
    def sock(self):
        return self._raw_sock

    @sock.setter
    def sock(self, value) -> None:
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host or None)
        self._raw_sock = value


class PrinterFtp:
    def __init__(self, profile: PrinterProfile, port: int = 990, timeout: float = 15.0) -> None:
        self.profile = profile
        self.port = port
        self.timeout = timeout
        self._ftp: ImplicitFtpTls | None = None

    def __enter__(self) -> "PrinterFtp":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # self-signed printer certificate, LAN only
        ftp = ImplicitFtpTls(context=ctx)
        ftp.connect(self.profile.ip, self.port, timeout=self.timeout)
        ftp.login("bblp", self.profile.access_code)
        ftp.prot_p()  # encrypt the data channel as well
        self._ftp = ftp
        return self

    def __exit__(self, *exc) -> None:
        if self._ftp:
            try:
                self._ftp.quit()
            except Exception:  # noqa: BLE001
                self._ftp.close()
            self._ftp = None

    def list_raw(self, path: str = "/") -> list[str]:
        assert self._ftp, "use as a context manager"
        lines: list[str] = []
        self._ftp.retrlines(f"LIST {path}", lines.append)
        return lines

    def download(self, remote: str, local: Path) -> int:
        assert self._ftp, "use as a context manager"
        local.parent.mkdir(parents=True, exist_ok=True)
        with open(local, "wb") as fh:
            self._ftp.retrbinary(f"RETR {remote}", fh.write)
        return local.stat().st_size
