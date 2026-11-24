from fleetctl.diagnostics.vectorstore import Doc, VectorStore
from fleetctl.models import RolloutIn
from simulator.fleet import artifact_sha


def test_vector_search_ranks_relevant_first():
    vs = VectorStore()
    vs.add(Doc("1", "ERROR E_NO_SPACE inactive slot needs 96MB only 80MB free"))
    vs.add(Doc("2", "INFO OTA_SUCCESS slot committed"))
    vs.add(Doc("3", "WARN THERMAL_THROTTLE SoC temp 88C clocks throttled"))
    assert vs.search("why is the device overheating thermal throttle", 1)[0][1].doc_id == "3"
    assert vs.search("not enough free space on data", 1)[0][1].doc_id == "1"


def test_rag_explains_failed_rollout(h):
    h.run(3)
    for d in h.sim.devices.values():
        d.free_disk_mb, d.loss = 1024, 0.01  # isolate the bad-build signal from disk/network noise
    r = h.cp.rollouts.create(RolloutIn(version="1.6.0", sha256=artifact_sha("1.6.0")))
    h.run_until(lambda: r.state != "running", 1000)
    crashed = next(d for d, e in r.failed.items() if e == "E_HEALTHCHECK_FAILED")
    res = h.cp.diagnostics.ask("why did the OTA update fail and roll back?", device_id=crashed)
    assert res["mode"] == "offline"
    assert any(e["meta"].get("device_id") == crashed for e in res["evidence"])
    assert any("runbook" in e["meta"].get("source", "") for e in res["evidence"])
    assert "RUNTIME_CRASH" in res["answer"] or "OTA_ROLLBACK" in res["answer"]


def test_llm_receives_grounded_prompt(h):
    class FakeLLM:
        def complete(self, system, user):
            self.user = user
            return "root cause [1]"
    llm = FakeLLM()
    h.cp.diagnostics.llm = llm
    h.cp.diagnostics.ingest_log({"device_id": "edge-0001", "level": "ERROR", "code": "E_NO_SPACE",
                                 "msg": "only 80MB free", "hw_class": "rpi4", "firmware": "1.4.0"})
    res = h.cp.diagnostics.ask("disk space problem", device_id="edge-0001")
    assert res["mode"] == "llm" and "E_NO_SPACE" in llm.user and "[1]" in llm.user


def test_llm_failure_falls_back(h):
    class Boom:
        def complete(self, *a):
            raise TimeoutError
    h.cp.diagnostics.llm = Boom()
    h.cp.diagnostics.ingest_log({"device_id": "edge-0001", "level": "ERROR", "code": "E_NO_SPACE", "msg": "x"})
    assert h.cp.diagnostics.ask("disk", "edge-0001")["mode"] == "offline"
