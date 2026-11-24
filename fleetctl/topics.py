"""MQTT topic contract between devices and the control plane.

device -> cloud:  fleet/{id}/register | telemetry | state | logs | cmd_ack
cloud  -> device: fleet/{id}/cmd
"""
PREFIX = "fleet"


def device(device_id: str, kind: str) -> str:
    return f"{PREFIX}/{device_id}/{kind}"


def all_of(kind: str) -> str:
    return f"{PREFIX}/+/{kind}"


def device_id_from(topic: str) -> str:
    return topic.split("/")[1]
