from core.commands import get_version, led, pushall
from core.config import load_profiles, save_profiles
from core.models import PrinterProfile
from core.state import deep_merge, summarize
import pytest


def test_pushall_shape():
    p = pushall()["pushing"]
    assert p["command"] == "pushall"


def test_led_payload():
    s = led("chamber_light", True)["system"]
    assert s["command"] == "ledctrl" and s["led_mode"] == "on" and s["led_node"] == "chamber_light"
    assert led("chamber_light", False)["system"]["led_mode"] == "off"


def test_sequence_ids_unique():
    ids = {get_version()["info"]["sequence_id"] for _ in range(50)}
    assert len(ids) == 50


def test_deep_merge_keeps_old_fields():
    st = {"print": {"mc_percent": 10, "nozzle_temper": 200}}
    deep_merge(st, {"print": {"mc_percent": 11}})
    assert st["print"] == {"mc_percent": 11, "nozzle_temper": 200}


def test_summarize_handles_empty_state():
    assert "State" in summarize({})


def test_profile_validation_and_no_leak():
    p = PrinterProfile(name="x", ip="192.168.1.102", serial="S", access_code="12345678")
    p.validate()
    assert "12345678" not in repr(p)
    with pytest.raises(ValueError):
        PrinterProfile(name="x", ip="bad", serial="S", access_code="12345678").validate()
    with pytest.raises(ValueError):
        PrinterProfile(
            name="x",
            ip="256.1.1.1",
            serial="S",
            access_code="12345678",
        ).validate()
    unknown_model = PrinterProfile(
        name="future",
        ip="192.168.1.103",
        serial="S2",
        access_code="12345678",
        model="FuturePrinter-9000",
    )
    unknown_model.validate()
    assert unknown_model.model == "FuturePrinter-9000"


def test_unknown_model_refuses_unsupported_model_specific_controls():
    from core.limits import CommandRefused, check_temp, fan_index

    with pytest.raises(CommandRefused, match="unknown model"):
        check_temp("FuturePrinter-9000", "bed", 60)
    with pytest.raises(CommandRefused, match="unknown model"):
        fan_index("FuturePrinter-9000", "part")
    with pytest.raises(ValueError):
        PrinterProfile(name="x", ip="1.1.1.1", serial="S", access_code="123").validate()


def test_profiles_round_trip_json_and_replace_atomically(tmp_path):
    path = tmp_path / "config" / "printers.json"
    profiles = [
        PrinterProfile(
            name="Atelier",
            ip="192.168.1.102",
            serial="SERIAL-1",
            access_code="12345678",
            model="A1MINI",
            enabled=True,
            id="stable-id",
        )
    ]

    save_profiles(profiles, path)

    loaded = load_profiles(path)
    assert loaded == profiles
    assert not path.with_name("printers.json.tmp").exists()


def test_save_profiles_rejects_invalid_profiles_without_overwriting(tmp_path):
    path = tmp_path / "printers.json"
    path.write_text('{"printers":[]}', encoding="utf-8")
    invalid = PrinterProfile(name="bad", ip="invalid", serial="S", access_code="12345678")

    with pytest.raises(ValueError):
        save_profiles([invalid], path)

    assert path.read_text(encoding="utf-8") == '{"printers":[]}'


@pytest.mark.parametrize(
    "second",
    [
        PrinterProfile(
            name="same-name",
            ip="192.168.1.103",
            serial="S2",
            access_code="12345678",
            id="two",
        ),
        PrinterProfile(
            name="second",
            ip="192.168.1.102",
            serial="S2",
            access_code="12345678",
            id="two",
        ),
        PrinterProfile(
            name="second",
            ip="192.168.1.103",
            serial="S1",
            access_code="12345678",
            id="two",
        ),
    ],
)
def test_save_profiles_rejects_duplicate_identity_fields(tmp_path, second):
    first = PrinterProfile(
        name="same-name" if second.name == "same-name" else "first",
        ip="192.168.1.102",
        serial="S1",
        access_code="12345678",
        id="one",
    )

    with pytest.raises(ValueError):
        save_profiles([first, second], tmp_path / "printers.json")


# ---- tests on real dumps from an A1 mini (firmware 01.08.01.00 + BMCU) ----
import json
from pathlib import Path

from core.state import ams_trays, fan_percent, hms_code

FIX = Path(__file__).parent / "fixtures"


def _merged(name: str) -> dict:
    st: dict = {}
    for line in (FIX / name).read_text(encoding="utf-8").splitlines():
        deep_merge(st, json.loads(line)["msg"])
    return st


def test_idle_dump():
    p = _merged("idle.jsonl")["print"]
    assert p["gcode_state"] == "IDLE" and p["s_obj"] == []
    assert "State        : IDLE" in summarize({"print": p})


def test_printing_dump():
    p = _merged("printing.jsonl")["print"]
    assert p["gcode_state"] == "RUNNING" and p["mc_percent"] == 54 and p["layer_num"] == 5
    assert p["subtask_name"].endswith(".3mf")
    assert fan_percent(p["cooling_fan_speed"]) == 87
    trays = ams_trays(p)
    assert len(trays) == 1 and trays[0]["type"] == "PETG" and trays[0]["active"]


def test_hms_format():
    assert hms_code({"attr": 83887104, "code": 65604}) == "HMS_0500_0400_0001_0044"


def test_merge_does_not_lose_fields_between_deltas():
    st = _merged("printing.jsonl")["print"]
    assert st["bed_target_temper"] == 70 and st["total_layer_num"] == 45


def test_print_error_format():
    from core.state import print_error_code
    assert print_error_code(0) == "none"
    assert print_error_code(0x0500409D) == "0500-409D"
