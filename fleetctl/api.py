"""REST API (FastAPI). The OpenAPI spec is served at /openapi.json and /docs."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .controlplane import ControlPlane
from .models import CommandIn, DesiredState, DiagnosticsQuery, RegisterRequest, RolloutIn


def create_app(cp: ControlPlane) -> FastAPI:
    app = FastAPI(title="Edge Fleet Control Plane", version="0.1.0",
                  description="Device registry, telemetry, remote commands, staged OTA and AI diagnostics.")
    reg = cp.registry

    def get_dev(device_id: str):
        dev = reg.devices.get(device_id)
        if not dev:
            raise HTTPException(404, f"device {device_id} not found")
        return dev

    @app.get("/healthz", tags=["ops"])
    def healthz():
        return {"status": "ok"}

    @app.get("/v1/fleet/summary", tags=["fleet"])
    def summary():
        return reg.snapshot()

    @app.post("/v1/devices", status_code=201, tags=["devices"])
    def register(req: RegisterRequest):
        return reg.register(req)

    @app.get("/v1/devices", tags=["devices"])
    def list_devices(status: str | None = None, hw_class: str | None = None, limit: int = 200):
        devs = [d for d in reg.devices.values()
                if (status is None or d.status.value == status) and (hw_class is None or d.hw_class == hw_class)]
        return devs[:limit]

    @app.get("/v1/devices/{device_id}", tags=["devices"])
    def get_device(device_id: str):
        return get_dev(device_id)

    @app.get("/v1/devices/{device_id}/telemetry", tags=["telemetry"])
    def telemetry(device_id: str, last: int = 60):
        get_dev(device_id)
        return list(reg.telemetry[device_id])[-last:]

    @app.put("/v1/devices/{device_id}/desired", tags=["devices"])
    def set_desired(device_id: str, desired: DesiredState):
        get_dev(device_id)
        reg.set_desired(device_id, firmware=desired.firmware, config=desired.config)
        return get_dev(device_id).desired

    @app.post("/v1/devices/{device_id}/commands", status_code=202, tags=["commands"])
    def command(device_id: str, cmd: CommandIn):
        get_dev(device_id)
        if cmd.action not in ("reboot", "set_config", "ota_update"):
            raise HTTPException(422, f"unsupported action {cmd.action}")
        return reg.send_command(device_id, cmd.action, cmd.args)

    @app.post("/v1/rollouts", status_code=201, tags=["ota"])
    def create_rollout(spec: RolloutIn):
        try:
            return cp.rollouts.create(spec).summary()
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/v1/rollouts", tags=["ota"])
    def list_rollouts():
        return [r.summary() for r in cp.rollouts.rollouts.values()]

    @app.get("/v1/rollouts/{rollout_id}", tags=["ota"])
    def get_rollout(rollout_id: str):
        r = cp.rollouts.rollouts.get(rollout_id)
        if not r:
            raise HTTPException(404, "rollout not found")
        return r.summary()

    @app.post("/v1/rollouts/{rollout_id}/halt", tags=["ota"])
    def halt(rollout_id: str):
        r = cp.rollouts.rollouts.get(rollout_id)
        if not r:
            raise HTTPException(404, "rollout not found")
        if r.state != "running":
            raise HTTPException(409, f"rollout is {r.state}")
        return cp.rollouts.halt(r, "halted by operator").summary()

    @app.post("/v1/diagnostics/query", tags=["diagnostics"])
    def diagnose(q: DiagnosticsQuery):
        if q.device_id:
            get_dev(q.device_id)
        return cp.diagnostics.ask(q.question, q.device_id, q.top_k)

    return app
