"""Simulated fleet of virtual edge devices speaking the MQTT topic contract.

Each device models what makes real fleets hard:
  * heterogeneous hardware (arch / accelerator / disk / bandwidth)
  * flaky links: packet loss and multi-tick network partitions
  * A/B-slot OTA: download (resumable, retried) -> verify sha256 -> apply -> reboot -> health check
    -> automatic bootloader rollback if the new image is unhealthy
  * structured logs with error codes that the diagnostics assistant can reason over
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field

from fleetctl import topics

HW_PROFILES = [
    # hw_class, arch, accelerator, base inference ms, bandwidth (MB/tick), weight
    ("jetson-orin", "aarch64", "gpu", 9.0, 8.0, 3),
    ("rpi4", "aarch64", "cpu", 85.0, 2.0, 3),
    ("imx8-npu", "aarch64", "npu", 22.0, 3.0, 2),
    ("x86-gateway", "x86_64", "cpu", 30.0, 10.0, 1),
    ("am335x", "armv7", "cpu", 160.0, 1.0, 1),
]

IMAGE_MB = 48


def artifact_sha(version: str) -> str:
    return hashlib.sha256(f"edge-runtime-{version}".encode()).hexdigest()


# Builds with a known defect on specific hardware (used to demo halted rollouts).
BAD_BUILDS: dict[str, set[str]] = {"1.6.0": {"rpi4", "imx8-npu", "am335x"}}


@dataclass
class VirtualDevice:
    device_id: str
    hw_class: str
    arch: str
    accel: str
    base_ms: float
    bandwidth: float
    rng: random.Random
    firmware: str = "1.4.0"
    config: dict = field(default_factory=lambda: {"model": "yolov8n-int8", "fps": 15})
    loss: float = 0.02              # per-message drop probability
    free_disk_mb: int = 512
    offline_ticks: int = 0
    ota: dict | None = None      # in-flight OTA job
    prev_slot: str | None = None  # firmware still present in the inactive A/B slot
    ota_status: str = "idle"
    ota_error: str | None = None
    rebooting: int = 0
    temp: float = 45.0

    # ---- transport helpers ----
    def online(self) -> bool:
        return self.offline_ticks == 0 and self.rebooting == 0

    def send(self, bus, kind: str, payload: dict) -> None:
        if self.online() and self.rng.random() >= self.loss:
            bus.publish(topics.device(self.device_id, kind), payload)

    def log(self, bus, now: float, level: str, code: str, msg: str) -> None:
        self.send(bus, "logs", {"device_id": self.device_id, "ts": now, "level": level, "code": code,
                                "hw_class": self.hw_class, "firmware": self.firmware, "msg": msg})

    def report_state(self, bus) -> None:
        self.send(bus, "state", {"firmware": self.firmware, "config": self.config,
                                 "ota_status": self.ota_status, "ota_error": self.ota_error})

    # ---- command handling ----
    def on_command(self, bus, cmd: dict, now: float) -> None:
        if not self.online():
            return  # command lost; the control plane's reconciler will retry
        self.send(bus, "cmd_ack", {"command_id": cmd["command_id"]})
        action, args = cmd["action"], cmd.get("args", {})
        if action == "reboot":
            self.rebooting = 2
        elif action == "set_config":
            self.config.update(args)
            self.log(bus, now, "INFO", "CFG_APPLIED", f"config updated: {args}")
            self.report_state(bus)
        elif action == "ota_update":
            if self.ota or args["version"] == self.firmware:
                self.report_state(bus)
                return
            if args["version"] == self.prev_slot:  # rollback: just boot the other slot, no download
                self.ota = {"version": args["version"], "stage": "applying", "slot_switch": True}
                self.ota_status, self.ota_error = "applying", None
                self.log(bus, now, "INFO", "OTA_SLOT_SWITCH", f"switching back to slot with {self.prev_slot}")
                self.report_state(bus)
                self.rebooting = 2
                return
            self.ota = {"version": args["version"], "sha256": args.get("sha256", ""), "got_mb": 0.0,
                        "retries": 0, "stage": "downloading"}
            self.ota_status, self.ota_error = "downloading", None
            self.log(bus, now, "INFO", "OTA_START", f"OTA {self.firmware} -> {args['version']} started")
            self.report_state(bus)

    # ---- per-tick behaviour ----
    def tick(self, bus, now: float) -> None:
        if self.rebooting:
            self.rebooting -= 1
            if self.rebooting == 0:
                self._after_reboot(bus, now)
            return
        if self.offline_ticks:
            self.offline_ticks -= 1
            if self.offline_ticks == 0:
                self.log(bus, now, "WARN", "NET_RECONNECT", "MQTT session re-established after link loss")
                self.report_state(bus)
            return
        if self.rng.random() < 0.01:  # network partition of 3-12 ticks
            self.offline_ticks = self.rng.randint(3, 12)
            return
        if self.ota:
            self._ota_step(bus, now)
        self._telemetry(bus, now)

    def _telemetry(self, bus, now: float) -> None:
        load = self.rng.uniform(0.3, 0.95)
        self.temp += (40 + 45 * load - self.temp) * 0.2 + self.rng.gauss(0, 1)
        throttle = 1.8 if self.temp > 82 else 1.0
        inf = self.base_ms * throttle * self.rng.uniform(0.9, 1.25)
        if self.temp > 82 and self.rng.random() < 0.3:
            self.log(bus, now, "WARN", "THERMAL_THROTTLE",
                     f"SoC temp {self.temp:.1f}C, clocks throttled; inference {inf:.0f}ms")
        self.send(bus, "telemetry", {"device_id": self.device_id, "ts": now, "cpu_pct": round(load * 100, 1),
                                     "mem_pct": round(self.rng.uniform(40, 80), 1),
                                     "temp_c": round(self.temp, 1), "inference_ms": round(inf, 1)})
        if self.rng.random() < 0.2:
            self.report_state(bus)  # periodic state heartbeat (lets control plane recover lost state msgs)

    def _ota_step(self, bus, now: float) -> None:
        job = self.ota
        if job["stage"] == "downloading":
            if self.rng.random() < self.loss * 2:  # chunk failure -> resume from last offset
                job["retries"] += 1
                self.log(bus, now, "WARN", "OTA_CHUNK_RETRY",
                         f"chunk fetch failed at {job['got_mb']:.0f}/{IMAGE_MB}MB, resuming (retry {job['retries']})")
            else:
                job["got_mb"] += self.bandwidth
                job["retries"] = 0  # counter tracks *consecutive* failures, like a real resumable client
            if job["retries"] > 4:
                return self._ota_fail(bus, now, "E_DOWNLOAD_TIMEOUT",
                                      f"download aborted after {job['retries']} consecutive chunk failures "
                                      f"at {job['got_mb']:.0f}MB")
            if job["got_mb"] >= IMAGE_MB:
                if self.free_disk_mb < IMAGE_MB * 2:
                    return self._ota_fail(bus, now, "E_NO_SPACE",
                                          f"inactive slot needs {IMAGE_MB * 2}MB, "
                                          f"only {self.free_disk_mb}MB free on /data")
                job["stage"], self.ota_status = "verifying", "verifying"
                self.report_state(bus)
        elif job["stage"] == "verifying":
            if job["sha256"] and job["sha256"] != artifact_sha(job["version"]):
                return self._ota_fail(bus, now, "E_CHECKSUM_MISMATCH",
                                      f"sha256 mismatch for {job['version']}: image rejected before install")
            job["stage"], self.ota_status = "applying", "applying"
            self.log(bus, now, "INFO", "OTA_APPLY", f"writing {job['version']} to inactive slot, rebooting")
            self.report_state(bus)
            self.rebooting = 2

    def _after_reboot(self, bus, now: float) -> None:
        job = self.ota
        if not job or job["stage"] != "applying":
            self.log(bus, now, "INFO", "BOOT_OK", f"rebooted on {self.firmware}")
            self.report_state(bus)
            return
        new = job["version"]
        if self.hw_class in BAD_BUILDS.get(new, set()):
            lib = "libnpu_delegate.so" if self.accel == "npu" else "libedge_infer.so"
            self.log(bus, now, "ERROR", "RUNTIME_CRASH",
                     f"inference runtime {new} crashed: SIGSEGV in {lib} during model load (3/3 boot attempts)")
            self.ota = None
            self.ota_status, self.ota_error = "rolled_back", "E_HEALTHCHECK_FAILED"
            self.log(bus, now, "ERROR", "OTA_ROLLBACK",
                     f"health check failed on {new}; bootloader reverted to slot with {self.firmware}")
        else:
            prev, self.firmware, self.ota = self.firmware, new, None
            self.prev_slot = prev
            self.ota_status, self.ota_error = "success", None
            self.log(bus, now, "INFO", "OTA_SUCCESS", f"OTA {prev} -> {new} healthy, slot committed")
        self.report_state(bus)

    def _ota_fail(self, bus, now: float, code: str, msg: str) -> None:
        self.ota = None
        self.ota_status, self.ota_error = "failed", code
        self.log(bus, now, "ERROR", code, msg)
        self.report_state(bus)


class SimClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


class FleetSimulator:
    def __init__(self, bus, n: int = 120, seed: int = 42, clock=None, tick_s: float = 1.0) -> None:
        self.bus, self.tick_s = bus, tick_s
        self.clock = clock or SimClock()
        rng = random.Random(seed)
        weights = [p[5] for p in HW_PROFILES]
        self.devices: dict[str, VirtualDevice] = {}
        for i in range(n):
            hw, arch, accel, base_ms, bw, _ = rng.choices(HW_PROFILES, weights)[0]
            d = VirtualDevice(device_id=f"edge-{i:04d}", hw_class=hw, arch=arch, accel=accel, base_ms=base_ms,
                              bandwidth=bw, rng=random.Random(seed * 1000 + i),
                              loss=rng.choice([0.01, 0.02, 0.05, 0.15]),
                              free_disk_mb=rng.choice([512, 512, 512, 1024, 80]))
            self.devices[d.device_id] = d
            bus.subscribe(topics.device(d.device_id, "cmd"),
                          lambda _t, cmd, d=d: d.on_command(self.bus, cmd, self.clock()))

    def boot(self) -> None:
        for d in self.devices.values():
            self.bus.publish(topics.device(d.device_id, "register"),
                             {"device_id": d.device_id, "arch": d.arch, "hw_class": d.hw_class,
                              "firmware": d.firmware})
            d.report_state(self.bus)

    def tick(self) -> None:
        if isinstance(self.clock, SimClock):
            self.clock.t += self.tick_s
        now = self.clock()
        for d in self.devices.values():
            d.tick(self.bus, now)
