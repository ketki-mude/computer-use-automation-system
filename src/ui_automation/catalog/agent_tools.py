"""The agent-facing contract: approved capabilities as callable tools, and the JSON Schemas
another team integrates against without reading this code.

An AI agent lists the tools (`GET /api/tools`), calls one by name with typed inputs
(`POST /api/capabilities/{name}/invoke`), and gets back the result contract
(`schemas/run_result.schema.json`).
"""

import json
from pathlib import Path

from ..models.app_profile import AppProfile
from ..models.capability import Capability
from ..models.run_result import RunResult
from ..settings import ROOT
from .request_router import tool_for

SCHEMAS_DIR = ROOT / "schemas"
SCHEMAS = {"capability": Capability, "run_result": RunResult, "app_profile": AppProfile}


def tool_catalog(capabilities: list[Capability]) -> list[dict]:
    """The approved capabilities in function-calling format (name, description, typed inputs)."""
    return [{**tool_for(c), "version": c.version, "risk": c.risk,
             "outputs": {k: {"type": v.type, "description": v.description} for k, v in c.outputs.items()},
             "business_outcomes": c.business_outcomes}
            for c in capabilities if c.status == "approved"]


def schema_documents() -> dict[str, str]:
    """File name -> JSON Schema text, generated from the Pydantic models."""
    return {f"{name}.schema.json": json.dumps(model.model_json_schema(), indent=2) + "\n"
            for name, model in SCHEMAS.items()}


def write_schemas(dest: Path = SCHEMAS_DIR) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, text in schema_documents().items():
        (dest / name).write_text(text, encoding="utf-8")
        paths.append(dest / name)
    return paths
