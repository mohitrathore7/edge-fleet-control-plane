"""Write the REST API's OpenAPI spec to docs/openapi.json."""
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import json
from pathlib import Path

from fleetctl.api import create_app
from fleetctl.bus import InMemoryBus
from fleetctl.controlplane import ControlPlane

spec = create_app(ControlPlane(InMemoryBus(), llm_client=False)).openapi()
Path("docs/openapi.json").write_text(json.dumps(spec, indent=2))
print("wrote docs/openapi.json with", len(spec["paths"]), "paths")
