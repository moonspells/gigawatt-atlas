"""Canonical JSON (07 §3.1): sorted keys, two-space indent, UTF-8 and LF.

Record files, receipts and the JSON Schema are written with dumps_pretty; review queue lines and
release objects use dumps_compact. Both are byte-stable for the same input.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pydantic import BaseModel, JsonValue


def dumps_pretty(obj: object) -> str:
    """Sorted keys, two-space indent, non-ASCII kept as is, one trailing newline."""
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def dumps_compact(obj: object) -> bytes:
    """Sorted keys, no whitespace, UTF-8 bytes, no trailing newline."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def record_json(r: BaseModel) -> dict[str, JsonValue]:
    """The full JSON dump of a model: every key present, nothing excluded."""
    return cast("dict[str, JsonValue]", r.model_dump(mode="json"))


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 text with LF line ends, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def write_bytes(path: Path, data: bytes) -> None:
    """Write bytes, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def read_json(path: Path) -> JsonValue:
    """Parse a UTF-8 JSON file."""
    with path.open(encoding="utf-8") as f:
        return cast("JsonValue", json.load(f))
