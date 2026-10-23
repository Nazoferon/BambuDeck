"""PrinterClient: facade combining MQTT, accumulated state, safety checks and commands."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

from . import commands, limits
from .limits import CommandFailed, CommandRefused
from .models import PrinterProfile
from .mqtt_client import BambuMqtt
from .state import deep_merge

OFFLINE_AFTER_S = 15.0
PUSHALL_EVERY_S = 45.0
IDLE_STATES = {"IDLE", "FINISH", "FAILED"}
PRINT_STATES = {"RUNNING", "PAUSE"}


class PrinterClient:
    def __init__(self, profile: PrinterProfile, dump_path: Path | None = None) -> None:
        self.profile = profile
        self.state: dict = {}
        self.ack_timeout = 3.0
        self._lock = threading.Lock()
        self._ack_lock = threading.Lock()
        self._ack_events: dict[str, threading.Event] = {}
        self._acks: dict[str, dict] = {}
        self._got_state = threading.Event()
        self._dump_file = open(dump_path, "a", encoding="utf-8") if dump_path else None
        self._mqtt = BambuMqtt(profile, on_message=self._handle)
        self._stop = threading.Event()
        self._keeper: threading.Thread | None = None

    # ---- lifecycle ----
    def connect(self, timeout: float = 10.0) -> None:
        self._mqtt.start()
        if not self._mqtt.connected.wait(timeout):
            err = self._mqtt.last_error or f"Timeout connecting to {self.profile.ip}:8883"
            self._mqtt.stop()
            raise ConnectionError(err)
        self.request_full_state()
        self._keeper = threading.Thread(target=self._keepalive_loop, daemon=True)
        self._keeper.start()

    def close(self) -> None:
        self._stop.set()
        self._mqtt.stop()
        if self._dump_file:
            self._dump_file.close()

    def _keepalive_loop(self) -> None:
        while not self._stop.wait(PUSHALL_EVERY_S):
            try:
                self.request_full_state()
            except ConnectionError:
                pass

    # ---- incoming ----
    def _handle(self, data: dict, raw: str) -> None:
        if self._dump_file:
            self._dump_file.write(json.dumps({"t": time.time(), "msg": data}, ensure_ascii=False) + "\n")
            self._dump_file.flush()
        self._capture_ack(data)
        with self._lock:
            deep_merge(self.state, data)
        if "print" in data:
            self._got_state.set()

    def _capture_ack(self, data: dict) -> None:
        """Command replies carry our sequence_id (push_status also echoes one, so skip it)."""
        for group in data.values():
            if not isinstance(group, dict) or group.get("command") == "push_status":
                continue
            seq = group.get("sequence_id")
            if seq is None:
                continue
            with self._ack_lock:
                event = self._ack_events.get(str(seq))
                if event is not None:
                    self._acks[str(seq)] = group
                    event.set()

    # ---- state ----
    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self.state))

    def wait_for_state(self, timeout: float = 10.0) -> bool:
        return self._got_state.wait(timeout)

    def wait_until(self, predicate: Callable[[dict], bool], timeout: float = 6.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate(self.snapshot().get("print", {})):
                return True
            time.sleep(0.25)
        return False

    @property
    def gcode_state(self) -> str | None:
        with self._lock:
            return (self.state.get("print") or {}).get("gcode_state")

    @property
    def online(self) -> bool:
        return self._mqtt.connected.is_set() and self._mqtt.seconds_since_message < OFFLINE_AFTER_S

    # ---- low-level send ----
    def send(self, payload: dict) -> dict | None:
        """Publish a command and wait briefly for the printer's reply (None if no reply)."""
        group = next(iter(payload))
        seq = str(payload[group]["sequence_id"])
        event = threading.Event()
        with self._ack_lock:
            self._ack_events[seq] = event
        try:
            self._mqtt.publish(payload)
            event.wait(self.ack_timeout)
            with self._ack_lock:
                ack = self._acks.pop(seq, None)
        finally:
            with self._ack_lock:
                self._ack_events.pop(seq, None)
        if ack and str(ack.get("result", "")).lower() in ("fail", "failed"):
            raise CommandFailed(f"Printer rejected command: {ack.get('reason') or ack}")
        return ack

    # ---- safety guards ----
    def _require_idle(self, action: str) -> None:
        state = self.gcode_state
        if state is None:
            raise CommandRefused("Printer state is not known yet, wait for the first report")
        if state not in IDLE_STATES:
            raise CommandRefused(f"Cannot {action} while the printer is busy (state {state})")

    def _require_state(self, allowed: set[str], action: str) -> None:
        state = self.gcode_state
        if state not in allowed:
            raise CommandRefused(f"Cannot {action} in state {state} (allowed: {sorted(allowed)})")

    # ---- read-only requests ----
    def request_full_state(self) -> None:
        self._mqtt.publish(commands.pushall())

    def request_version(self) -> None:
        self._mqtt.publish(commands.get_version())

    # ---- light ----
    def set_light(self, on: bool, node: str = "chamber_light") -> None:
        self._mqtt.publish(commands.led(node, on))

    def light_mode(self, node: str = "chamber_light") -> str | None:
        for item in self.snapshot().get("print", {}).get("lights_report", []):
            if item.get("node") == node:
                return item.get("mode")
        return None

    def set_light_confirmed(self, on: bool, node: str = "chamber_light", timeout: float = 6.0) -> bool:
        """Send the light command and wait until lights_report confirms it (it lags by a few seconds)."""
        want = "on" if on else "off"
        self.set_light(on, node)
        deadline = time.time() + timeout
        asked = False
        while time.time() < deadline:
            if self.light_mode(node) == want:
                return True
            if not asked and time.time() > deadline - timeout / 2:
                self.request_full_state()
                asked = True
            time.sleep(0.25)
        return False

    # ---- temperatures (idle only) ----
    def set_nozzle_temp(self, temp: float) -> dict | None:
        self._require_idle("change temperature")
        t = limits.check_temp(self.profile.model, "nozzle", temp)
        return self.send(commands.gcode(f"M104 S{t}"))

    def set_bed_temp(self, temp: float) -> dict | None:
        self._require_idle("change temperature")
        t = limits.check_temp(self.profile.model, "bed", temp)
        return self.send(commands.gcode(f"M140 S{t}"))

    def heat_off(self) -> dict | None:
        self._require_idle("switch heaters off this way")
        return self.send(commands.gcode("M104 S0\nM140 S0"))

    # ---- fans / speed ----
    def set_fan(self, percent: float, fan: str = "part") -> dict | None:
        idx = limits.fan_index(self.profile.model, fan)
        pwm = limits.percent_to_pwm(percent)
        return self.send(commands.gcode(f"M106 P{idx} S{pwm}"))

    def set_speed(self, level) -> dict | None:
        lvl = limits.speed_level(level)
        self._require_state(PRINT_STATES, "change print speed")
        return self.send(commands.print_speed(lvl))

    # ---- print control ----
    def pause(self) -> dict | None:
        self._require_state({"RUNNING"}, "pause")
        return self.send(commands.pause())

    def resume(self) -> dict | None:
        self._require_state({"PAUSE"}, "resume")
        return self.send(commands.resume())

    def stop(self) -> dict | None:
        self._require_state({"RUNNING", "PAUSE", "PREPARE"}, "stop")
        return self.send(commands.stop())

    def skip_objects(self, label_ids: list[int]) -> dict | None:
        self._require_state({"RUNNING"}, "skip objects")
        ids = limits.object_label_ids(label_ids)
        return self.send(commands.skip_objects(ids))
