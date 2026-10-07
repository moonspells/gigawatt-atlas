from __future__ import annotations

import json
from pathlib import Path

from atlas.jsonio import dumps_compact, dumps_pretty, read_json, record_json, write_text
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
