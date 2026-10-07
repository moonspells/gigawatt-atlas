"""JSON Schema export of the facility record (07 §3.1): schema/facility.v1.json, Draft 2020-12."""

from __future__ import annotations

from typing import cast

from pydantic import JsonValue

from atlas.jsonio import dumps_pretty
from atlas.schema.record import SCHEMA_VERSION, FacilityRecord

SCHEMA_ID = "https://moonspells.dev/atlas/schema/facility.v1.json"
SCHEMA_PATH = "schema/facility.v1.json"
DRAFT = "https://json-schema.org/draft/2020-12/schema"


def facility_json_schema() -> dict[str, JsonValue]:
    """The serialization-mode schema with $schema, $id, title and description."""
    generated = cast("dict[str, JsonValue]", FacilityRecord.model_json_schema(mode="serialization"))
    schema: dict[str, JsonValue] = {
        "$schema": DRAFT,
        "$id": SCHEMA_ID,
        "title": "Gigawatt Atlas facility record",
        "description": (
            f"One campus or project in the Gigawatt Atlas, schema_version {SCHEMA_VERSION}. "
            "Generated from atlas/schema/record.py by `atlas schema export`. "
            "The Atlas database is published under the Open Database License, ODbL 1.0."
        ),
    }
    schema.update({k: v for k, v in generated.items() if k not in schema})
    return schema


def render() -> str:
    """The canonical text of schema/facility.v1.json."""
    return dumps_pretty(facility_json_schema())
