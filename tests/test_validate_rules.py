"""Rules added after review round 1: date keys and their support, events, status_reason,
county_name, quote offsets, the placeholder id and the records-directory listing."""

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
from atlas.schema.record import PLACEHOLDER_ID, FacilityRecord
from atlas.store import RecordStore, StoreError, list_records_dir
from atlas.validate import validate_dataset, validate_record

MakeRecord = Callable[..., FacilityRecord]
TODAY = date(2026, 10, 12)
FIXTURE_2 = "gwa-01m4785401mdczvghe70mfz68q"


def issues_of(
    record: FacilityRecord, counties: CountyIndex | None = None
) -> list[tuple[str, str | None]]:
    return [(i.rule, i.pointer) for i in validate_record(record, counties=counties, today=TODAY)]


def event(seq: int, status: str, ev: str, value: str, **extra: Any) -> dict[str, Any]:
    precision = {4: "year", 7: "month", 10: "day"}[len(value)]
    return {
        "seq": seq,
        "status": status,
        "event": ev,
        "as_of": {"value": value, "precision": precision},
        "source_ids": ["s1"],
        **extra,
    }


def with_supports(make_record: MakeRecord, *extra: str) -> list[Any]:
    sources = record_json(make_record())["sources"]
    assert isinstance(sources, list) and isinstance(sources[0], dict)
    supports = sources[0]["supports"]
    assert isinstance(supports, list)
    supports.extend(extra)
    return sources


# ------------------------------------------------------------------------------- SV-6


def test_expected_in_service_needs_a_source(make_record: MakeRecord) -> None:
    dates = {"expected_in_service": {"value": "2027", "precision": "year"}}
    r = make_record(dates=dates)
    assert issues_of(r) == [
        ("support", "/dates/expected_in_service/value"),
        ("support", "/dates/expected_in_service/precision"),
    ]
    r = make_record(dates=dates, sources=with_supports(make_record, "/dates/expected_in_service"))
    assert issues_of(r) == []


def test_derived_dates_need_no_source(make_record: MakeRecord) -> None:
    r = make_record()
    assert set(r.dates) == {"first_reported", "announced"}
    assert issues_of(r) == []


@pytest.mark.parametrize("key", ["operating_sinse", "expected_inservice", "opened", ""])
def test_unknown_date_keys_are_rejected(make_record: MakeRecord, key: str) -> None:
    dates = {key: {"value": "2027", "precision": "year"}}
    sources = with_supports(make_record, "/dates")
    issues = issues_of(make_record(dates=dates, sources=sources))
    assert issues == [("dates", f"/dates/{key}")]


# ------------------------------------------------------------------------------- SV-7


def test_a_future_event_must_be_planned(make_record: MakeRecord) -> None:
    history = [
        event(1, "announced", "announced", "2026-03"),
        event(2, "operating", "energized", "2031"),
    ]
    r = make_record(status_history=history)
    assert r.status == "operating"  # what the unplanned future event would publish
    assert issues_of(r) == [("rollup", "/status_history/1/planned")]
    history[1]["planned"] = True
    r = make_record(status_history=history)
    assert (r.status, issues_of(r)) == ("announced", [])


def test_an_event_in_the_current_period_is_not_future(make_record: MakeRecord) -> None:
    for value in ("2026", "2026-10", "2026-10-12"):
        history = [event(1, "announced", "announced", value)]
        assert issues_of(make_record(status_history=history)) == [], value
    history = [event(1, "announced", "announced", "2026-10-13")]
    assert issues_of(make_record(status_history=history)) == [
        ("rollup", "/status_history/0/planned")
    ]


def test_duplicate_seq_is_rejected(make_record: MakeRecord) -> None:
    """Two events with the same date and seq: list order decided the status."""
    history = [
        event(1, "announced", "announced", "2026-03"),
        event(1, "cancelled", "cancelled", "2026-03"),
    ]
    forward = make_record(status_history=history)
    backward = make_record(status_history=history[::-1])
    assert forward.status != backward.status
    for r in (forward, backward):
        assert ("rollup", "/status_history") in issues_of(r)


# ------------------------------------------------------------------------------ SV-10


@pytest.mark.parametrize(
    ("history", "reason", "ok"),
    [
        ([event(1, "announced", "announced", "2026-03")], "local_denial", False),
        ([event(1, "operating", "energized", "2025")], "moratorium", False),
        ([event(1, "denied", "denied", "2026-03")], "local_denial", True),
        ([event(1, "paused", "paused", "2026-03")], "moratorium", True),
        ([event(1, "cancelled", "withdrawn", "2026-03")], "developer_withdrawal", True),
        (
            [
                event(1, "paused", "paused", "2025"),
                event(2, "announced", "resumed", "2026-03"),
            ],
            "regulatory_pause",
            False,
        ),
    ],
)
def test_status_reason_needs_a_stopped_status(
    make_record: MakeRecord, history: list[dict[str, Any]], reason: str, ok: bool
) -> None:
    r = make_record(status_history=history, status_reason=reason)
    assert issues_of(r) == ([] if ok else [("rollup", "/status_reason")])


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("Loudoun", True),
        ("Loudoun County", True),
        ("loudoun county", True),
        ("Fairfax", False),
        ("Fairfax County", False),
        ("Loudoun Parish", False),
    ],
)
def test_county_name_must_be_the_fips_county(
    make_record: MakeRecord, counties: CountyIndex, name: str, ok: bool
) -> None:
    loc = record_json(make_record())["location"]
    assert isinstance(loc, dict)
    loc["county_name"] = name
    r = make_record(location=loc)
    assert issues_of(r, counties) == ([] if ok else [("geo", "/location/county_name")])
    assert issues_of(r) == []  # without counties the check is skipped


@pytest.mark.parametrize("precision", ["county", "state", "unknown"])
def test_a_centroid_or_no_point_names_no_city_or_municipality(
    make_record: MakeRecord, precision: str
) -> None:
    # AWS Berwick: Luzerne County's centroid lies in Rice township, not in the Salem Township
    # the record named, so a county-precision record cannot name a city or municipality.
    loc: dict[str, Any] = {
        "lat": 41.16574,
        "lon": -75.954718,
        "precision": precision,
        "city": "Berwick",
        "municipality": "Salem Township",
        "county_fips": "42079",
        "state_abbr": "PA",
    }
    if precision != "county":
        loc.update(lat=None, lon=None, county_fips=None)
    r = make_record(location=loc)
    assert issues_of(r) == [("geo", "/location/city"), ("geo", "/location/municipality")]
    loc.update(city=None, municipality=None)
    assert issues_of(make_record(location=loc)) == []


def test_county_name_forms_of_an_independent_city(counties: CountyIndex) -> None:
    for name in ("Manassas", "Manassas city", "City of Manassas", "manassas city"):
        assert counties.name_matches("51683", name), name
    assert not counties.name_matches("51683", "Manassas Park")
    assert not counties.name_matches("51153", "Manassas")
    assert counties.name_matches("51153", "Prince William County")
    assert not counties.name_matches("99999", "Nowhere")


def test_placeholder_id_cannot_be_stored(
    tmp_path: Path,
    make_record: MakeRecord,
    fixture_orgs_path: Path,
    repo_root: Path,
    counties: CountyIndex,
) -> None:
    r = make_record().model_copy(update={"id": PLACEHOLDER_ID})
    with pytest.raises(ValueError, match="placeholder"):
        RecordStore(tmp_path / "records").write(r)
    records = tmp_path / "records"
    records.mkdir(exist_ok=True)
    (records / f"{PLACEHOLDER_ID}.json").write_text(dumps_pretty(record_json(r)), encoding="utf-8")
    report = validate_dataset(
        records,
        fixture_orgs_path,
        schema_path=repo_root / "schema" / "facility.v1.json",
        counties=counties,
        today=TODAY,
    )
    assert [(i.rule, i.message) for i in report.issues] == [
        ("file", "id is the importer placeholder; apply_import assigns ids")
    ]


# ------------------------------------------------------------------------------ SV-14


def quoted(make_record: MakeRecord, **fields: Any) -> FacilityRecord:
    sources = with_supports(make_record)
    sources[0].update(fields)
    return make_record(sources=sources)


def test_exact_quote_offsets_span_the_quote(make_record: MakeRecord) -> None:
    sha = "a" * 64
    r = quoted(
        make_record, quote="abc", quote_start=0, quote_end=500, text_sha256=sha, quote_match="exact"
    )
    assert issues_of(r) == [("quote", "/sources/0/quote_end")]
    r = quoted(
        make_record, quote="abc", quote_start=7, quote_end=10, text_sha256=sha, quote_match="exact"
    )
    assert issues_of(r) == []
    # A fuzzy or human-entered quote may differ in length from the span it was matched to.
    for match in ("fuzzy", "human"):
        r = quoted(
            make_record, quote="abc", quote_start=0, quote_end=5, text_sha256=sha, quote_match=match
        )
        assert issues_of(r) == [], match


def test_quote_match_needs_a_quote(make_record: MakeRecord) -> None:
    assert issues_of(quoted(make_record, quote_match="exact")) == [
        ("quote", "/sources/0/quote_match")
    ]
    assert issues_of(quoted(make_record, quote="abc", quote_match="human")) == []


# ------------------------------------------------------------------------------- SV-8


@pytest.mark.parametrize("name", [".x.json", f"._{FIXTURE_2}.json", ".hidden", "x.json.tmp"])
def test_validate_and_the_store_reject_the_same_stray_files(
    tmp_path: Path,
    fixture_records_dir: Path,
    fixture_orgs_path: Path,
    repo_root: Path,
    counties: CountyIndex,
    name: str,
) -> None:
    """A dot-file passed validate silently and then broke RecordStore.load (every import)."""
    records = tmp_path / "records"
    records.mkdir()
    shutil.copy(fixture_records_dir / f"{FIXTURE_2}.json", records / f"{FIXTURE_2}.json")
    shutil.copy(fixture_records_dir / f"{FIXTURE_2}.json", records / name)
    (records / ".gitkeep").write_text("", encoding="utf-8")
    report = validate_dataset(
        records,
        fixture_orgs_path,
        schema_path=repo_root / "schema" / "facility.v1.json",
        counties=counties,
        today=TODAY,
    )
    assert [(i.rule, Path(i.file or "").name) for i in report.issues] == [("file", name)]
    with pytest.raises(StoreError) as info:
        RecordStore(records).load()
    assert [Path(p.split(":")[0]).name for p in info.value.problems] == [name]
    assert [p.name for p in RecordStore(records).iter_files()] == [f"{FIXTURE_2}.json"]


def test_list_records_dir(tmp_path: Path) -> None:
    assert list_records_dir(tmp_path / "missing") == ([], [])
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")
    (tmp_path / "b.json").write_text("{}", encoding="utf-8")
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("", encoding="utf-8")
    (tmp_path / "sub.json").mkdir()
    files, stray = list_records_dir(tmp_path)
    assert [p.name for p in files] == ["a.json", "b.json"]
    assert [p.name for p in stray] == ["notes.txt", "sub.json"]


def test_the_records_fixture_round_trips_through_the_store(fixture_records_dir: Path) -> None:
    loaded = RecordStore(fixture_records_dir).load()
    assert len(loaded) == len(list(fixture_records_dir.glob("gwa-*.json")))
    doc = json.loads((fixture_records_dir / f"{FIXTURE_2}.json").read_text(encoding="utf-8"))
    assert "/dates/expected_in_service" in doc["sources"][0]["supports"]
