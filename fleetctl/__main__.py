"""Start the control plane: REST :8000, gRPC :50051, MQTT bus.

BUS=mqtt (default with docker-compose) connects to MQTT_HOST.
BUS=memory runs a self-contained demo with an in-process simulated fleet (SIM_DEVICES).
"""
import logging
import os

import uvicorn

from .api import create_app
from .bus import InMemoryBus, MqttBus
from .controlplane import ControlPlane
from .grpc_server import serve


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    mode = os.getenv("BUS", "memory")
    bus = MqttBus(os.getenv("MQTT_HOST", "localhost"), int(os.getenv("MQTT_PORT", "1883"))) if mode == "mqtt" \
        else InMemoryBus()
    cp = ControlPlane(bus, offline_after_s=float(os.getenv("OFFLINE_AFTER_S", "30")))
    if mode == "memory":
        import time

        from simulator.fleet import FleetSimulator
        sim = FleetSimulator(bus, n=int(os.getenv("SIM_DEVICES", "120")), clock=time.time)
        sim.boot()
        cp.on_tick.append(sim.tick)
        logging.info("demo mode: %d simulated devices in-process", len(sim.devices))
    cp.run_forever(float(os.getenv("RECONCILE_INTERVAL_S", "1")))
    serve(cp, int(os.getenv("GRPC_PORT", "50051")))
    uvicorn.run(create_app(cp), host="0.0.0.0", port=int(os.getenv("HTTP_PORT", "8000")), log_level="warning")


if __name__ == "__main__":
    main()
