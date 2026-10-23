"""Data models for BambuDeck."""
from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass, field

@dataclass
class PrinterProfile:
    name: str
    ip: str
    serial: str
    access_code: str
    model: str = "UNKNOWN"
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("Printer name is empty")
        try:
            ipaddress.IPv4Address(self.ip)
        except ipaddress.AddressValueError:
            raise ValueError(f"Invalid IP address: {self.ip!r}")
        if not self.serial.strip():
            raise ValueError("Serial number is required (used in MQTT topics)")
        if len(self.access_code) != 8:
            raise ValueError("Access code must be 8 characters")
        self.model = self.model.strip() or "UNKNOWN"

    def __repr__(self) -> str:  # never leak the access code into logs
        return f"PrinterProfile(name={self.name!r}, ip={self.ip!r}, serial={self.serial!r}, model={self.model!r})"
