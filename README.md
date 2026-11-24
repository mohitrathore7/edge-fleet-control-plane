# Edge Fleet Cloud Control Plane with AI-Assisted Diagnostics

A cloud control plane for a fleet of edge AI devices. It handles device registration,
telemetry, remote commands and **staged OTA updates**, and it includes a **RAG-based diagnostics
assistant** that explains device failures from their logs. A simulated fleet of **120 virtual
edge devices** (Jetson, Raspberry Pi, i.MX8 NPU, x86 gateways, ARMv7) talks to it over MQTT
on flaky links.

`Python` · `FastAPI (REST + OpenAPI)` · `gRPC` · `MQTT` · `RAG / vector retrieval` · `LLM APIs` · `Docker multi-arch` · `GitHub Actions`

```
                 REST / OpenAPI (:8000)          gRPC (:50051)
   operators ───────────────┐      backend svcs ──────┐
                            ▼                         ▼
             ┌─────────────────────────────────────────────────┐
             │                CONTROL PLANE                    │
             │  Registry ── desired vs reported state          │
             │  Reconciler ── re-drives drifted devices        │
             │  RolloutManager ── canary → waves → halt/rollback│
             │  DiagnosticsAssistant ── vector store + LLM     │
             └───────────────────────┬─────────────────────────┘
                                     │ MQTT  fleet/{id}/{register|telemetry|state|logs|cmd_ack}
                                     │       fleet/{id}/cmd   (cloud → device)
                           ┌─────────┴─────────┐
                           │   MQTT broker     │
                           └─────────┬─────────┘
             ┌───────────┬───────────┼───────────┬───────────┐
          jetson-orin   rpi4      imx8-npu   x86-gateway   am335x    … 120 simulated devices
          (A/B slots, resumable downloads, packet loss, partitions, thermal throttling)
```

## What it does

| Capability | Where |
|---|---|
| Device registration, listing, filtering, desired state | `fleetctl/registry.py`, `fleetctl/api.py` |
| Telemetry ingestion (CPU, memory, temperature, inference latency) + gRPC streaming | `registry.py`, `grpc_server.py` (`WatchTelemetry`) |
| Remote commands: `reboot`, `set_config`, `ota_update` with acks | `registry.send_command`, `POST /v1/devices/{id}/commands` |
| **Device-state reconciliation**: desired vs reported, offline detection, retry of lost commands | `Registry.reconcile()` |
| **Staged OTA**: random canary (5%) → 25% → 100%, failure-rate gate, automatic halt + rollback | `fleetctl/ota.py` |
| A/B-slot OTA on device: resumable download → sha256 verify → apply → reboot → health check → bootloader rollback | `simulator/fleet.py` |
| **RAG diagnostics**: log + runbook retrieval, grounded LLM prompt with citations, offline fallback | `fleetctl/diagnostics/` |
| OpenAPI spec | `/docs`, `/openapi.json`, `docs/openapi.json` |

## Quick start

**No Docker, everything in one process** (control plane and 120 simulated devices on an in-memory bus):

```bash
pip install -r requirements-dev.txt
python -m fleetctl                      # REST :8000 (Swagger UI at /docs), gRPC :50051
./scripts/demo.sh                       # good rollout, bad rollout (halts), diagnostics query
```

**With a real MQTT broker** (Mosquitto, control plane and simulator run as separate containers):

```bash
docker compose up --build
./scripts/demo.sh
```

**Tests:**

```bash
pytest -q        # 15 tests, deterministic (simulated clock + seeded randomness), ~5 s
```

Optional LLM: set `LLM_API_KEY` (and optionally `LLM_BASE_URL` / `LLM_MODEL`) to any
OpenAI-compatible endpoint. Without a key, diagnostics returns a deterministic evidence summary.

## API examples

```bash
curl localhost:8000/v1/fleet/summary
curl "localhost:8000/v1/devices?status=offline"
curl -XPOST localhost:8000/v1/devices/edge-0007/commands -H 'content-type: application/json' -d '{"action":"reboot"}'
curl -XPUT  localhost:8000/v1/devices/edge-0007/desired  -H 'content-type: application/json' -d '{"firmware":"1.4.0","config":{"fps":5}}'
curl -XPOST localhost:8000/v1/rollouts -H 'content-type: application/json' \
     -d '{"version":"1.5.0","sha256":"<sha>","waves":[0.05,0.25,1.0],"max_failure_rate":0.2}'
curl -XPOST localhost:8000/v1/diagnostics/query -H 'content-type: application/json' \
     -d '{"question":"why did edge-0042 roll back?","device_id":"edge-0042"}'
```

## Design decisions

**1. Desired state + reconciliation instead of fire-and-forget commands.**
Rollouts and operators only change a device's *desired* state. One reconcile loop compares it
with *reported* state and issues commands. A command lost to a network partition times out and
is re-sent. A device that comes back online converges on its own. This is the Kubernetes /
AWS IoT device shadow pattern, and it keeps retries, offline devices and partial failures in one
code path.

**2. Transient vs permanent failures.**
`E_DOWNLOAD_TIMEOUT` is retried by the reconciler with a per-(device, version) budget.
`E_CHECKSUM_MISMATCH`, `E_NO_SPACE` and `E_HEALTHCHECK_FAILED` are not retried, because retrying
can't fix them. They count toward the rollout's failure rate instead.

**3. Staged rollout gate.**
The canary is a random sample, so it isn't biased toward the first devices that registered. A
wave advances only when every member has finished or the wave times out. The rollout halts if the
failure rate goes over `max_failure_rate`. An early halt needs a minimum sample, so one unlucky
device can't stop a wave. On halt, every touched device is pointed back to its previous firmware.

**4. A/B slots make rollback cheap and safe.**
The device writes the new image to the inactive slot and boots it. If the post-boot health check
fails, the bootloader reverts. A control-plane rollback is a slot switch, with no re-download.
*(Bug found in testing: rollback first reused the new build's checksum, so devices rejected the
old image. Fixed by treating rollback as a slot switch. Covered by
`test_bad_build_halts_at_canary_and_rolls_back`.)*

**5. RAG that stays grounded.**
Logs are structured (`level`, `code`, `hw_class`, `firmware`) and embedded into a vector store
together with runbook sections. Retrieval combines three sources: the asked-about device's logs,
fleet-wide ERRORs (to spot patterns such as "only rpi4/imx8 crash on 1.6.0"), and runbooks. The
LLM must answer only from numbered evidence and cite `[n]`. The embedder is a dependency-free
hashing vectorizer that runs on CPU, and it sits behind an interface so you can swap in
sentence-transformers or an embeddings API. If the LLM is down, the endpoint still answers.

**6. Testable by construction.**
The clock is injected and randomness is seeded. The bus is an interface (`InMemoryBus` /
`MqttBus` with the same MQTT wildcard semantics), so a full 120-device fleet test with network
faults runs deterministically in seconds.

## Simulated failure modes

| Failure | How it shows up |
|---|---|
| Packet loss (1–15% per device) | lost telemetry/commands → reconciler retries |
| Network partitions (3–12 ticks) | device marked offline → recovers → `NET_RECONNECT` |
| Lossy download | `OTA_CHUNK_RETRY` (resumes) → `E_DOWNLOAD_TIMEOUT` after 5 consecutive failures |
| Wrong artifact digest | `E_CHECKSUM_MISMATCH`, image rejected before install |
| Low disk (80 MB devices) | `E_NO_SPACE` for the inactive slot |
| Bad build `1.6.0` on rpi4 / imx8-npu / am335x | `RUNTIME_CRASH` (SIGSEGV in delegate lib) → `OTA_ROLLBACK` |
| Thermal load | `THERMAL_THROTTLE`, inference latency ~1.8× |

## Project layout

```
fleetctl/            control plane
  bus.py             InMemoryBus / MqttBus (paho), MQTT wildcard matching
  topics.py          MQTT topic contract
  models.py          pydantic models (device, desired/reported state, rollout spec, ...)
  registry.py        registry, ingestion, commands, reconciliation
  ota.py             staged rollout manager
  diagnostics/       vector store + RAG assistant + OpenAI-compatible LLM client
  api.py             FastAPI REST
  grpc_server.py     gRPC service;  pb/ = generated stubs (scripts/gen_proto.sh)
simulator/           120-device virtual fleet (python -m simulator for MQTT mode)
proto/fleet.proto    gRPC contract
kb/                  runbooks indexed by the diagnostics assistant
tests/               bus, reconciliation, OTA, RAG, REST and gRPC tests
.github/workflows/   CI: ruff, pytest (py3.10/3.12), multi-arch image build (amd64 + arm64)
```

## Limitations / next steps

- State is in memory; production would use Postgres/etcd for the registry and a time-series DB for telemetry.
- MQTT is anonymous in the dev compose file; production needs per-device mTLS and topic ACLs (`fleet/<own-id>/#`).
- The simulator models devices; the next step is running the device agent on real hardware (e.g. Jetson) and adding those boards to CI.
- Artifact signing (e.g. TUF / Sigstore) on top of the sha256 check.
