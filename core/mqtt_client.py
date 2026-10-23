"""Thin MQTT wrapper for a single Bambu printer (LAN mode, TLS 8883)."""
from __future__ import annotations

import json
import logging
import ssl
import threading
import time
import uuid
from typing import Callable

import paho.mqtt.client as mqtt

from .models import PrinterProfile

log = logging.getLogger(__name__)

OnMessage = Callable[[dict, str], None]  # (parsed_json, raw_text)


class BambuMqtt:
    def __init__(self, profile: PrinterProfile, on_message: OnMessage | None = None) -> None:
        self.profile = profile
        self.on_message = on_message
        self.connected = threading.Event()
        self.last_error: str | None = None
        self._closing = False
        self.last_message_at: float = 0.0
        self._topic_report = f"device/{profile.serial}/report"
        self._topic_request = f"device/{profile.serial}/request"

        # Unique client_id so we don't kick Orca / Studio off the printer.
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"bambudeck-{uuid.uuid4().hex[:8]}",
        )
        self._client.username_pw_set("bblp", profile.access_code)
        # Printer uses a self-signed certificate: verification is disabled (LAN only).
        self._client.tls_set(cert_reqs=ssl.CERT_NONE)
        self._client.tls_insecure_set(True)
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    # ---- lifecycle -------------------------------------------------
    def start(self) -> None:
        self._client.connect_async(self.profile.ip, 8883, keepalive=30)
        self._client.loop_start()

    def stop(self) -> None:
        self._closing = True
        self._client.loop_stop()
        try:
            self._client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.connected.clear()

    @property
    def seconds_since_message(self) -> float:
        return time.time() - self.last_message_at if self.last_message_at else float("inf")

    # ---- publish ---------------------------------------------------
    def publish(self, payload: dict) -> None:
        if not self.connected.is_set():
            raise ConnectionError("Not connected to printer")
        info = self._client.publish(self._topic_request, json.dumps(payload), qos=0)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError(f"Publish failed: {mqtt.error_string(info.rc)}")

    # ---- callbacks -------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            msg = str(reason_code)
            if "bad user" in msg.lower() or "not authorized" in msg.lower():
                self.last_error = "Authentication failed: check the access code"
            else:
                self.last_error = f"Connection refused: {msg} (too many clients connected?)"
            log.error(self.last_error)
            return
        self.last_error = None
        client.subscribe(self._topic_report)
        self.connected.set()
        log.info("Connected to %s", self.profile)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected.clear()
        level = logging.INFO if self._closing else logging.WARNING
        log.log(level, "Disconnected from %s: %s", self.profile.name, reason_code)

    def _on_message(self, client, userdata, msg):
        self.last_message_at = time.time()
        raw = msg.payload.decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Non-JSON message ignored")
            return
        if self.on_message:
            try:
                self.on_message(data, raw)
            except Exception:  # noqa: BLE001
                log.exception("on_message handler failed")
