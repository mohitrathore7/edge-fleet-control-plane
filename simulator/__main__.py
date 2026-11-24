"""Run the simulated fleet against a real MQTT broker: python -m simulator --devices 120"""
import argparse
import logging
import os
import time

from fleetctl.bus import MqttBus
from simulator.fleet import FleetSimulator

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--devices", type=int, default=int(os.getenv("SIM_DEVICES", "120")))
    ap.add_argument("--broker", default=os.getenv("MQTT_HOST", "localhost"))
    ap.add_argument("--tick", type=float, default=1.0)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    bus = MqttBus(a.broker, client_id="fleet-simulator")
    sim = FleetSimulator(bus, n=a.devices, clock=time.time)
    sim.boot()
    logging.info("simulating %d devices against %s", a.devices, a.broker)
    while True:
        sim.tick()
        time.sleep(a.tick)
