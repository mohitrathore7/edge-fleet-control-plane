"""Staged OTA rollouts: canary -> waves -> full fleet, with automatic halt + rollback.

A rollout never pushes firmware directly. It only changes *desired* state for the
devices in the current wave; the reconciler drives devices toward it. That keeps
retries, offline devices and partial failures in one code path.
"""
from __future__ import annotations

import itertools
import logging
import random
from dataclasses import dataclass, field

from .models import RolloutIn
from .registry import Registry

log = logging.getLogger(__name__)


@dataclass
class Rollout:
    rollout_id: str
    spec: RolloutIn
    targets: list[str]                     # ordered: canary devices first
    previous: dict[str, str]               # device_id -> firmware before rollout (for rollback)
    wave: int = 0
    state: str = "running"                 # running | completed | halted
    wave_started_at: float = 0.0
    succeeded: set[str] = field(default_factory=set)
    failed: dict[str, str] = field(default_factory=dict)  # device_id -> error
    reason: str | None = None

    def wave_members(self, upto: int | None = None) -> list[str]:
        upto = self.wave if upto is None else upto
        n = max(1, round(len(self.targets) * self.spec.waves[upto]))
        return self.targets[:n]

    def summary(self) -> dict:
        return {"rollout_id": self.rollout_id, "version": self.spec.version, "state": self.state,
                "wave": self.wave + 1, "waves": len(self.spec.waves), "targets": len(self.targets),
                "succeeded": len(self.succeeded), "failed": len(self.failed),
                "failures": dict(list(self.failed.items())[:20]), "reason": self.reason}


class RolloutManager:
    def __init__(self, registry: Registry, wave_timeout_s: float = 120.0, seed: int = 7) -> None:
        self.reg = registry
        self.wave_timeout_s = wave_timeout_s
        self.rollouts: dict[str, Rollout] = {}
        self._ids = itertools.count(1)
        self._rng = random.Random(seed)

    def create(self, spec: RolloutIn) -> Rollout:
        if any(r.state == "running" for r in self.rollouts.values()):
            raise ValueError("another rollout is already running")
        if not spec.waves or spec.waves[-1] != 1.0 or sorted(spec.waves) != spec.waves:
            raise ValueError("waves must be increasing and end at 1.0")
        targets = [d.device_id for d in self.reg.devices.values()
                   if (spec.target_hw_class in (None, d.hw_class)) and d.reported.firmware != spec.version]
        if not targets:
            raise ValueError("no eligible devices")
        self._rng.shuffle(targets)  # canary = random sample, not "first N registered"
        r = Rollout(rollout_id=f"ro-{next(self._ids)}", spec=spec, targets=targets,
                    previous={t: self.reg.devices[t].reported.firmware for t in targets})
        self.rollouts[r.rollout_id] = r
        self._start_wave(r)
        return r

    def _start_wave(self, r: Rollout) -> None:
        r.wave_started_at = self.reg.clock()
        for dev_id in r.wave_members():
            if dev_id not in r.succeeded and dev_id not in r.failed:
                self.reg.set_desired(dev_id, firmware=r.spec.version, sha256=r.spec.sha256)
        log.info("%s wave %d/%d -> %d devices", r.rollout_id, r.wave + 1, len(r.spec.waves),
                 len(r.wave_members()))

    def step(self) -> None:
        """Evaluate running rollouts. Call periodically (after reconcile)."""
        for r in self.rollouts.values():
            if r.state == "running":
                self._step(r)

    def _step(self, r: Rollout) -> None:
        members = r.wave_members()
        for dev_id in members:
            dev = self.reg.devices[dev_id]
            if dev.reported.firmware == r.spec.version and dev.reported.ota_status == "success":
                r.succeeded.add(dev_id)
                r.failed.pop(dev_id, None)
            elif dev.reported.ota_status in ("failed", "rolled_back") and dev_id not in r.succeeded \
                    and dev.desired.firmware == r.spec.version and not self.reg.can_retry_ota(dev):
                r.failed[dev_id] = dev.reported.ota_error or dev.reported.ota_status

        done = [d for d in members if d in r.succeeded or d in r.failed]
        timed_out = self.reg.clock() - r.wave_started_at > self.wave_timeout_s
        # Judge the failure rate on devices that finished; stragglers (offline) count only on timeout.
        if done:
            rate = sum(d in r.failed for d in done) / len(done)
            # Early halt needs a minimum sample so one unlucky device can't stop a wave.
            if len(done) >= max(3, len(members) // 2) and rate > r.spec.max_failure_rate:
                return self.halt(r, f"wave {r.wave + 1} failure rate {rate:.0%} > "
                                    f"{r.spec.max_failure_rate:.0%}")
        if len(done) < len(members) and not timed_out:
            return
        if timed_out:
            for d in members:
                if d not in done:
                    r.failed[d] = "timeout"
            rate = sum(d in r.failed for d in members) / len(members)
            if rate > r.spec.max_failure_rate:
                return self.halt(r, f"wave {r.wave + 1} timed out with failure rate {rate:.0%}")
        if r.wave + 1 >= len(r.spec.waves):
            r.state = "completed"
            log.info("%s completed: %d ok, %d failed", r.rollout_id, len(r.succeeded), len(r.failed))
            return
        r.wave += 1
        self._start_wave(r)

    def halt(self, r: Rollout, reason: str) -> Rollout:
        """Stop the rollout and point every touched device back at its previous firmware."""
        r.state, r.reason = "halted", reason
        for dev_id in r.wave_members():
            self.reg.set_desired(dev_id, firmware=r.previous[dev_id], sha256="")
        log.warning("%s halted: %s", r.rollout_id, reason)
        return r
