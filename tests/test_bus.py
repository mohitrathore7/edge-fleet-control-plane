from fleetctl.bus import InMemoryBus, topic_matches


def test_wildcards():
    assert topic_matches("fleet/+/telemetry", "fleet/edge-1/telemetry")
    assert not topic_matches("fleet/+/telemetry", "fleet/edge-1/state")
    assert topic_matches("fleet/#", "fleet/edge-1/logs")
    assert not topic_matches("fleet/+", "fleet/edge-1/logs")


def test_bad_handler_does_not_block_others():
    bus, got = InMemoryBus(), []
    bus.subscribe("a/#", lambda t, p: 1 / 0)
    bus.subscribe("a/b", lambda t, p: got.append(p))
    bus.publish("a/b", {"x": 1})
    assert got == [{"x": 1}]
