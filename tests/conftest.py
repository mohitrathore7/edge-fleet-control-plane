import pytest

from fleetctl.bus import InMemoryBus
from fleetctl.controlplane import ControlPlane
from simulator.fleet import FleetSimulator, SimClock


class Harness:
    def __init__(self, n=120, seed=42):
        self.bus = InMemoryBus()
        self.clock = SimClock()
        self.cp = ControlPlane(self.bus, clock=self.clock, llm_client=False, offline_after_s=15, cmd_timeout_s=8)
        self.cp.rollouts.wave_timeout_s = 200
        self.sim = FleetSimulator(self.bus, n=n, seed=seed, clock=self.clock)
        self.sim.boot()
        self.cp.on_tick.append(self.sim.tick)

    def run(self, ticks):
        for _ in range(ticks):
            self.cp.step()

    def run_until(self, cond, max_ticks=2000):
        for i in range(max_ticks):
            if cond():
                return i
            self.cp.step()
        raise AssertionError("condition not reached")


@pytest.fixture
def h():
    return Harness()
