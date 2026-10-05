"""Writes the OpenAPI specification to docs/openapi.json. The live, interactive
version is served at /docs whenever the API is running."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.api import create_api  # noqa: E402
from app.config import Settings  # noqa: E402


class _NoPlatform:  # building the schema needs no database
    settings = Settings()


Path("docs/openapi.json").write_text(json.dumps(create_api(_NoPlatform()).openapi(), indent=2) + "\n")
print("Wrote docs/openapi.json")
