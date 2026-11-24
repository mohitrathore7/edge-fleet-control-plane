"""Message bus abstraction.

`InMemoryBus` lets the control plane and simulator run in one process (tests,
demo mode). `MqttBus` is the production transport over an MQTT broker.
Both use MQTT topic semantics, including `+` and `#` wildcards.
"""
from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from collections.abc import Callable

Handler = Callable[[str, dict], None]
log = logging.getLogger(__name__)


def topic_matches(pattern: str, topic: str) -> bool:
    p, t = pattern.split("/"), topic.split("/")
    for i, seg in enumerate(p):
        if seg == "#":
            return True
        if i >= len(t) or (seg != "+" and seg != t[i]):
            return False
    return len(p) == len(t)


class InMemoryBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)
        self._lock = threading.RLock()

    def subscribe(self, pattern: str, handler: Handler) -> None:
        with self._lock:
            self._subs[pattern].append(handler)

    def publish(self, topic: str, payload: dict, qos: int = 1) -> None:
        with self._lock:
            handlers = [h for p, hs in self._subs.items() if topic_matches(p, topic) for h in hs]
        for h in handlers:
            try:
                h(topic, payload)
            except Exception:  # one bad handler must not break delivery
                log.exception("handler failed for %s", topic)


class MqttBus:
    def __init__(self, host: str = "localhost", port: int = 1883, client_id: str = "control-plane") -> None:
        import paho.mqtt.client as mqtt

        self._subs: dict[str, list[Handler]] = defaultdict(list)
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        self._client.on_message = self._on_message
        self._client.on_connect = self._on_connect
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.connect(host, port, keepalive=30)
        self._client.loop_start()

    def _on_connect(self, client, userdata, flags, rc, props=None):
        for pattern in self._subs:  # re-subscribe after reconnect
            client.subscribe(pattern, qos=1)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            log.warning("dropping non-JSON message on %s", msg.topic)
            return
        for pattern, hs in list(self._subs.items()):
            if topic_matches(pattern, msg.topic):
                for h in hs:
                    try:
                        h(msg.topic, payload)
                    except Exception:
                        log.exception("handler failed for %s", msg.topic)

    def subscribe(self, pattern: str, handler: Handler) -> None:
        self._subs[pattern].append(handler)
        self._client.subscribe(pattern, qos=1)

    def publish(self, topic: str, payload: dict, qos: int = 1) -> None:
        self._client.publish(topic, json.dumps(payload), qos=qos)

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()
