import threading

import pytest

from core import commands, limits
from core.limits import CommandFailed, CommandRefused
from core.models import PrinterProfile
from core.printer import PrinterClient


def make_client(state: str | None, model: str = "A1MINI", fun: str | None = None):
    prof = PrinterProfile(name="t", ip="127.0.0.1", serial="S", access_code="12345678", model=model)
    c = PrinterClient(prof)
    c.ack_timeout = 0.01
    sent: list[dict] = []
    c._mqtt.publish = sent.append  # no real connection
    if state is not None:
        c.state = {"print": {"gcode_state": state}}
        if fun is not None:
            c.state["print"]["fun"] = fun
    return c, sent


# ---- command builders ----
def test_gcode_is_newline_terminated():
    assert commands.gcode("M104 S200")["print"]["param"] == "M104 S200\n"


def test_print_ctl_payloads():
    assert commands.pause()["print"]["command"] == "pause"
    assert commands.print_speed(3)["print"]["param"] == "3"
    payload = commands.skip_objects([198])["print"]
    assert payload["command"] == "skip_objects"
    assert payload["obj_list"] == [198]
    assert payload["sequence_id"]


# ---- limits ----
def test_temp_limits():
    assert limits.check_temp("A1MINI", "bed", 60) == 60
    assert limits.check_temp("A1MINI", "nozzle", 0) == 0
    for bad in (81, 20, -5, float("nan")):
        with pytest.raises(CommandRefused):
            limits.check_temp("A1MINI", "bed", bad)
    assert limits.check_temp("A1", "bed", 100) == 100
    with pytest.raises(CommandRefused):
        limits.check_temp("A1MINI", "nozzle", 301)


def test_fan_and_speed_conversion():
    assert limits.percent_to_pwm(100) == 255 and limits.percent_to_pwm(0) == 0
    with pytest.raises(CommandRefused):
        limits.percent_to_pwm(101)
    with pytest.raises(CommandRefused):
        limits.fan_index("A1MINI", "aux")
    assert limits.speed_level("sport") == 3 and limits.speed_level(4) == 4
    with pytest.raises(CommandRefused):
        limits.speed_level("warp")


def test_skip_object_label_ids_are_validated():
    assert limits.object_label_ids([198, 163]) == [198, 163]
    for bad in ([], [0], [-1], [True], [1, 1], ["198"]):
        with pytest.raises(CommandRefused):
            limits.object_label_ids(bad)


# ---- state guards ----
@pytest.mark.parametrize("state", ["RUNNING", "PAUSE", "PREPARE", None])
def test_temperature_refused_when_busy_or_unknown(state):
    c, sent = make_client(state)
    for call in (lambda: c.set_nozzle_temp(200), lambda: c.set_bed_temp(60), lambda: c.heat_off()):
        with pytest.raises(CommandRefused):
            call()
    assert sent == []  # nothing reached the printer


@pytest.mark.parametrize("state", ["IDLE", "FINISH", "FAILED"])
def test_temperature_allowed_when_idle(state):
    c, sent = make_client(state)
    c.set_nozzle_temp(200)
    c.set_bed_temp(60)
    c.heat_off()
    assert [s["print"]["param"] for s in sent] == ["M104 S200\n", "M140 S60\n", "M104 S0\nM140 S0\n"]


def test_speed_only_while_printing():
    c, sent = make_client("IDLE")
    with pytest.raises(CommandRefused):
        c.set_speed("sport")
    c2, sent2 = make_client("RUNNING")
    c2.set_speed("sport")
    assert sent2[0]["print"]["param"] == "3"


def test_pause_resume_stop_state_rules():
    c, _ = make_client("IDLE")
    for f in (c.pause, c.resume, c.stop):
        with pytest.raises(CommandRefused):
            f()
    c, sent = make_client("RUNNING")
    c.pause()
    with pytest.raises(CommandRefused):
        c.resume()
    c2, sent2 = make_client("PAUSE")
    c2.resume()
    c2.stop()
    assert [s["print"]["command"] for s in sent2] == ["resume", "stop"]


def test_fan_allowed_any_state_and_uses_pwm():
    c, sent = make_client("RUNNING")
    c.set_fan(50)
    assert sent[0]["print"]["param"] == "M106 P1 S128\n"


def test_skip_objects_allowed_only_while_printing():
    c, sent = make_client("IDLE")
    with pytest.raises(CommandRefused):
        c.skip_objects([198])
    assert sent == []

    c, sent = make_client("RUNNING")
    c.skip_objects([198])
    assert sent[0]["print"]["command"] == "skip_objects"
    assert sent[0]["print"]["obj_list"] == [198]


def test_skip_objects_rejects_invalid_ids_before_publish():
    c, sent = make_client("RUNNING")
    c.state["print"]["fun"] = "2000000000000"
    for ids in ([], [0], [-1], [198, 198]):
        with pytest.raises(CommandRefused):
            c.skip_objects(ids)
    assert sent == []


@pytest.mark.parametrize("fun", [None, "0"])
def test_skip_objects_is_not_blocked_by_unverified_fun_bit(fun):
    c, sent = make_client("RUNNING", fun=fun)
    c.skip_objects([198])
    assert sent[0]["print"]["obj_list"] == [198]


# ---- ack handling ----
def test_ack_is_matched_by_sequence_id_and_push_status_ignored():
    c, sent = make_client("IDLE")
    c.ack_timeout = 2.0

    def reply():
        while not sent:
            pass
        seq = sent[0]["print"]["sequence_id"]
        c._handle({"print": {"command": "push_status", "sequence_id": seq}}, "")  # must be ignored
        c._handle({"print": {"command": "gcode_line", "sequence_id": seq, "result": "success"}}, "")

    threading.Thread(target=reply, daemon=True).start()
    ack = c.set_bed_temp(50)
    assert ack and ack["command"] == "gcode_line" and ack["result"] == "success"


def test_failed_ack_raises():
    c, sent = make_client("IDLE")
    c.ack_timeout = 2.0

    def reply():
        while not sent:
            pass
        c._handle({"print": {"command": "gcode_line", "sequence_id": sent[0]["print"]["sequence_id"],
                             "result": "fail", "reason": "rejected"}}, "")

    threading.Thread(target=reply, daemon=True).start()
    with pytest.raises(CommandFailed):
        c.set_bed_temp(50)


def test_ftp_wrapper_imports_and_wraps_socket():
    import socket
    import ssl
    from core.ftp_client import ImplicitFtpTls
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    f = ImplicitFtpTls(context=ctx)
    assert f.sock is None
    a, b = socket.socketpair()
    try:
        with pytest.raises((ssl.SSLError, OSError)):
            a.settimeout(0.2)
            f.sock = a  # plain socket must get TLS-wrapped (handshake fails on a socketpair)
    finally:
        a.close()
        b.close()
