"""Wires registry, OTA rollouts and diagnostics together and runs the control loop."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from .diagnostics.assistant import DiagnosticsAssistant
from .ota import RolloutManager
from .registry import Registry

log = logging.getLogger(__name__)
KB_DIR = Path(__file__).resolve().parent.parent / "kb"


class ControlPlane:
    def __init__(self, bus, clock=time.time, llm_client=None, **registry_kw) -> None:
        self.bus = bus
        self.registry = Registry(bus, clock=clock, **registry_kw)
        self.rollouts = RolloutManager(self.registry)
        self.diagnostics = DiagnosticsAssistant(KB_DIR, llm_client=llm_client)
        self.registry.log_sinks.append(self.diagnostics.ingest_log)
        self._stop = threading.Event()
        self.on_tick = []  # extra per-tick hooks (e.g. in-process simulator in demo mode)

    def step(self) -> None:
        for hook in self.on_tick:
            hook()
        self.registry.reconcile()
        self.rollouts.step()

    def run_forever(self, interval_s: float = 1.0) -> threading.Thread:
        def loop():
            while not self._stop.is_set():
                try:
                    self.step()
                except Exception:
                    log.exception("control loop iteration failed")
                self._stop.wait(interval_s)
        t = threading.Thread(target=loop, name="control-loop", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
