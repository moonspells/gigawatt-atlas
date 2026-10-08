"""Claim pointers and the support rule (07 §3.3).

Every non-null published value must be backed by a source. claim_pointers lists the RFC 6901
pointers of those values; is_supported says whether one is backed.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from atlas.schema.record import FacilityRecord
from atlas.schema.rollup import DERIVED_DATE_KEYS

# Bookkeeping, derived and provenance fields: they need no supporting source. Of the dates, only
# the keys derived from status_history are exempt; expected_in_service is a sourced claim.
EXEMPT_PREFIXES = (
    "/schema_version",
    "/id",
    "/record_type",
    "/parent_id",
    "/merged_into",
    "/scope",
    "/status",
    "/status_reason",
    "/evidence_level",
    *(f"/dates/{key}" for key in DERIVED_DATE_KEYS),
    "/review",
    "/corrections",
    "/created_at",
    "/updated_at",
    "/last_verified_at",
    "/field_meta",
    "/sources",
    "/external_ids",
    "/location/precision",
    "/location/geocode_method",
    "/location/geometry_ref",
)

# Lists whose items carry their own source_ids (rule b).
_SOURCED_LISTS = ("status_history", "aliases", "phases", "incentives")


def escape(token: str) -> str:
    """Escape one RFC 6901 reference token."""
    return token.replace("~", "~0").replace("/", "~1")


def unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def split(pointer: str) -> list[str]:
    """The unescaped tokens of a pointer ("" is the whole document)."""
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError(f"not a JSON Pointer: {pointer!r}")
    return [unescape(t) for t in pointer[1:].split("/")]


def is_under(pointer: str, prefix: str) -> bool:
    """True when pointer equals prefix or lies inside it, on token boundaries."""
    return pointer == prefix or pointer.startswith(prefix + "/")


def resolve(doc: object, pointer: str) -> tuple[bool, object]:
    """Resolve a pointer against parsed JSON: (found, value)."""
    node = doc
    for token in split(pointer):
        if isinstance(node, dict):
            if token not in node:
                return False, None
            node = node[token]
        elif isinstance(node, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                return False, None
            index = int(token)
            if index >= len(node):
                return False, None
            node = node[index]
        else:
            return False, None
    return True, node


def _exempt(pointer: str) -> bool:
    return any(is_under(pointer, p) for p in EXEMPT_PREFIXES)


def _walk(value: Any, pointer: str, out: list[str]) -> None:
    if _exempt(pointer) or value is None:
        return
    if isinstance(value, BaseModel):
        for name, info in type(value).model_fields.items():
            child = getattr(value, name)
            if child is None:
                continue
            if info.default is not PydanticUndefined and child == info.default:
                continue
            _walk(child, f"{pointer}/{escape(name)}", out)
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _walk(item, f"{pointer}/{i}", out)
        return
    if isinstance(value, dict):
        for key in sorted(value):
            _walk(value[key], f"{pointer}/{escape(str(key))}", out)
        return
    out.append(pointer)  # a leaf: str, number, bool, URL, date or datetime


def claim_pointers(record: FacilityRecord) -> list[str]:
    """Pointers of every leaf that is non-null, not an empty list, not its field default and
    not under an EXEMPT_PREFIXES entry. List items are addressed by index."""
    out: list[str] = []
    _walk(record, "", out)
    return out


def _own_source_ids(record: FacilityRecord, tokens: list[str]) -> bool:
    """Rule b: the pointer lies inside an item that has its own non-empty source_ids."""
    if len(tokens) >= 2 and tokens[0] in _SOURCED_LISTS and tokens[1].isdigit():
        items: list[Any] = getattr(record, tokens[0])
        index = int(tokens[1])
        return index < len(items) and bool(items[index].source_ids)
    if len(tokens) >= 3 and tokens[0] == "parties" and tokens[2].isdigit():
        refs = getattr(record.parties, tokens[1], None)
        if not isinstance(refs, list):
            return False
        index = int(tokens[2])
        return index < len(refs) and bool(refs[index].source_ids)
    return False


def is_supported(record: FacilityRecord, p: str) -> bool:
    """07 §3.3: a value is supported when (a) a source's supports entry covers it, (b) it lies in an
    item with its own source_ids, or (c) a field_meta entry covers it with method "derived" or with
    source_ids. Coverage is on token boundaries: "/location" covers "/location/lat", "/loc" does
    not; an empty entry (the whole record) covers nothing."""
    for source in record.sources:
        if any(e and is_under(p, e) for e in source.supports):
            return True
    if _own_source_ids(record, split(p)):
        return True
    return any(
        k and is_under(p, k) and (meta.method == "derived" or bool(meta.source_ids))
        for k, meta in record.field_meta.items()
    )


def unsupported_pointers(record: FacilityRecord) -> list[str]:
    return [p for p in claim_pointers(record) if not is_supported(record, p)]
