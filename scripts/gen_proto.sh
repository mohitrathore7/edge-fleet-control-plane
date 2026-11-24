#!/usr/bin/env bash
# Regenerate gRPC stubs from proto/fleet.proto
set -euo pipefail
python -m grpc_tools.protoc -Iproto --python_out=fleetctl/pb --grpc_python_out=fleetctl/pb proto/fleet.proto
sed -i 's/^import fleet_pb2/from . import fleet_pb2/' fleetctl/pb/fleet_pb2_grpc.py
