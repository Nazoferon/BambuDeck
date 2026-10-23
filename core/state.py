"""Printer state: delta merging and human-readable summary."""
from __future__ import annotations

from typing import Any


def deep_merge(base: dict, delta: dict) -> dict:
    """Merge a partial report into the accumulated state (dicts merge, everything else is replaced)."""
    for key, value in delta.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _fmt(v: Any, suffix: str = "") -> str:
    return "—" if v is None else f"{v}{suffix}"


def _temp(v: Any) -> str:
    return "—" if v is None else f"{float(v):.1f}"


def fan_percent(v: Any) -> int | None:
    """Fan speeds in reports are strings on a 0-15 scale (seen: '13' while M106 was ~226/255)."""
    try:
        return round(int(v) * 100 / 15)
    except (TypeError, ValueError):
        return None


def hms_code(item: dict) -> str:
    """Format an HMS entry as HMS_AAAA_BBBB_CCCC_DDDD (attr high/low, code high/low)."""
    a, c = int(item.get("attr", 0)), int(item.get("code", 0))
    return f"HMS_{a >> 16:04X}_{a & 0xFFFF:04X}_{c >> 16:04X}_{c & 0xFFFF:04X}"


def print_error_code(value: Any) -> str:
    """print_error is an int; Bambu shows it as XXXX-XXXX (e.g. 0500-4001). 0 = no error."""
    if not value:
        return "none"
    v = int(value)
    return f"{v >> 16:04X}-{v & 0xFFFF:04X}"


def ams_trays(print_state: dict) -> list[dict]:
    """Flat list of filled AMS/BMCU trays; the active one is marked via tray_now."""
    ams = print_state.get("ams") or {}
    now = str(ams.get("tray_now", ""))
    out = []
    for unit in ams.get("ams", []):
        for t in unit.get("tray", []):
            if "tray_type" not in t:  # empty slot reports only {"id": "N"}
                continue
            slot = int(unit.get("id", 0)) * 4 + int(t["id"])
            out.append({
                "slot": slot,
                "type": t.get("tray_type"),
                "color": (t.get("tray_color") or "")[:6],
                "active": now == str(slot),
            })
    return out


SPEED_NAMES = {1: "silent", 2: "standard", 3: "sport", 4: "ludicrous"}


def summarize(state: dict) -> str:
    p = state.get("print", {}) or {}
    lights = {l.get("node"): l.get("mode") for l in p.get("lights_report", []) if isinstance(l, dict)}
    spd = p.get("spd_lvl")
    trays = ams_trays(p)
    tray_txt = ", ".join(
        f"#{t['slot']+1} {t['type']} #{t['color']}{' *' if t['active'] else ''}" for t in trays
    ) or "—"
    hms = [hms_code(h) for h in p.get("hms", []) if isinstance(h, dict)]
    lines = [
        f"State        : {_fmt(p.get('gcode_state'))}",
        f"Job          : {_fmt(p.get('subtask_name') or p.get('gcode_file') or None)}",
        f"Progress     : {_fmt(p.get('mc_percent'), '%')}  "
        f"layer {_fmt(p.get('layer_num'))}/{_fmt(p.get('total_layer_num'))}  "
        f"remaining {_fmt(p.get('mc_remaining_time'), ' min')}",
        f"Nozzle       : {_temp(p.get('nozzle_temper'))} / {_temp(p.get('nozzle_target_temper'))} °C",
        f"Bed          : {_temp(p.get('bed_temper'))} / {_temp(p.get('bed_target_temper'))} °C",
        f"Speed level  : {_fmt(spd)} ({SPEED_NAMES.get(spd, '?')})",
        f"Fans         : part {_fmt(fan_percent(p.get('cooling_fan_speed')), '%')}  "
        f"aux {_fmt(fan_percent(p.get('big_fan1_speed')), '%')}  "
        f"chamber {_fmt(fan_percent(p.get('big_fan2_speed')), '%')}",
        f"Lights       : {lights or '—'}",
        f"WiFi         : {_fmt(p.get('wifi_signal'))}",
        f"Error        : {print_error_code(p.get('print_error'))}  HMS: {', '.join(hms) or '—'}",
        f"Filament     : {tray_txt}",
        f"Reported skip IDs (s_obj): {p.get('s_obj') or '—'}",
    ]
    return "\n".join(lines)
