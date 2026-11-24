from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class DeviceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    UPDATING = "updating"
    DEGRADED = "degraded"


class DesiredState(BaseModel):
    firmware: str
    config: dict[str, Any] = Field(default_factory=dict)


class ReportedState(BaseModel):
    firmware: str
    config: dict[str, Any] = Field(default_factory=dict)
    ota_status: str = "idle"  # idle | downloading | verifying | applying | success | failed | rolled_back
    ota_error: str | None = None


class Telemetry(BaseModel):
    device_id: str
    ts: float
    cpu_pct: float
    mem_pct: float
    temp_c: float
    inference_ms: float


class Device(BaseModel):
    device_id: str
    arch: str  # aarch64 | x86_64 | armv7
    hw_class: str  # e.g. jetson-orin, rpi4, x86-gateway
    status: DeviceStatus = DeviceStatus.ONLINE
    desired: DesiredState
    reported: ReportedState
    last_seen: float = 0.0
    last_telemetry: Telemetry | None = None


class RegisterRequest(BaseModel):
    device_id: str
    arch: str
    hw_class: str
    firmware: str


class CommandIn(BaseModel):
    action: str  # reboot | set_config | ota_update
    args: dict[str, Any] = Field(default_factory=dict)


class Command(CommandIn):
    command_id: str
    device_id: str
    issued_at: float
    attempts: int = 1
    acked: bool = False


class RolloutIn(BaseModel):
    version: str
    sha256: str
    artifact_url: str = ""
    waves: list[float] = Field(default_factory=lambda: [0.05, 0.25, 1.0])
    max_failure_rate: float = 0.2
    target_hw_class: str | None = None


class DiagnosticsQuery(BaseModel):
    question: str
    device_id: str | None = None
    top_k: int = 6
