"""Device registry + MQTT ingestion + desired/reported state reconciliation."""
from __future__ import annotations

import itertools
import logging
import threading
import time
from collections import deque
from collections.abc import Callable

from . import topics
from .models import Command, DesiredState, Device, DeviceStatus, RegisterRequest, ReportedState, Telemetry

log = logging.getLogger(__name__)
Clock = Callable[[], float]


class Registry:
    """Single source of truth for fleet state. Thread-safe (MQTT callbacks run on their own thread)."""

    def __init__(self, bus, clock: Clock = time.time, offline_after_s: float = 30.0,
                 cmd_timeout_s: float = 10.0, max_cmd_attempts: int = 5) -> None:
        self.bus = bus
        self.clock = clock
        self.offline_after_s = offline_after_s
        self.cmd_timeout_s = cmd_timeout_s
        self.max_cmd_attempts = max_cmd_attempts
        self.devices: dict[str, Device] = {}
        self.pending: dict[str, Command] = {}  # device_id -> outstanding command
        self.telemetry: dict[str, deque[Telemetry]] = {}
        self.ota_attempts: dict[tuple[str, str], int] = {}  # (device, version) -> OTA commands issued
        self.log_sinks: list[Callable[[dict], None]] = []
        self.telemetry_sinks: list[Callable[[Telemetry], None]] = []
        self._ids = itertools.count(1)
        self._lock = threading.RLock()

        bus.subscribe(topics.all_of("register"), self._on_register)
        bus.subscribe(topics.all_of("telemetry"), self._on_telemetry)
        bus.subscribe(topics.all_of("state"), self._on_state)
        bus.subscribe(topics.all_of("logs"), self._on_log)
        bus.subscribe(topics.all_of("cmd_ack"), self._on_ack)

    # ---------- ingestion ----------
    def register(self, req: RegisterRequest) -> Device:
        with self._lock:
            dev = self.devices.get(req.device_id)
            if dev is None:
                dev = Device(device_id=req.device_id, arch=req.arch, hw_class=req.hw_class,
                             desired=DesiredState(firmware=req.firmware),
                             reported=ReportedState(firmware=req.firmware))
                self.devices[req.device_id] = dev
                self.telemetry[req.device_id] = deque(maxlen=120)
            dev.last_seen = self.clock()
            dev.status = DeviceStatus.ONLINE
            return dev

    def _on_register(self, topic: str, p: dict) -> None:
        self.register(RegisterRequest(**p))

    def _touch(self, device_id: str) -> Device | None:
        dev = self.devices.get(device_id)
        if dev:
            dev.last_seen = self.clock()
            if dev.status == DeviceStatus.OFFLINE:
                dev.status = DeviceStatus.ONLINE
        return dev

    def _on_telemetry(self, topic: str, p: dict) -> None:
        with self._lock:
            dev = self._touch(topics.device_id_from(topic))
            if not dev:
                return
            t = Telemetry(**p)
            dev.last_telemetry = t
            self.telemetry[dev.device_id].append(t)
            dev.status = DeviceStatus.DEGRADED if (t.temp_c > 85 or t.mem_pct > 95) else (
                DeviceStatus.UPDATING if dev.status == DeviceStatus.UPDATING else DeviceStatus.ONLINE)
        for sink in self.telemetry_sinks:
            sink(t)

    def _on_state(self, topic: str, p: dict) -> None:
        with self._lock:
            dev = self._touch(topics.device_id_from(topic))
            if not dev:
                return
            dev.reported = ReportedState(**p)
            busy = dev.reported.ota_status in ("downloading", "verifying", "applying")
            dev.status = DeviceStatus.UPDATING if busy else DeviceStatus.ONLINE
            cmd = self.pending.get(dev.device_id)
            # A terminal OTA state or converged state resolves the outstanding command.
            if cmd and (dev.reported.ota_status in ("success", "failed", "rolled_back") or self.in_sync(dev)):
                self.pending.pop(dev.device_id, None)

    def _on_log(self, topic: str, p: dict) -> None:
        self._touch(topics.device_id_from(topic))
        for sink in self.log_sinks:
            sink(p)

    def _on_ack(self, topic: str, p: dict) -> None:
        with self._lock:
            cmd = self.pending.get(topics.device_id_from(topic))
            if cmd and cmd.command_id == p.get("command_id"):
                cmd.acked = True
                if cmd.action != "ota_update":  # OTA resolves on terminal state instead
                    self.pending.pop(cmd.device_id, None)

    # ---------- commands ----------
    def send_command(self, device_id: str, action: str, args: dict | None = None) -> Command:
        with self._lock:
            if device_id not in self.devices:
                raise KeyError(device_id)
            cmd = Command(command_id=f"cmd-{next(self._ids)}", device_id=device_id, action=action,
                          args=args or {}, issued_at=self.clock())
            if action in ("ota_update", "set_config"):
                self.pending[device_id] = cmd
        self.bus.publish(topics.device(device_id, "cmd"), cmd.model_dump())
        return cmd

    def set_desired(self, device_id: str, firmware: str | None = None, config: dict | None = None,
                    sha256: str = "") -> None:
        with self._lock:
            d = self.devices[device_id].desired
            if firmware:
                d.firmware = firmware
                d.config["_sha256"] = sha256  # "" = no download expected (e.g. A/B slot rollback)
            if config is not None:
                d.config.update(config)

    RETRYABLE_OTA_ERRORS = {"E_DOWNLOAD_TIMEOUT"}

    def can_retry_ota(self, dev: Device) -> bool:
        return (dev.reported.ota_error in self.RETRYABLE_OTA_ERRORS and
                self.ota_attempts.get((dev.device_id, dev.desired.firmware), 0) < self.max_cmd_attempts)

    @staticmethod
    def in_sync(dev: Device) -> bool:
        user_cfg = {k: v for k, v in dev.desired.config.items() if not k.startswith("_")}
        return dev.desired.firmware == dev.reported.firmware and all(
            dev.reported.config.get(k) == v for k, v in user_cfg.items())

    # ---------- reconciliation loop ----------
    def reconcile(self) -> dict:
        """One pass: mark offline devices, (re)issue commands for drifted devices.

        Idempotent and safe to call on any interval. A device that missed a command
        (offline, dropped packet) is re-driven once the previous attempt times out.
        """
        now = self.clock()
        stats = {"offline": 0, "drifted": 0, "issued": 0, "gave_up": 0}
        with self._lock:
            devices = list(self.devices.values())
        for dev in devices:
            if now - dev.last_seen > self.offline_after_s:
                if dev.status != DeviceStatus.OFFLINE:
                    log.info("%s offline", dev.device_id)
                dev.status = DeviceStatus.OFFLINE
                stats["offline"] += 1
                continue
            if self.in_sync(dev):
                continue
            stats["drifted"] += 1
            cmd = self.pending.get(dev.device_id)
            if cmd and now - cmd.issued_at < self.cmd_timeout_s:
                continue  # still in flight
            # Transient errors (network) are retried with a budget; permanent ones (bad image,
            # checksum, disk) are left for the rollout manager to judge — retrying can't fix them.
            if dev.reported.ota_status in ("failed", "rolled_back") and dev.desired.firmware != dev.reported.firmware \
                    and not cmd and not self.can_retry_ota(dev):
                continue
            attempts = (cmd.attempts + 1) if cmd else 1
            if attempts > self.max_cmd_attempts:
                stats["gave_up"] += 1
                continue
            if dev.desired.firmware != dev.reported.firmware:
                key = (dev.device_id, dev.desired.firmware)
                self.ota_attempts[key] = self.ota_attempts.get(key, 0) + 1
                new = self.send_command(dev.device_id, "ota_update",
                                        {"version": dev.desired.firmware,
                                         "sha256": dev.desired.config.get("_sha256", "")})
            else:
                cfg = {k: v for k, v in dev.desired.config.items() if not k.startswith("_")}
                new = self.send_command(dev.device_id, "set_config", cfg)
            new.attempts = attempts
            stats["issued"] += 1
        return stats

    def snapshot(self) -> dict:
        with self._lock:
            by_status: dict[str, int] = {}
            by_fw: dict[str, int] = {}
            for d in self.devices.values():
                by_status[d.status.value] = by_status.get(d.status.value, 0) + 1
                by_fw[d.reported.firmware] = by_fw.get(d.reported.firmware, 0) + 1
            return {"devices": len(self.devices), "by_status": by_status, "by_firmware": by_fw,
                    "pending_commands": len(self.pending)}
