"""Pure functions that build MQTT command payloads (no I/O, easy to unit test)."""
from __future__ import annotations

import itertools

_seq = itertools.count(1)


def _sid() -> str:
    return str(next(_seq))


def pushall() -> dict:
    """Ask the printer for its full state (otherwise it only sends deltas)."""
    return {"pushing": {"sequence_id": _sid(), "command": "pushall", "version": 1, "push_target": 1}}


def get_version() -> dict:
    """Firmware / module versions."""
    return {"info": {"sequence_id": _sid(), "command": "get_version"}}


def led(node: str, on: bool) -> dict:
    """Light control. A1 mini has a single light; node is usually 'chamber_light' [VERIFY]."""
    return {
        "system": {
            "sequence_id": _sid(),
            "command": "ledctrl",
            "led_node": node,
            "led_mode": "on" if on else "off",
            "led_on_time": 500,
            "led_off_time": 500,
            "loop_times": 0,
            "interval_time": 0,
        }
    }


def gcode(lines: str) -> dict:
    """Run one or several G-code lines (newline separated). Always terminated with a newline."""
    if not lines.endswith("\n"):
        lines += "\n"
    return {"print": {"sequence_id": _sid(), "command": "gcode_line", "param": lines}}


def print_speed(level: int) -> dict:
    """level 1..4 = silent/standard/sport/ludicrous."""
    return {"print": {"sequence_id": _sid(), "command": "print_speed", "param": str(level)}}


def _print_ctl(command: str) -> dict:
    return {"print": {"sequence_id": _sid(), "command": command, "param": ""}}


def pause() -> dict:
    return _print_ctl("pause")


def resume() -> dict:
    return _print_ctl("resume")


def stop() -> dict:
    return _print_ctl("stop")


def skip_objects(label_ids: list[int]) -> dict:
    """Skip G-code object labels during a print."""
    return {
        "print": {
            "sequence_id": _sid(),
            "command": "skip_objects",
            "obj_list": label_ids,
        }
    }
