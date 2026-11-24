import grpc
from fastapi.testclient import TestClient

from fleetctl.api import create_app
from fleetctl.grpc_server import serve
from fleetctl.pb import fleet_pb2 as pb
from fleetctl.pb import fleet_pb2_grpc as pb_grpc
from simulator.fleet import artifact_sha


def test_rest_endpoints(h):
    h.run(10)
    c = TestClient(create_app(h.cp))
    assert c.get("/v1/fleet/summary").json()["devices"] == 120
    assert c.get("/v1/devices/edge-0001").status_code == 200
    assert c.get("/v1/devices/nope").status_code == 404
    assert sum(len(c.get(f"/v1/devices/edge-{i:04d}/telemetry").json()) for i in range(5)) > 0
    assert c.post("/v1/devices/edge-0001/commands", json={"action": "reboot"}).status_code == 202
    assert c.post("/v1/devices/edge-0001/commands", json={"action": "rm -rf"}).status_code == 422
    r = c.post("/v1/rollouts", json={"version": "1.5.0", "sha256": artifact_sha("1.5.0")})
    assert r.status_code == 201
    assert c.post("/v1/rollouts", json={"version": "1.5.0", "sha256": "x"}).status_code == 409
    assert c.post(f"/v1/rollouts/{r.json()['rollout_id']}/halt").json()["state"] == "halted"
    d = c.post("/v1/diagnostics/query", json={"question": "network reconnects"}).json()
    assert "answer" in d
    spec = c.get("/openapi.json").json()
    assert "/v1/rollouts" in spec["paths"]


def test_grpc(h):
    h.run(3)
    server = serve(h.cp, port=50999)
    try:
        stub = pb_grpc.FleetServiceStub(grpc.insecure_channel("localhost:50999"))
        assert len(stub.ListDevices(pb.ListDevicesRequest()).devices) == 120
        assert stub.GetDevice(pb.GetDeviceRequest(device_id="edge-0002")).device_id == "edge-0002"
        assert stub.SendCommand(pb.CommandRequest(device_id="edge-0002", action="reboot")).accepted
        try:
            stub.GetDevice(pb.GetDeviceRequest(device_id="missing"))
            raise AssertionError
        except grpc.RpcError as e:
            assert e.code() == grpc.StatusCode.NOT_FOUND
    finally:
        server.stop(0)
