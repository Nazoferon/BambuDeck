"""Loading printer profiles from printers.json."""
from __future__ import annotations

import json
from pathlib import Path

from .models import PrinterProfile

DEFAULT_PATH = Path("printers.json")


def load_profiles(path: Path = DEFAULT_PATH) -> list[PrinterProfile]:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Create it from the template in the README.")
    data = json.loads(path.read_text(encoding="utf-8"))
    profiles = []
    for raw in data.get("printers", []):
        p = PrinterProfile(**raw)
        profiles.append(p)
    _validate_profiles(profiles)
    return profiles


def _validate_profiles(profiles: list[PrinterProfile]) -> None:
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    seen_ips: set[str] = set()
    seen_serials: set[str] = set()
    for profile in profiles:
        profile.validate()
        if profile.id in seen_ids:
            raise ValueError(f"Duplicate printer profile id: {profile.id}")
        if profile.name.casefold() in seen_names:
            raise ValueError(f"Duplicate printer name: {profile.name}")
        if profile.ip in seen_ips:
            raise ValueError(f"Duplicate printer IP address: {profile.ip}")
        if profile.serial.casefold() in seen_serials:
            raise ValueError(f"Duplicate printer serial number: {profile.serial}")
        seen_ids.add(profile.id)
        seen_names.add(profile.name.casefold())
        seen_ips.add(profile.ip)
        seen_serials.add(profile.serial.casefold())


def save_profiles(profiles: list[PrinterProfile], path: Path = DEFAULT_PATH) -> None:
    _validate_profiles(profiles)

    data = {
        "printers": [
            {
                "id": profile.id,
                "name": profile.name,
                "model": profile.model,
                "ip": profile.ip,
                "serial": profile.serial,
                "access_code": profile.access_code,
                "enabled": profile.enabled,
            }
            for profile in profiles
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def find_profile(profiles: list[PrinterProfile], name: str | None) -> PrinterProfile:
    if not profiles:
        raise ValueError("No printers configured")
    if name is None:
        return profiles[0]
    for p in profiles:
        if p.name.lower() == name.lower():
            return p
    raise ValueError(f"Printer {name!r} not found. Available: {[p.name for p in profiles]}")
