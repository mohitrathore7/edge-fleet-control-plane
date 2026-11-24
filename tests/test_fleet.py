from fleetctl.models import DeviceStatus, RolloutIn
from simulator.fleet import artifact_sha


def test_fleet_registers_and_streams_telemetry(h):
    assert len(h.cp.registry.devices) == 120
    h.run(10)
    with_tel = [d for d in h.cp.registry.devices.values() if d.last_telemetry]
    assert len(with_tel) > 110


def test_offline_detection_and_recovery(h):
    h.run(5)
    dev = h.sim.devices["edge-0007"]
    dev.offline_ticks = 25
    h.run(20)
    assert h.cp.registry.devices["edge-0007"].status == DeviceStatus.OFFLINE
    h.run(15)
    assert h.cp.registry.devices["edge-0007"].status != DeviceStatus.OFFLINE


def test_config_drift_is_reconciled_despite_lost_commands(h):
    reg = h.cp.registry
    dev = h.sim.devices["edge-0003"]
    dev.offline_ticks = 6  # first set_config is lost
    reg.set_desired("edge-0003", config={"fps": 5})
    h.run_until(lambda: dev.config.get("fps") == 5 and reg.in_sync(reg.devices["edge-0003"]), 200)


def test_staged_rollout_completes_for_good_build(h):
    h.run(3)
    for d in h.sim.devices.values():
        d.free_disk_mb = 1024  # isolate OTA logic from disk failures
    r = h.cp.rollouts.create(RolloutIn(version="1.5.0", sha256=artifact_sha("1.5.0"), max_failure_rate=0.25))
    seen_waves = set()
    h.run_until(lambda: seen_waves.add(r.wave) or r.state != "running", 3000)
    assert r.state == "completed", r.summary()
    assert seen_waves == {0, 1, 2}
    on_new = sum(d.firmware == "1.5.0" for d in h.sim.devices.values())
    assert on_new >= 0.75 * len(h.sim.devices)


def test_bad_build_halts_at_canary_and_rolls_back(h):
    h.run(3)
    for d in h.sim.devices.values():
        d.free_disk_mb, d.loss = 1024, 0.01  # isolate the bad-build signal from disk/network noise
    r = h.cp.rollouts.create(RolloutIn(version="1.6.0", sha256=artifact_sha("1.6.0")))
    h.run_until(lambda: r.state != "running", 1000)
    assert r.state == "halted" and r.wave == 0
    assert any(e == "E_HEALTHCHECK_FAILED" for e in r.failed.values())
    # devices outside the canary never got touched
    canary = set(r.wave_members())
    assert all(d.firmware == "1.4.0" for i, d in h.sim.devices.items() if i not in canary)
    # canary devices converge back to the previous firmware
    h.run_until(lambda: all(h.sim.devices[i].firmware == "1.4.0" for i in canary), 1500)


def test_checksum_mismatch_is_rejected(h):
    h.run(3)
    r = h.cp.rollouts.create(RolloutIn(version="1.5.0", sha256="deadbeef"))
    h.run_until(lambda: r.state != "running", 1000)
    assert r.state == "halted"
    assert "E_CHECKSUM_MISMATCH" in r.failed.values()
    assert all(d.firmware == "1.4.0" for d in h.sim.devices.values())


def test_only_one_rollout_at_a_time(h):
    import pytest
    h.cp.rollouts.create(RolloutIn(version="1.5.0", sha256=artifact_sha("1.5.0")))
    with pytest.raises(ValueError):
        h.cp.rollouts.create(RolloutIn(version="1.5.1", sha256=artifact_sha("1.5.1")))
