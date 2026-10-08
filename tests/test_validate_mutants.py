"""One case per validate.py branch that review round 1 found untested (SV-5): each test kills a
mutant that deleted or weakened the branch while the whole suite still passed."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from atlas.geo.counties import CountyIndex
from atlas.jsonio import dumps_pretty, record_json
from atlas.schema.record import FacilityRecord
from atlas.store import RecordStore
from atlas.validate import ValidationReport, validate_dataset, validate_record

MakeRecord = Callable[..., FacilityRecord]
TODAY = date(2026, 10, 12)
ORG_A = "gwo-01m4785400aaaaaaaaaaaaaaaa"
ORG_B = "gwo-01m4785400bbbbbbbbbbbbbbbb"


def pairs(
    record: FacilityRecord, counties: CountyIndex | None = None
) -> list[tuple[str, str | None]]:
    return [(i.rule, i.pointer) for i in validate_record(record, counties=counties, today=TODAY)]


def supported(make_record: MakeRecord, *extra: str) -> list[Any]:
    sources = record_json(make_record())["sources"]
    assert isinstance(sources, list) and isinstance(sources[0], dict)
    supports = sources[0]["supports"]
    assert isinstance(supports, list)
    supports.extend(extra)
    return sources


def run_dataset(records: Path, orgs: Path, schema: Path, counties: CountyIndex) -> ValidationReport:
    return validate_dataset(records, orgs, schema_path=schema, counties=counties, today=TODAY)


@pytest.fixture
def schema(repo_root: Path) -> Path:
    return repo_root / "schema" / "facility.v1.json"


def org(oid: str, name: str) -> dict[str, Any]:
    return {
        "aliases": [],
        "id": oid,
        "kind": "colocation",
        "name": name,
        "parent_id": None,
        "sec_cik": None,
        "wikidata_qid": None,
    }


# ------------------------------------------------------------------------ per-record rules


def test_v1_every_event_planned(make_record: MakeRecord) -> None:
    history = [
        {
            "seq": 1,
            "status": "announced",
            "event": "announced",
            "as_of": {"value": "2027", "precision": "year"},
            "planned": True,
            "source_ids": ["s1"],
        }
    ]
    r = make_record(rollup=False, status_history=history, dates={})
    assert pairs(r) == [("rollup", "/status_history")]


def test_v3_offsets_need_the_quote(make_record: MakeRecord) -> None:
    sources = supported(make_record)
    sources[0].update(quote_start=0, quote_end=5, text_sha256="a" * 64)
    assert pairs(make_record(sources=sources)) == [("quote", "/sources/0/quote")]


@pytest.mark.parametrize(("start", "end"), [(0, None), (None, 5)])
def test_v4_offsets_are_set_together(
    make_record: MakeRecord, start: int | None, end: int | None
) -> None:
    sources = supported(make_record)
    sources[0].update(quote="abcde", quote_start=start, quote_end=end, text_sha256="a" * 64)
    issues = validate_record(make_record(sources=sources), counties=None, today=TODAY)
    assert [(i.rule, i.message) for i in issues] == [
        ("quote", "quote_start and quote_end must be set together")
    ]


@pytest.mark.parametrize(
    ("changes", "pointer"),
    [
        ({"money": {"investment_usd": 0}}, "/money/investment_usd"),
        ({"money": {"investment_usd": -1e6}}, "/money/investment_usd"),
        ({"cooling": {"water_use_mgd": -1}}, "/cooling/water_use_mgd"),
        ({"capacity": {"backup_generation_mw": 0}}, "/capacity/backup_generation_mw"),
        ({"capacity": {"backup_generation_mw": 10_001}}, "/capacity/backup_generation_mw"),
    ],
)
def test_v6_v7_v18_range_bounds(
    make_record: MakeRecord, changes: dict[str, Any], pointer: str
) -> None:
    sources = supported(make_record, "/money", "/cooling")
    assert pairs(make_record(sources=sources, **changes)) == [("range", pointer)]


def test_v6_v7_in_range_values_pass(make_record: MakeRecord) -> None:
    sources = supported(make_record, "/money", "/cooling")
    r = make_record(
        sources=sources,
        money={"investment_usd": 1},
        cooling={"water_use_mgd": 0},
        capacity={"backup_generation_mw": 10_000},
    )
    assert pairs(r) == []


def test_v8_phase_expected_in_service_range(make_record: MakeRecord) -> None:
    phases = [
        {
            "phase_id": "p1",
            "name": "Phase 1",
            "expected_in_service": {"value": "2050", "precision": "year"},
            "source_ids": ["s1"],
        }
    ]
    assert pairs(make_record(phases=phases)) == [("range", "/phases/0/expected_in_service")]


def test_v9_privacy_checks_dict_keys(make_record: MakeRecord) -> None:
    r = make_record(external_ids={"jane@example.com": ["1"]})
    assert pairs(r) == [("privacy", "/external_ids/jane@example.com")]


def test_v10_duplicate_phase_id(make_record: MakeRecord) -> None:
    phases = [{"phase_id": "p1", "name": n, "source_ids": ["s1"]} for n in ("A", "B")]
    issues = validate_record(make_record(phases=phases), counties=None, today=TODAY)
    assert [(i.rule, i.pointer, i.message) for i in issues] == [
        ("phase", "/phases", "duplicate phase_id p1")
    ]


def test_v11_building_phase_id_must_exist(make_record: MakeRecord) -> None:
    sources = supported(make_record, "/buildings")
    r = make_record(sources=sources, buildings=[{"ref": "osm:way/1", "phase_id": "p9"}])
    assert pairs(r) == [("phase", "/buildings/0/phase_id")]


def test_v15_empty_supports_entry(make_record: MakeRecord) -> None:
    sources = supported(make_record, "")
    issues = validate_record(make_record(sources=sources), counties=None, today=TODAY)
    assert [(i.rule, i.pointer, i.message) for i in issues] == [
        ("sources", "/sources/0/supports/4", "supports entry '' is not a path in the record")
    ]


def test_v20_unknown_state(make_record: MakeRecord) -> None:
    loc = record_json(make_record())["location"]
    assert isinstance(loc, dict)
    loc.update(state_abbr="XX", county_fips=None, precision="locality")
    issues = validate_record(make_record(location=loc), counties=None, today=TODAY)
    assert ("geo", "unknown state_abbr XX") in [(i.rule, i.message) for i in issues]


# --------------------------------------------------------------------------- dataset rules


def test_v2_merged_into_target_must_exist(
    tmp_path: Path,
    make_record: MakeRecord,
    fixture_orgs_path: Path,
    schema: Path,
    counties: CountyIndex,
) -> None:
    records = tmp_path / "records"
    missing = "gwa-01m4785400zzzzzzzzzzzzzzzz"
    RecordStore(records).write(make_record(merged_into=missing))
    report = run_dataset(records, fixture_orgs_path, schema, counties)
    assert [(i.rule, i.pointer, i.message) for i in report.issues] == [
        ("merged_into", "/merged_into", f"target {missing} does not exist")
    ]
    assert report.merged == 1


def test_v14_duplicate_record_id(
    tmp_path: Path,
    fixture_records_dir: Path,
    fixture_orgs_path: Path,
    schema: Path,
    counties: CountyIndex,
) -> None:
    records = tmp_path / "records"
    records.mkdir()
    rid = "gwa-01m4785401mdczvghe70mfz68q"
    shutil.copy(fixture_records_dir / f"{rid}.json", records / f"{rid}.json")
    shutil.copy(fixture_records_dir / f"{rid}.json", records / "z-copy.json")
    report = run_dataset(records, fixture_orgs_path, schema, counties)
    messages = [(i.rule, i.message) for i in report.issues]
    assert ("file", f"duplicate id (also in {records / f'{rid}.json'})") in messages
    assert report.records == 1


@pytest.mark.parametrize(
    ("orgs", "canonical", "message"),
    [
        ([org(ORG_B, "B"), org(ORG_A, "A")], True, "orgs must be sorted by id"),
        ([org(ORG_A, "A"), org(ORG_A, "A2")], True, f"duplicate org id {ORG_A}"),
        ([org(ORG_A, "A")], False, "orgs file is not in canonical form"),
    ],
)
def test_v12_v13_v17_orgs_file(
    tmp_path: Path,
    fixture_records_dir: Path,
    schema: Path,
    counties: CountyIndex,
    orgs: list[dict[str, Any]],
    canonical: bool,
    message: str,
) -> None:
    records = tmp_path / "records"
    records.mkdir()
    path = tmp_path / "orgs.json"
    text = dumps_pretty(orgs) if canonical else json.dumps(orgs, indent=4)
    path.write_text(text, encoding="utf-8")
    report = run_dataset(records, path, schema, counties)
    assert [i.message for i in report.issues] == [message]


def test_v16_schema_that_is_not_a_json_schema(
    tmp_path: Path, fixture_records_dir: Path, fixture_orgs_path: Path, counties: CountyIndex
) -> None:
    bad = tmp_path / "facility.v1.json"
    bad.write_text('{"type": 5}\n', encoding="utf-8")
    report = run_dataset(fixture_records_dir, fixture_orgs_path, bad, counties)
    messages = [(i.rule, i.message) for i in report.issues]
    assert ("jsonschema", f"{bad} is out of date; run uv run atlas schema export") in messages
    assert any(r == "jsonschema" and m.startswith("not a valid JSON Schema") for r, m in messages)
    assert {r for r, _ in messages} == {"jsonschema"}


def test_v25_missing_schema_file(
    tmp_path: Path, fixture_records_dir: Path, fixture_orgs_path: Path, counties: CountyIndex
) -> None:
    report = run_dataset(fixture_records_dir, fixture_orgs_path, tmp_path / "nope.json", counties)
    assert [i.rule for i in report.issues] == ["jsonschema"]
    assert report.issues[0].message.startswith("cannot read the JSON Schema")
    assert report.records == 9  # the records are still checked, without the JSON Schema


def test_v26_missing_records_dir(
    tmp_path: Path, fixture_orgs_path: Path, schema: Path, counties: CountyIndex
) -> None:
    report = run_dataset(tmp_path / "nope", fixture_orgs_path, schema, counties)
    assert [(i.rule, i.message) for i in report.issues] == [("file", "records directory not found")]
    assert not report.ok
