from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas.jsonio import (
    dumps_compact,
    dumps_pretty,
    loads,
    read_json,
    record_json,
    write_bytes,
    write_text,
)
from atlas.schema.record import FuzzyDate


def test_dumps_pretty_is_sorted_indented_and_newline_terminated() -> None:
    text = dumps_pretty({"b": 1, "a": {"d": [1, 2], "c": "é"}})
    assert (
        text
        == '{\n  "a": {\n    "c": "é",\n    "d": [\n      1,\n      2\n    ]\n  },\n  "b": 1\n}\n'
    )


def test_dumps_compact_is_sorted_utf8_without_spaces() -> None:
    assert dumps_compact({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode()


def test_record_json_is_the_full_dump() -> None:
    assert record_json(FuzzyDate(value="2026", precision="year")) == {
        "value": "2026",
        "precision": "year",
    }


def test_write_text_creates_parents_and_uses_lf(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b.json"
    write_text(path, "x\ny\n")
    assert path.read_bytes() == b"x\ny\n"
    path.write_text(json.dumps({"k": [1]}), encoding="utf-8")
    assert read_json(path) == {"k": [1]}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_dumps_refuse_nan_and_infinity(value: float) -> None:
    """SV-3: NaN is not JSON, and JSON.parse in the browser rejects it."""
    with pytest.raises(ValueError, match="JSON compliant"):
        dumps_pretty({"building_sqft": value})
    with pytest.raises(ValueError, match="JSON compliant"):
        dumps_compact({"building_sqft": value})


@pytest.mark.parametrize("text", ['{"a": NaN}', '{"a": Infinity}', "[-Infinity]"])
def test_loads_and_read_json_refuse_nan_and_infinity(text: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="is not valid JSON"):
        loads(text)
    path = tmp_path / "x.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="is not valid JSON"):
        read_json(path)


def test_loads_reports_deep_nesting_as_a_value_error() -> None:
    """SV-13: RecursionError is not a ValueError, so it escaped every caller's except."""
    with pytest.raises(ValueError, match="nested too deeply"):
        loads("[" * 200_000 + "]" * 200_000)
    assert loads("[" * 100 + "]" * 100) == loads("[" * 100 + "]" * 100)


def test_write_text_keeps_the_old_file_when_encoding_fails(tmp_path: Path) -> None:
    """SV-4: the target was truncated before the UTF-8 encode raised."""
    path = tmp_path / "r.json"
    write_text(path, "old\n")
    lone = json.loads('"\\ud83d"')
    with pytest.raises(UnicodeEncodeError):
        write_text(path, f"new {lone}\n")
    assert path.read_bytes() == b"old\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["r.json"]


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "d" / "b.bin"
    write_bytes(path, b"one")
    write_bytes(path, b"two")
    assert path.read_bytes() == b"two"
    assert [p.name for p in path.parent.iterdir()] == ["b.bin"]
    write_text(path, "a\r\nb\n")
    assert path.read_bytes() == b"a\r\nb\n"
