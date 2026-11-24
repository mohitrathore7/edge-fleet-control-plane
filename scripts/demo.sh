#!/usr/bin/env bash
# End-to-end demo against a running control plane (python -m fleetctl  OR  docker compose up)
set -euo pipefail
API=${API:-http://localhost:8000}
sha() { python3 -c "import hashlib,sys;print(hashlib.sha256(f'edge-runtime-{sys.argv[1]}'.encode()).hexdigest())" "$1"; }

echo "== fleet summary";          curl -s $API/v1/fleet/summary; echo
echo "== good build 1.5.0 rollout"; curl -s -XPOST $API/v1/rollouts -H 'content-type: application/json' \
  -d "{\"version\":\"1.5.0\",\"sha256\":\"$(sha 1.5.0)\"}"; echo
for i in $(seq 1 12); do sleep 10; curl -s $API/v1/rollouts/ro-1; echo; done

echo "== bad build 1.6.0 rollout (crashes on rpi4/imx8/am335x -> should halt at canary)"
curl -s -XPOST $API/v1/rollouts -H 'content-type: application/json' \
  -d "{\"version\":\"1.6.0\",\"sha256\":\"$(sha 1.6.0)\"}"; echo
sleep 40; curl -s $API/v1/rollouts/ro-2; echo

echo "== ask the diagnostics assistant"
curl -s -XPOST $API/v1/diagnostics/query -H 'content-type: application/json' \
  -d '{"question":"why did the 1.6.0 update fail and roll back?"}' | python3 -m json.tool | head -40
