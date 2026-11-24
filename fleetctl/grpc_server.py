"""gRPC service for backend-to-backend access (see proto/fleet.proto)."""
from __future__ import annotations

import queue
from concurrent import futures

import grpc

from .controlplane import ControlPlane
from .pb import fleet_pb2 as pb
from .pb import fleet_pb2_grpc as pb_grpc


def _to_pb(d) -> pb.Device:
    return pb.Device(device_id=d.device_id, arch=d.arch, hw_class=d.hw_class, status=d.status.value,
                     desired_firmware=d.desired.firmware, reported_firmware=d.reported.firmware,
                     last_seen=d.last_seen)


class FleetService(pb_grpc.FleetServiceServicer):
    def __init__(self, cp: ControlPlane) -> None:
        self.reg = cp.registry
        self._watchers: list[tuple[str, queue.Queue]] = []
        self.reg.telemetry_sinks.append(self._fanout)

    def _fanout(self, t) -> None:
        for dev_id, q in list(self._watchers):
            if not dev_id or dev_id == t.device_id:
                try:
                    q.put_nowait(t)
                except queue.Full:
                    pass  # slow consumer: drop rather than block ingestion

    def ListDevices(self, request, context):
        return pb.ListDevicesResponse(devices=[_to_pb(d) for d in self.reg.devices.values()
                                               if not request.status or d.status.value == request.status])

    def GetDevice(self, request, context):
        d = self.reg.devices.get(request.device_id)
        if not d:
            context.abort(grpc.StatusCode.NOT_FOUND, f"device {request.device_id} not found")
        return _to_pb(d)

    def SendCommand(self, request, context):
        if request.device_id not in self.reg.devices:
            context.abort(grpc.StatusCode.NOT_FOUND, "device not found")
        if request.action not in ("reboot", "set_config", "ota_update"):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "unsupported action")
        cmd = self.reg.send_command(request.device_id, request.action, dict(request.args))
        return pb.CommandResponse(command_id=cmd.command_id, accepted=True)

    def WatchTelemetry(self, request, context):
        q: queue.Queue = queue.Queue(maxsize=1000)
        entry = (request.device_id, q)
        self._watchers.append(entry)
        try:
            while context.is_active():
                try:
                    t = q.get(timeout=1.0)
                except queue.Empty:
                    continue
                yield pb.Telemetry(device_id=t.device_id, ts=t.ts, cpu_pct=t.cpu_pct, mem_pct=t.mem_pct,
                                   temp_c=t.temp_c, inference_ms=t.inference_ms)
        finally:
            self._watchers.remove(entry)


def serve(cp: ControlPlane, port: int = 50051) -> grpc.Server:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    pb_grpc.add_FleetServiceServicer_to_server(FleetService(cp), server)
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    return server
