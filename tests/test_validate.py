from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from atlas.cli import main
from atlas.geo.counties import CountyIndex
from atlas.jsonio import record_json
from atlas.schema.record import FacilityRecord
from atlas.store import RecordStore
from atlas.validate import RULES, Issue, validate_dataset, validate_record

MakeRecord = Callable[..., FacilityRecord]
TODAY = date(2026, 10, 12)
INVALID = Path(__file__).parent / "fixtures" / "invalid"
# Cases whose defect also trips another rule by nature (a Pydantic failure is also a JSON Schema
# failure; a JSON Schema-only failure means the file is not the canonical dump).
ALSO_ALLOWED = {"schema": {"jsonschema"}, "jsonschema": {"file"}}


def invalid_cases() -> list[str]:
    """Case names: {rule}.json, {rule}-{variant}.json or a {rule} directory."""
    return sorted(p.stem if p.is_file() else p.name for p in INVALID.iterdir())


def case_rule(case: str) -> str:
    return case.split("-", 1)[0]


def stage_case(case: str, dest: Path) -> Path:
    """Copy one invalid case into dest/records, naming single files {id}.json."""
    records = dest / "records"
    records.mkdir(parents=True)
    source = INVALID / case
    if source.is_dir():
        for f in source.glob("*.json"):
            shutil.copy(f, records / f.name)
    else:
        source = INVALID / f"{case}.json"
        record_id = json.loads(source.read_text(encoding="utf-8"))["id"]
        shutil.copy(source, records / f"{record_id}.json")
    return records


def rules_of(issues: list[Issue]) -> set[str]:
    return {i.rule for i in issues}


def test_every_fixture_record_passes(
    fixture_records_dir: Path, fixture_orgs_path: Path, repo_root: Path, counties: CountyIndex
) -> None:
    report = validate_dataset(
        fixture_records_dir,
        fixture_orgs_path,
        schema_path=repo_root / "schema" / "facility.v1.json",
        counties=counties,
        today=TODAY,
    )
    assert report.issues == []
    assert report.ok
    assert (report.records, report.in_scope, report.out_of_scope, report.merged) == (9, 7, 1, 1)


def test_every_rule_has_an_invalid_case() -> None:
    assert {case_rule(c) for c in invalid_cases()} == set(RULES)
    assert set(RULES) <= set(invalid_cases())


@pytest.mark.parametrize("case", invalid_cases())
def test_invalid_case_fails_with_its_rule(
    case: str, tmp_path: Path, fixture_orgs_path: Path, repo_root: Path, counties: CountyIndex
) -> None:
    records = stage_case(case, tmp_path)
    report = validate_dataset(
        records,
        fixture_orgs_path,
        schema_path=repo_root / "schema" / "facility.v1.json",
        counties=counties,
        today=TODAY,
    )
    assert not report.ok
    rule = case_rule(case)
    assert rule in rules_of(report.issues)
    assert rules_of(report.issues) <= {rule} | ALSO_ALLOWED.get(case, set())


def test_file_name_must_match_id(
    tmp_path: Path,
    fixture_records_dir: Path,
    fixture_orgs_path: Path,
    repo_root: Path,
    counties: CountyIndex,
) -> None:
    records = tmp_path / "records"
    records.mkdir()
    first = sorted(fixture_records_dir.glob("*.json"))[1]
    shutil.copy(first, records / "renamed.json")
    (records / "notes.txt").write_text("x", encoding="utf-8")
    report = validate_dataset(
        records,
        fixture_orgs_path,
        schema_path=repo_root / "schema" / "facility.v1.json",
        counties=counties,
        today=TODAY,
    )
    messages = [i.message for i in report.issues if i.rule == "file"]
    assert any("file name must be" in m for m in messages)
    assert any("only {id}.json" in m for m in messages)


def test_stale_schema_is_reported(
    tmp_path: Path,
    fixture_records_dir: Path,
    fixture_orgs_path: Path,
    repo_root: Path,
    counties: CountyIndex,
) -> None:
    stale = tmp_path / "facility.v1.json"
    stale.write_text(
        (repo_root / "schema" / "facility.v1.json")
        .read_text(encoding="utf-8")
        .replace("ODbL", "X"),
        encoding="utf-8",
    )
    report = validate_dataset(
        fixture_records_dir, fixture_orgs_path, schema_path=stale, counties=counties, today=TODAY
    )
    assert [i.rule for i in report.issues] == ["jsonschema"]
    assert "out of date" in report.issues[0].message


def test_orgs_file_rules(
    tmp_path: Path,
    fixture_records_dir: Path,
    fixture_orgs_path: Path,
    repo_root: Path,
    counties: CountyIndex,
) -> None:
    orgs = json.loads(fixture_orgs_path.read_text(encoding="utf-8"))
    orgs[0]["parent_id"] = "gwo-01m4785400zzzzzzzzzzzzzzzz"
    bad = tmp_path / "orgs.json"
    bad.write_text(json.dumps(orgs, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report = validate_dataset(
        fixture_records_dir,
        bad,
        schema_path=repo_root / "schema" / "facility.v1.json",
        counties=counties,
        today=TODAY,
    )
    assert [i.message for i in report.issues] == [
        "parent_id gwo-01m4785400zzzzzzzzzzzzzzzz is not in orgs"
    ]


def test_validate_record_without_counties_skips_polygons(make_record: MakeRecord) -> None:
    r = make_record(location={**record_json(make_record())["location"], "lat": 38.85, "lon": -77.3})  # type: ignore[dict-item]
    assert validate_record(r, counties=None, today=TODAY) == []


def location(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "lat": 39.0438,
        "lon": -77.4874,
        "precision": "site",
        "county_fips": "51107",
        "state_abbr": "VA",
    }
    base.update(changes)
    return base


@pytest.mark.parametrize(
    ("loc", "message"),
    [
        (location(lat=None, lon=None), "needs lat and lon"),
        (location(lon=None), "both be set"),
        (location(precision="state", county_fips=None), "must not have coordinates"),
        (location(precision="unknown", county_fips=None), "must not have coordinates"),
        (location(county_fips="24031"), "is not in VA"),
        (location(county_fips=None), "needs county_fips"),
        (location(precision="county", county_fips="51059"), "not inside county 51059"),
        (location(precision="locality", state_abbr="MD", county_fips=None), "not inside MD"),
        (location(precision="locality", county_fips="51999"), "not a 2025 Census county"),
    ],
)
def test_geo_rules(
    make_record: MakeRecord, counties: CountyIndex, loc: dict[str, Any], message: str
) -> None:
    r = make_record(location=loc)
    issues = [i for i in validate_record(r, counties=counties, today=TODAY) if i.rule == "geo"]
    assert any(message in i.message for i in issues), issues


def test_geo_passes_for_locality_and_state(make_record: MakeRecord, counties: CountyIndex) -> None:
    for loc in (
        location(precision="locality", county_fips=None),
        location(precision="state", lat=None, lon=None, county_fips=None),
    ):
        assert validate_record(make_record(location=loc), counties=counties, today=TODAY) == []


@pytest.mark.parametrize(
    ("changes", "pointer"),
    [
        ({"capacity": {"facility_mw": 0}}, "/capacity/facility_mw"),
        ({"capacity": {"utility_request_mw": 10_001}}, "/capacity/utility_request_mw"),
        ({"money": {"investment_usd": 6e11}}, "/money/investment_usd"),
        ({"site": {"acreage": 100_001}}, "/site/acreage"),
        ({"cooling": {"water_use_mgd": 51}}, "/cooling/water_use_mgd"),
        (
            {"dates": {"expected_in_service": {"value": "2042", "precision": "year"}}},
            "/dates/expected_in_service",
        ),
        (
            {
                "phases": [
                    {
                        "phase_id": "p1",
                        "name": "P1",
                        "capacity": {"it_mw": -1},
                        "source_ids": ["s1"],
                    }
                ]
            },
            "/phases/0/capacity/it_mw",
        ),
    ],
)
def test_range_rules(make_record: MakeRecord, changes: dict[str, Any], pointer: str) -> None:
    sources = record_json(make_record())["sources"]
    assert isinstance(sources, list) and isinstance(sources[0], dict)
    supports: list[JsonValue] = ["/canonical_name", "/location", "/capacity", "/money", "/site"]
    supports += ["/cooling", *(f"/dates/{key}" for key in changes.get("dates", {}))]
    sources[0]["supports"] = supports
    r = make_record(sources=sources, **changes)
    issues = validate_record(r, counties=None, today=TODAY)
    assert [(i.rule, i.pointer) for i in issues] == [("range", pointer)]


def test_dates_before_1990_are_out_of_range(make_record: MakeRecord) -> None:
    r = make_record(
        status_history=[
            {
                "seq": 1,
                "status": "operating",
                "event": "energized",
                "as_of": {"value": "1989", "precision": "year"},
                "source_ids": ["s1"],
            }
        ]
    )
    rules = {(i.rule, i.pointer) for i in validate_record(r, counties=None, today=TODAY)}
    assert ("range", "/status_history/0/as_of") in rules
    assert ("range", "/dates/first_reported") in rules


def test_privacy_rule_ignores_urls(make_record: MakeRecord) -> None:
    sources = record_json(make_record())["sources"]
    assert isinstance(sources, list) and isinstance(sources[0], dict)
    sources[0]["url"] = "https://user@example.com/page"
    r = make_record(sources=sources)
    assert validate_record(r, counties=None, today=TODAY) == []
    r = make_record(canonical_name="Call (512) 555-0142 Campus (Ashburn, VA)")
    assert [i.pointer for i in validate_record(r, counties=None, today=TODAY)] == [
        "/canonical_name"
    ]


def test_sources_rules(make_record: MakeRecord) -> None:
    sources = record_json(make_record())["sources"]
    assert isinstance(sources, list) and isinstance(sources[0], dict)
    dup = dict(sources[0])
    sources[0]["supports"] = ["/canonical_name", "/location", "/capacity/it_mw/extra", "/aliases/3"]
    r = make_record(
        sources=[sources[0], dup],
        field_meta={
            "/x": {
                "confidence": 0.5,
                "method": "stated",
                "source_ids": ["s7"],
                "conflicts": [{"value": 1, "source_ids": ["s8"]}],
            }
        },
    )
    messages = sorted(
        i.message for i in validate_record(r, counties=None, today=TODAY) if i.rule == "sources"
    )
    assert messages == [
        "duplicate source id s1",
        "source id s7 does not exist in sources",
        "source id s8 does not exist in sources",
        "supports entry '/aliases/3' is not a path in the record",
        "supports entry '/capacity/it_mw/extra' is not a path in the record",
    ]


def test_quote_rules(make_record: MakeRecord) -> None:
    sources = record_json(make_record())["sources"]
    assert isinstance(sources, list) and isinstance(sources[0], dict)
    sources[0].update(quote="abc", quote_start=5, quote_end=5, text_sha256="ABC")
    r = make_record(sources=sources)
    messages = sorted(i.message for i in validate_record(r, counties=None, today=TODAY))
    assert messages == [
        "quote offsets 5..5 are not 0 <= start < end",
        "text_sha256 is not 64 lowercase hex characters",
    ]


def test_cli_text_and_json(
    fixture_records_dir: Path,
    fixture_orgs_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    repo_root: Path,
) -> None:
    common = [
        "--orgs",
        str(fixture_orgs_path),
        "--today",
        "2026-10-12",
        "--schema",
        str(repo_root / "schema" / "facility.v1.json"),
        "--counties",
        str(repo_root / "reference/census/cb_2025_us_county_5m.zip"),
    ]
    assert main(["validate", "--records", str(fixture_records_dir), *common]) == 0
    out = capsys.readouterr().out
    assert out.strip() == "validate: 9 records (in scope 7, out of scope 1, merged 1), 0 issues"

    records = stage_case("range", tmp_path)
    assert main(["validate", "--records", str(records), *common]) == 1
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].endswith(":/capacity/it_mw [range] it_mw 20000.0 is outside (0, 10000]")
    assert lines[-1] == "validate: 1 records (in scope 1, out of scope 0, merged 0), 1 issues"

    assert main(["validate", "--records", str(records), "--format", "json", *common]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["issues"][0]["rule"] == "range"

    assert main(["validate", "--records", str(records), "--max-issues", "0", *common]) == 1
    assert "1 more issues" in capsys.readouterr().out
    assert main(["validate", "--today", "yesterday"]) == 2


def test_store_round_trip_is_byte_identical(fixture_records_dir: Path, tmp_path: Path) -> None:
    records = RecordStore(fixture_records_dir).load()
    out = RecordStore(tmp_path)
    for r in records.values():
        out.write(r)
    for path in fixture_records_dir.glob("*.json"):
        assert (tmp_path / path.name).read_bytes() == path.read_bytes()


def test_unparsable_file_is_an_issue_and_the_rest_is_still_checked(
    tmp_path: Path,
    fixture_records_dir: Path,
    fixture_orgs_path: Path,
    repo_root: Path,
    counties: CountyIndex,
) -> None:
    """SV-13: one deeply nested file ended the run with a RecursionError traceback."""
    records = tmp_path / "records"
    records.mkdir()
    for path in fixture_records_dir.glob("*.json"):
        shutil.copy(path, records / path.name)
    (records / "gwa-01m478540h0000000000000009.json").write_text(
        "[" * 200_000 + "]" * 200_000, encoding="utf-8"
    )
    report = validate_dataset(
        records,
        fixture_orgs_path,
        schema_path=repo_root / "schema" / "facility.v1.json",
        counties=counties,
        today=TODAY,
    )
    assert [(i.rule, i.message) for i in report.issues] == [
        ("file", "not a UTF-8 JSON file: JSON is nested too deeply to parse")
    ]
    assert report.records == 9


def test_deep_orgs_file_is_an_issue(
    tmp_path: Path, fixture_records_dir: Path, repo_root: Path, counties: CountyIndex
) -> None:
    orgs = tmp_path / "orgs.json"
    orgs.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
    report = validate_dataset(
        fixture_records_dir,
        orgs,
        schema_path=repo_root / "schema" / "facility.v1.json",
        counties=counties,
        today=TODAY,
    )
    assert ("org", "cannot read orgs: JSON is nested too deeply to parse") in [
        (i.rule, i.message) for i in report.issues
    ]
