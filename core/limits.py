"""Safety limits and argument validation for control commands (pure, unit-testable)."""
from __future__ import annotations

import math


class CommandRefused(ValueError):
    """A command was refused by our own safety checks (nothing was sent)."""


class CommandFailed(RuntimeError):
    """The printer answered with result=fail."""


# [VERIFY] limits per model; A1 mini bed max is 80, A1 bed max is 100.
MODEL_LIMITS = {
    "A1MINI": {"nozzle_max": 300, "bed_max": 80},
    "A1": {"nozzle_max": 300, "bed_max": 100},
}

# M106 fan indexes: P1 part cooling, P2 aux, P3 chamber [VERIFY per model].
# The A1 mini report mirrors one value into all three fan fields, so only the part fan is exposed.
MODEL_FANS = {
    "A1MINI": {"part": 1},
    "A1": {"part": 1, "aux": 2},
}

SPEED_LEVELS = {"silent": 1, "standard": 2, "sport": 3, "ludicrous": 4}


def check_temp(model: str, kind: str, value: float) -> int:
    """kind: 'nozzle' | 'bed'. 0 means heater off."""
    if model not in MODEL_LIMITS:
        raise CommandRefused(f"Temperature control is unavailable for unknown model {model!r}")
    lim = MODEL_LIMITS[model][f"{kind}_max"]
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise CommandRefused(f"Invalid {kind} temperature: {value!r}")
    value = int(round(value))
    if value != 0 and not 40 <= value <= lim:
        raise CommandRefused(f"{kind} temperature must be 0 (off) or 40..{lim} °C for {model}, got {value}")
    return value


def fan_index(model: str, fan: str) -> int:
    fans = MODEL_FANS.get(model)
    if fans is None:
        raise CommandRefused(f"Fan control is unavailable for unknown model {model!r}")
    if fan not in fans:
        raise CommandRefused(f"{model} has no controllable fan {fan!r}; available: {list(fans)}")
    return fans[fan]


def percent_to_pwm(percent: float) -> int:
    if not isinstance(percent, (int, float)) or not math.isfinite(percent) or not 0 <= percent <= 100:
        raise CommandRefused(f"Fan speed must be 0..100 %, got {percent!r}")
    return round(percent * 255 / 100)


def speed_level(name_or_level) -> int:
    if isinstance(name_or_level, str) and name_or_level.lower() in SPEED_LEVELS:
        return SPEED_LEVELS[name_or_level.lower()]
    if name_or_level in SPEED_LEVELS.values():
        return int(name_or_level)
    raise CommandRefused(f"Speed must be one of {list(SPEED_LEVELS)} or 1..4, got {name_or_level!r}")


def object_label_ids(values: list[int]) -> list[int]:
    """Validate G-code object label IDs used by skip_objects."""
    if not values:
        raise CommandRefused("At least one G-code object label ID is required")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
        raise CommandRefused("G-code object label IDs must be positive integers")
    if len(set(values)) != len(values):
        raise CommandRefused("G-code object label IDs must not contain duplicates")
    return list(values)


def object_skip_flag(value: object) -> bool | None:
    """Read bit 49 from print.fun without treating it as verified skip support."""
    if not isinstance(value, str):
        return None
    flag = value.strip().lower().removeprefix("0x")
    try:
        capabilities = int(flag, 16)
    except ValueError:
        return None
    return bool((capabilities >> 49) & 1)
