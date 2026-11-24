# Runtime and device health runbook

## THERMAL_THROTTLE
SoC temperature above ~82C causes clock throttling and roughly doubles inference latency.
Remediation: check enclosure airflow, lower model fps in device config (set_config fps), or switch
to a smaller/quantized model (int8).

## NET_RECONNECT
The device lost its MQTT session and reconnected. Occasional reconnects are normal. Frequent ones
indicate a poor link; the control plane will mark the device offline after the offline timeout and
the reconciler re-sends any commands it missed.

## High inference latency
Compare inference_ms against the hw_class baseline. CPU-only boards (rpi4, am335x) are expected to
be slower than GPU/NPU boards. A sudden increase after an OTA points to a runtime regression.
