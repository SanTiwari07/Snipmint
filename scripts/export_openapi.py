"""Write the API schema to frontend/openapi.json (the TypeScript types are generated from it)."""

import json
from pathlib import Path

from snipmint.server import create_app

out = Path(__file__).resolve().parents[1] / "frontend" / "openapi.json"
out.write_text(json.dumps(create_app().openapi(), indent=2), encoding="utf-8")
print(f"wrote {out}")
