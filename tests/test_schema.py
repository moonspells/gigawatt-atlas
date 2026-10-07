from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from atlas.cli import main
from atlas.jsonio import dumps_pretty, record_json
from atlas.schema.export import SCHEMA_ID, facility_json_schema, render
from atlas.schema.org import Org
from atlas.schema.record import (
    PLACEHOLDER_ID,
    SCHEMA_VERSION,
    Capacity,
    FacilityRecord,
    FuzzyDate,
    Location,
    Source,
)
from atlas.store import RecordStore

MakeRecord = Callable[..., FacilityRecord]


@pytest.mark.parametrize(
    ("value", "precision"),
    [("2026", "year"), ("2026-Q2", "quarter"), ("2026-02", "month"), ("2024-02-29", "day")],
)
def test_fuzzy_date_valid(value: str, precision: Any) -> None:
    assert FuzzyDate(value=value, precision=precision).value == value


@pytest.mark.parametrize(
    ("value", "precision"),
    [
        ("2026", "month"),
        ("2026-Q2", "year"),
        ("2026-02", "day"),
        ("2026-02-03", "month"),
        ("2026-02-30", "day"),
        ("2025-02-29", "day"),
        ("2026-13", "month"),
        ("2026-Q5", "quarter"),
        ("26-01", "month"),
        ("2026-1", "month"),
    ],
)
def test_fuzzy_date_invalid(value: str, precision: Any) -> None:
    with pytest.raises(ValidationError):
        FuzzyDate(value=value, precision=precision)


def test_extra_keys_are_forbidden(make_record: MakeRecord) -> None:
    data = record_json(make_record())
    data["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        FacilityRecord.model_validate(data)
    with pytest.raises(ValidationError):
        Capacity.model_validate({"it_mw": 1, "mw": 2})


def test_constraints(make_record: MakeRecord) -> None:
    good = record_json(make_record())

    def broken(**changes: Any) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(json.dumps(good))
        for path, value in changes.items():
            node: Any = data
            keys = path.split("__")
            for key in keys[:-1]:
                node = node[int(key)] if key.isdigit() else node[key]
            node[keys[-1]] = value
        return data

    cases = [
        broken(id="gwa-ILLEGAL0000000000000000000"),
        broken(id="gwa-01m4785400"),
        broken(parent_id="gwa-01m47854008j5vt37xkj5ag72d"),
        broken(status="rumored"),
        broken(status_history=[]),
        broken(sources=[]),
        broken(location__lat=10.0),
        broken(location__lon=-50.0),
        broken(location__county_fips="5110"),
        broken(location__state_abbr="Va"),
        broken(sources__0__id="src1"),
        broken(sources__0__url="not a url"),
        broken(sources__0__retrieved_at="2026-10-06T00:00:00"),
        broken(sources__0__quote="x" * 301),
        broken(status_history__0__source_ids=[]),
        broken(status_history__0__note="x" * 201),
        broken(created_at="2026-10-06"),
    ]
    for data in cases:
        with pytest.raises(ValidationError):
            FacilityRecord.model_validate(data)


def test_field_meta_confidence_bounds() -> None:
    from atlas.schema.record import FieldMeta

    with pytest.raises(ValidationError):
        FieldMeta(confidence=1.5, method="stated")
    assert FieldMeta(
        confidence=0.5, method="derived", conflicts=[{"value": 1, "source_ids": ["s1"]}]
    )


def test_round_trip(fixture_records_dir: Path) -> None:
    records = RecordStore(fixture_records_dir).load()
    assert len(records) == 9
    for r in records.values():
        assert FacilityRecord.model_validate(record_json(r)) == r
        assert FacilityRecord.model_validate_json(dumps_pretty(record_json(r))) == r


def test_full_dump_has_every_key(make_record: MakeRecord) -> None:
    data = record_json(make_record())
    assert set(data) == set(FacilityRecord.model_fields)
    location = data["location"]
    assert isinstance(location, dict)
    assert set(location) == set(Location.model_fields)
    assert data["schema_version"] == SCHEMA_VERSION


def test_http_urls_are_normalized() -> None:
    source = Source.model_validate(
        {
            "id": "s1",
            "url": "https://aigridwatch.com",
            "publisher": "AI GridWatch",
            "source_type": "open_dataset",
            "retrieved_at": "2026-10-06T00:00:00Z",
        }
    )
    assert record_json(source)["url"] == "https://aigridwatch.com/"
    assert record_json(source)["retrieved_at"] == "2026-10-06T00:00:00Z"


def test_placeholder_id_is_a_valid_id(make_record: MakeRecord) -> None:
    r = make_record(id=PLACEHOLDER_ID)
    assert r.id == PLACEHOLDER_ID


def test_org_model() -> None:
    org = Org(id="gwo-01m4785400yqzp3nxn6f6q6wfh", name="Example", kind="colocation")
    assert record_json(org)["aliases"] == []
    with pytest.raises(ValidationError):
        Org(id="gwa-01m4785400yqzp3nxn6f6q6wfh", name="Example", kind="colocation")
    with pytest.raises(ValidationError):
        Org(id="gwo-01m4785400yqzp3nxn6f6q6wfh", name="X", kind="other", wikidata_qid="42")
    with pytest.raises(ValidationError):
        Org(id="gwo-01m4785400yqzp3nxn6f6q6wfh", name="X", kind="other", sec_cik="12345")


def test_export_is_deterministic_and_draft_2020_12() -> None:
    schema = facility_json_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == SCHEMA_ID
    assert schema["title"] == "Gigawatt Atlas facility record"
    assert "1.0.0" in str(schema["description"]) and "ODbL 1.0" in str(schema["description"])
    assert render() == render()
    Draft202012Validator.check_schema(schema)
    required = schema["required"]
    assert isinstance(required, list) and set(required) == set(FacilityRecord.model_fields)


def test_committed_schema_is_current(repo_root: Path) -> None:
    assert (repo_root / "schema" / "facility.v1.json").read_text(encoding="utf-8") == render()


def test_fixture_records_pass_the_exported_schema(fixture_records_dir: Path) -> None:
    validator = Draft202012Validator(json.loads(render()))
    for path in sorted(fixture_records_dir.glob("*.json")):
        errors = list(validator.iter_errors(json.loads(path.read_text(encoding="utf-8"))))
        assert errors == [], path


def test_schema_export_check_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "schema" / "facility.v1.json"
    assert main(["schema", "export", "--out", str(out), "--check"]) == 1
    assert main(["schema", "export", "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == render()
    assert main(["schema", "export", "--out", str(out), "--check"]) == 0
    out.write_text(render().replace("Gigawatt Atlas facility record", "edited"), encoding="utf-8")
    assert main(["schema", "export", "--out", str(out), "--check"]) == 1
    err = capsys.readouterr().err
    assert "is out of date; run uv run atlas schema export" in err
