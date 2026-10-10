"""Canonical JSON (07 §3.1): sorted keys, two-space indent, UTF-8 and LF.

Record files, receipts and the JSON Schema are written with dumps_pretty; review queue lines and
release objects use dumps_compact. Both are byte-stable for the same input, and both refuse NaN
and Infinity, which are not JSON (JSON.parse in the browser rejects them). loads and read_json
refuse them too, and report nesting too deep to parse as a ValueError. Files are written
atomically: a failed write leaves the previous file in place.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, cast

if TYPE_CHECKING:
    from pydantic import BaseModel, JsonValue


def dumps_pretty(obj: object) -> str:
    """Sorted keys, two-space indent, non-ASCII kept as is, one trailing newline."""
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def dumps_compact(obj: object) -> bytes:
    """Sorted keys, no whitespace, UTF-8 bytes, no trailing newline."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def record_json(r: BaseModel) -> dict[str, JsonValue]:
    """The full JSON dump of a model: every key present, nothing excluded."""
    return cast("dict[str, JsonValue]", r.model_dump(mode="json"))


def _no_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} is not valid JSON")


def loads(text: str | bytes) -> JsonValue:
    """Parse JSON text. NaN, Infinity and -Infinity, and nesting too deep for the parser, raise
    ValueError."""
    try:
        return cast("JsonValue", json.loads(text, parse_constant=_no_constant))
    except RecursionError:
        raise ValueError("JSON is nested too deeply to parse") from None


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with tmp.open("xb") as f:
            f.write(data)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 text as given (LF stays LF), creating parent directories. The text is encoded
    before the file is touched, and the file is replaced atomically."""
    _write_atomic(path, text.encode("utf-8"))


def write_bytes(path: Path, data: bytes) -> None:
    """Write bytes atomically, creating parent directories."""
    _write_atomic(path, data)


def read_json(path: Path) -> JsonValue:
    """Parse a UTF-8 JSON file (see loads)."""
    return loads(path.read_bytes().decode("utf-8"))
