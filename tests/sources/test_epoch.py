"""The Epoch AI importer (atlas/sources/epoch.py) against the committed fixture ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import zipfile
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from atlas.cli import main
from atlas.geo.counties import CountyIndex
from atlas.net import FetchError, fetch, make_client
from atlas.schema.record import FacilityRecord
from atlas.sources.base import Candidate, ImportContext, apply_import, read_review_queue
from atlas.sources.epoch import (
    IMPORTER,
    OVERRIDES_PATH,
    ZIP_URL,
    Placed,
    Site,
    TimelineRow,
    check_license,
    event_note,
    load_overrides,
    selected_links,
    site_record,
    status_events,
    strip_links,
    tagged_names,
)
from atlas.store import RecordStore
from atlas.text import find_personal_data
from atlas.validate import validate_record

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EPOCH_ZIP = FIXTURES / "epoch" / "data_centers.zip"
CENSUS = FIXTURES / "census"
GEOCODE_CENSUS = FIXTURES / "geocode" / "census"  # the seed check's recorded responses
REPO_OVERRIDES = Path(__file__).resolve().parents[2] / "config" / "overrides" / "epoch.json"
MILAM_SOURCE = (
    "https://www.datacenterdynamics.com/en/news/softbanks-sb-energy-to-build-and-operate-"
    "openais-12gw-stargate-data-center-in-milam-county-texas/"
)
# The citation fields every override needs besides its place (made-up text for the test entries).
CITED = {
    "source_url": MILAM_SOURCE,
    "quote": "The data center site is in Milam County, Texas.",
    "retrieved_at": "2026-10-08T05:00:00Z",
    "note": "x",
}
MakeContext = Callable[..., ImportContext]


def census_handler(calls: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    """Answers the Census Geocoder from tests/fixtures/census and tests/fixtures/geocode/census;
    everything else is 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host != "geocoding.geo.census.gov":
            return httpx.Response(404, request=request)
        digest = hashlib.sha256(request.url.params["address"].encode()).hexdigest()
        path = CENSUS / f"{digest}.json"
        if not path.exists():
            path = GEOCODE_CENSUS / f"{digest}.json"
        if not path.exists():
            return httpx.Response(500, request=request)
        return httpx.Response(
            200,
            content=path.read_bytes(),
            headers={"content-type": "application/json"},
            request=request,
        )

    return handler


@pytest.fixture
def no_overrides(tmp_path: Path) -> Path:
    path = tmp_path / "overrides.json"
    path.write_text("{}\n", encoding="utf-8")
    return path


def run_args(overrides: Path, *, no_geocode: bool = True) -> argparse.Namespace:
    return argparse.Namespace(overrides=overrides, no_geocode=no_geocode)


def by_name(candidates: list[Candidate]) -> dict[str, FacilityRecord]:
    return {c.match_values[0]: c.record for c in candidates}


def rezip(tmp_path: Path, edit: Callable[[str, bytes], bytes | None]) -> Path:
    """A copy of the fixture ZIP with members edited (None drops a member)."""
    out = tmp_path / "edited.zip"
    with zipfile.ZipFile(EPOCH_ZIP) as src, zipfile.ZipFile(out, "w") as dst:
        for name in src.namelist():
            data = edit(name, src.read(name))
            if data is not None:
                dst.writestr(name, data)
    return out


def snapshot(*roots: Path) -> dict[str, bytes]:
    return {
        str(p): p.read_bytes() for root in roots for p in sorted(root.rglob("*")) if p.is_file()
    }


# ---------------------------------------------------------------------------- parsing helpers


def test_tagged_names() -> None:
    text = "Anthropic #confident, Cursor #likely, Foo #speculative, Bar #unlikely, Google, cursor"
    assert tagged_names(text) == ["Anthropic", "Cursor", "Google"]
    assert tagged_names("") == []
    assert tagged_names("Project Rainier #speculative") == []


def test_event_notes_reduce_links_and_stay_short() -> None:
    text = "Matches the [company S-1 filing, page 76](https://www.sec.gov/x.htm) and [tweets](https://x.com/a)."
    assert strip_links(text) == "Matches the company S-1 filing, page 76 and tweets."
    long = "Construction " * 40
    note = event_note(long)
    assert note is not None and len(note) == 200 and note.endswith("…")
    assert event_note("Call 555-123-4567 for updates") is None
    assert event_note("   ") is None


def test_link_sources() -> None:
    md = "- [One](https://a.example/1)\n- [Bad](notaurl)\n- [Again](https://a.example/1)\n- [Two](https://b.example/2)\n"
    assert selected_links(md) == [("One", "https://a.example/1"), ("Two", "https://b.example/2")]


def row(day: str, text: str, operational: float | None) -> TimelineRow:
    return TimelineRow(date.fromisoformat(day), text, operational, None, None, None)


def test_status_events_keep_only_changes_and_plan_future_rows() -> None:
    rows = [
        row("2024-01-10", "Site acquired for the campus", 0),
        row("2024-06-01", "Land clearing begins", 0),
        row("2024-09-01", "Steel framing and roofing underway", 0),
        row("2025-03-01", "Building 1 operational", 1),
        row("2025-09-01", "Building 2 operational", 2),
        row("2027-01-01", "Building 3 projected", 3),
    ]
    events = status_events(rows, date(2026, 10, 12))
    got = [(e["seq"], e["status"], e["event"], e["as_of"]["value"], e["planned"]) for e in events]
    assert got == [
        (1, "announced", "announced", "2024-01-10", False),
        (2, "under_construction", "construction_start", "2024-06-01", False),
        (3, "operating", "energized", "2025-03-01", False),
    ]
    future = status_events(
        [row("2025-01-01", "Land clearing begins", 0), row("2028-01-01", "Phase 1 online", 9)],
        date(2026, 10, 12),
    )
    assert [(e["status"], e["planned"]) for e in future] == [
        ("under_construction", False),
        ("operating", True),
    ]
    assert all(e["source_ids"] == ["s1"] for e in events + future)


def test_a_blank_building_count_falls_back_to_it_power(make_test_context: MakeContext) -> None:
    # Epoch leaves "Buildings operational" empty on some projected rows (OpenAI Stargate Milam,
    # 2028-12-31, "site is fully operational", 857 MW): IT power above 0 then means operating, in
    # the events and in the record type (from_epoch_row's reading).
    rows = [
        row("2025-09-27", "Land clearing begins for the site", 0),
        TimelineRow(
            date(2028, 12, 31),
            "All buildings finished, site is fully operational.",
            None,
            857.0,
            1200.0,
            None,
        ),
    ]
    events = status_events(rows, date(2026, 10, 12))
    assert [(e["status"], e["event"], e["planned"]) for e in events] == [
        ("under_construction", "construction_start", False),
        ("operating", "energized", True),
    ]
    site = Site("Test site", "5420 Tulane Rd, Memphis, TN 38109", "", "", "", "", None)
    placed = Placed(
        {
            "lat": 35.1,
            "lon": -90.0,
            "precision": "locality",
            "city": "Memphis",
            "state_abbr": "TN",
            "geocode_method": "gazetteer",
        },
        "Memphis",
        0.60,
        "locality",
        None,
    )
    ctx = make_test_context()

    def record(buildings: float | None, it_mw: float | None) -> FacilityRecord:
        rows = [TimelineRow(date(2025, 1, 1), "Building 1 online", buildings, it_mw, 12.0, None)]
        return site_record(site, rows, placed, ctx=ctx, retrieved_at="2026-10-12T12:00:00+00:00")

    assert (record(None, 10.0).record_type, record(None, 10.0).status) == ("campus", "operating")
    assert (record(0, 10.0).record_type, record(0, 10.0).status) == (
        "project",
        "under_construction",
    )
    assert (record(None, 0.0).record_type, record(None, None).record_type) == ("project", "project")


def test_water_use_is_kept_only_when_positive(make_test_context: MakeContext) -> None:
    # Epoch writes 0.0 for "not estimated"; every water cell of the fixture ZIP is empty.
    site = Site("Test site", "5420 Tulane Rd, Memphis, TN 38109", "", "", "", "", None)
    placed = Placed(
        {
            "lat": 35.1,
            "lon": -90.0,
            "precision": "locality",
            "city": "Memphis",
            "state_abbr": "TN",
            "geocode_method": "gazetteer",
        },
        "Memphis",
        0.60,
        "locality",
        None,
    )
    ctx = make_test_context()

    def water(mgd: float | None) -> FacilityRecord:
        rows = [TimelineRow(date(2025, 1, 1), "Land clearing begins", 0, 10.0, 12.0, mgd)]
        return site_record(site, rows, placed, ctx=ctx, retrieved_at="2026-10-12T12:00:00+00:00")

    for mgd in (0.0, None):
        rec = water(mgd)
        assert rec.cooling.water_use_mgd is None and "/cooling/water_use_mgd" not in rec.field_meta
    rec = water(2.5)
    assert rec.cooling.water_use_mgd == 2.5
    meta = rec.field_meta["/cooling/water_use_mgd"]
    assert (meta.confidence, meta.method, meta.source_ids) == (0.70, "imported", ["s1"])


# ---------------------------------------------------------------------------- the fixture run


def test_fixture_run_without_geocoding(
    make_test_context: MakeContext, no_overrides: Path, counties: CountyIndex, today: date
) -> None:
    ctx = make_test_context(input_path=EPOCH_ZIP)
    result = IMPORTER.run(ctx, run_args(no_overrides))
    records = by_name(result.candidates)
    # Without the Census step, Colossus 2's postal city (Memphis) gives no point: the address
    # states no county (07 §6.5). The Madison Mega Site's address names Madison County.
    assert sorted(records) == ["Amazon Madison Mega Site"]
    review = {(i.kind, i.external_id) for i in result.review}
    assert review == {
        ("missing_location", "OpenAI Stargate Milam"),
        ("geocode_failed", "Meta Hyperion"),
        ("geocode_failed", "Colossus 2"),
    }
    (colossus,) = [i for i in result.review if i.external_id == "Colossus 2"]
    assert "the address's city (Memphis) is only its postal city" in colossus.reason
    assert "no Census step" in colossus.reason and "config/overrides/epoch.json" in colossus.reason
    assert result.metrics["us_sites"] == 4 and result.metrics["sites"] == 5
    assert result.metrics["timeline_rows"] == 26
    assert "census_requests" not in result.metrics
    (snap,) = result.inputs
    assert snap.license == "CC-BY-4.0" and snap.upstream_version is None
    assert snap.sha256 == hashlib.sha256(EPOCH_ZIP.read_bytes()).hexdigest()
    for c in result.candidates:
        assert validate_record(c.record, counties=counties, today=today) == []
        assert c.record.created_at == ctx.now


def test_control_characters_in_the_csv_are_dropped(
    make_test_context: MakeContext,
    tmp_path: Path,
    no_overrides: Path,
    counties: CountyIndex,
    today: date,
) -> None:
    # One stray ESC or DEL upstream must not hold a record: clean_text is the shared cleaner.
    def edit(name: str, data: bytes) -> bytes:
        return data.replace(b"Colossus 2", b"Colossus\x1b 2\x7f") if name.endswith(".csv") else data

    calls: list[httpx.Request] = []
    ctx = make_test_context(input_path=rezip(tmp_path, edit), handler=census_handler(calls))
    result = IMPORTER.run(ctx, run_args(no_overrides, no_geocode=False))
    records = by_name(result.candidates)
    assert sorted(records) == ["Amazon Madison Mega Site", "Colossus 2"]
    assert not [i for i in result.review if i.kind == "invalid"]
    for c in result.candidates:
        assert validate_record(c.record, counties=counties, today=today) == []
        dumped = c.record.model_dump_json()
        assert "\\u001b" not in dumped and "\\u007f" not in dumped


def test_record_mapping(make_test_context: MakeContext, no_overrides: Path) -> None:
    ctx = make_test_context(input_path=EPOCH_ZIP, handler=census_handler([]))
    result = IMPORTER.run(ctx, run_args(no_overrides, no_geocode=False))
    colossus = by_name(result.candidates)["Colossus 2"]
    # The Census point lies in Memphis city (the place polygons), so the record names it.
    assert colossus.canonical_name == "Colossus 2 (Memphis, TN)"
    rec = colossus  # the latest row on or before 2026-10-12 is 2026-06-15
    assert rec.record_type == "campus" and rec.status == "operating"
    assert (rec.capacity.it_mw, rec.capacity.facility_mw) == (946.0, 1276.0)
    assert rec.money.investment_usd == 35_836_372_000.0
    assert rec.money.investment_basis == "estimate" and rec.money.currency_year == 2025
    assert rec.cooling.water_use_mgd is None
    # Epoch's Owner (the owner of the AI hardware) and Users are tenants; Epoch names no
    # facility owner or operator.
    assert rec.parties.owner == [] and rec.parties.operator == []
    assert [o.name for o in rec.parties.tenant] == ["SpaceXAI", "Anthropic", "Cursor"]
    assert rec.external_ids == {"epoch_name": ["Colossus 2"]}
    assert rec.evidence_level == "reported" and rec.purpose == "unknown"
    assert rec.location.precision == "address" and rec.location.city == "Memphis"
    assert rec.location.geocode_method == "census_geocoder"
    s1, *links = rec.sources
    assert (str(s1.url), s1.publisher, s1.source_type, s1.license) == (
        "https://epoch.ai/data/ai-data-centers",
        "Epoch AI",
        "open_dataset",
        "CC-BY-4.0",
    )
    assert s1.supports == [
        "/canonical_name",
        "/aliases",
        "/parties",
        "/location",
        "/capacity",
        "/cooling",
        "/money",
        "/status_history",
    ]
    assert len(links) == 5 and all(s.supports == [] for s in links)
    assert links[0].title == "WSJ profile of the Colossus data centers"
    assert [s.publisher for s in links] == ["wsj.com", "x.com", "mlgw.com", "x.com", "x.com"]
    assert rec.field_meta["/capacity/it_mw"].method == "imported"
    assert rec.field_meta["/capacity/it_mw"].confidence == 0.70
    events = [(e.status, e.as_of.value, e.planned) for e in rec.status_history]
    assert events == [
        ("under_construction", "2025-02-28", False),
        ("operating", "2025-10-19", False),
    ]
    assert rec.dates["operating_since"].value == "2025-10-19"
    madison = by_name(result.candidates)["Amazon Madison Mega Site"]
    assert madison.canonical_name == "Amazon Madison Mega Site (Madison County, MS)"
    assert madison.location.precision == "county" and madison.location.county_fips == "28089"
    assert madison.location.city is None  # Canton is only the postal city
    assert [o.name for o in madison.parties.tenant] == ["Amazon"]  # not Anthropic #speculative
    assert madison.aliases == []  # Project Rainier #speculative


# The name a quote uses for a city whose Census place name differs (Pryor is Pryor Creek).
QUOTED_AS = {"Pryor Creek": "Pryor"}
# Hosts that answered the project's user agent with 403 or a bot challenge (2026-10-08), so an entry
# citing them must name the Wayback snapshot that was read.
REFUSING_HOSTS = (
    "datacenterdynamics.com",
    "globenewswire.com",
    "michigan.gov",
    "oracle.com",
    "streamdatacenters.com",
)
REFUSES_NOTE = "refuses automated clients"
# "Kansas City, MO — March 20, 2024 — ...": a dateline says where a release was issued.
DATELINE_RE = re.compile(r"^[A-Z][\w .]*, [A-Z]{2}\.? (?:\u2014|\u2013|-) ")


def test_every_committed_override_is_cited(counties: CountyIndex) -> None:
    """Each entry names its source, the verbatim quote that states the place, when it was read,
    and places the site no more precisely than a municipality or county (07 §6.5). The quote
    itself names every place the entry gives: a reader of the record's source sees only the
    quote, not the note."""
    raw = json.loads(REPO_OVERRIDES.read_text(encoding="utf-8"))
    overrides = load_overrides(REPO_OVERRIDES)
    assert REPO_OVERRIDES.parts[-3:] == OVERRIDES_PATH.parts
    assert len(overrides) == len(raw) >= 1
    now = datetime.now(UTC)
    for name, ov in overrides.items():
        for key in ("source_url", "publisher", "source_type", "quote", "retrieved_at", "note"):
            assert raw[name].get(key), (name, key)
        assert 0 < len(ov.quote) <= 300 and ov.retrieved_at <= now, name
        assert find_personal_data(ov.quote) == [] and find_personal_data(ov.note) == [], name
        assert (ov.lat, ov.lon) == (None, None) and ov.precision in ("locality", "county"), name
        assert not DATELINE_RE.match(ov.quote), name
        for place in (ov.city, ov.municipality):
            assert place is None or QUOTED_AS.get(place, place) in ov.quote, (name, place)
        if ov.county_fips is not None:
            county = counties.get(ov.county_fips)
            assert county is not None and county.name in ov.quote, (name, ov.county_fips)
        assert ov.city or ov.county_fips, name
        # A page that refuses automated clients is cited with the snapshot that was read, and the
        # note says so (and only then).
        host = (ov.source_url.host or "").removeprefix("www.")
        if host.endswith(REFUSING_HOSTS):
            assert ov.archive_url is not None, (name, host)
        assert (ov.archive_url is not None) == (REFUSES_NOTE in ov.note), name
        if ov.archive_url is not None:
            assert str(ov.archive_url).startswith("https://web.archive.org/web/"), name
            assert str(ov.archive_url).endswith(str(ov.source_url)), name


def test_committed_override_places_meta_hyperion(
    make_test_context: MakeContext, counties: CountyIndex, today: date
) -> None:
    result = IMPORTER.run(make_test_context(input_path=EPOCH_ZIP), run_args(REPO_OVERRIDES))
    hyperion = by_name(result.candidates)["Meta Hyperion"]
    rec = hyperion
    loc = rec.location
    assert (loc.precision, loc.county_fips, loc.state_abbr, loc.geocode_method) == (
        "county",
        "22083",
        "LA",
        "county_centroid",
    )
    assert (loc.lat, loc.lon) == counties.centroid("22083")
    assert rec.canonical_name == "Meta Hyperion (Richland Parish, LA)"
    s1, s2 = rec.sources[0], rec.sources[1]
    assert "/location" not in s1.supports
    assert (s2.publisher, s2.source_type, s2.supports) == ("Meta", "company_release", ["/location"])
    assert s2.quote is not None and "Richland Parish, Louisiana" in s2.quote
    assert s2.quote_match == "human" and s2.retrieved_at.isoformat() == "2026-10-08T05:15:06+00:00"
    urls = [str(s.url) for s in rec.sources]
    assert len(urls) == len(set(urls))
    assert rec.field_meta["/location"].method == "stated"
    assert rec.field_meta["/location"].source_ids == ["s2"]
    # 07 §3.4: stated with a verbatim quote, 0.85, plus 0.05 for a company release.
    assert rec.field_meta["/location"].confidence == 0.90
    # Projections: the 2028 row (Buildings operational 9) is a planned event, so the status
    # stays under construction and there is no operating_since date.
    planned = [(e.status, e.as_of.value) for e in rec.status_history if e.planned]
    assert planned == [("operating", "2028-01-01")]
    assert rec.status == "under_construction" and rec.record_type == "project"
    assert "operating_since" not in rec.dates
    assert [a.name for a in rec.aliases] == ["Hyperion"]
    assert validate_record(rec, counties=counties, today=today) == []


def test_override_for_a_site_without_an_address(
    make_test_context: MakeContext, tmp_path: Path, counties: CountyIndex, today: date
) -> None:
    milam = counties.by_name("TX", "Milam")
    assert milam is not None
    path = tmp_path / "overrides.json"
    path.write_text(
        json.dumps(
            {
                "OpenAI Stargate Milam": {
                    **CITED,
                    "state_abbr": "TX",
                    "county_fips": milam.fips,
                    "precision": "county",
                    "note": "Epoch's first Selected Source names Milam County, Texas.",
                }
            }
        ),
        encoding="utf-8",
    )
    result = IMPORTER.run(make_test_context(input_path=EPOCH_ZIP), run_args(path))
    rec = by_name(result.candidates)["OpenAI Stargate Milam"]
    assert rec.location.county_fips == milam.fips
    assert rec.sources[1].source_type == "news"  # by host
    assert rec.sources[1].publisher == "datacenterdynamics.com"
    assert rec.field_meta["/location"].confidence == 0.85  # news: no source adjustment
    assert validate_record(rec, counties=counties, today=today) == []
    assert "missing_location" not in {i.kind for i in result.review}
    assert (result.metrics["overrides_used"], result.metrics["overrides_unused"]) == (1, 0)
    # The 2028-12-31 row has no building count but 857 MW of IT power: a planned operating event.
    planned = [(e.status, e.as_of.value) for e in rec.status_history if e.planned]
    assert planned == [("operating", "2028-12-31")]
    assert rec.status == "under_construction" and rec.record_type == "project"


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"state_abbr": "TX", "precision": "county", "note": "x"}, "not a valid overrides file"),
        (  # cited, but without the quote that states the place
            {
                **CITED,
                "state_abbr": "TX",
                "county_fips": "48331",
                "precision": "county",
                "quote": "",
            },
            "not a valid overrides file",
        ),
        (
            {k: v for k, v in CITED.items() if k != "retrieved_at"}
            | {"state_abbr": "TX", "county_fips": "48331", "precision": "county"},
            "not a valid overrides file",
        ),
        (
            {**CITED, "state_abbr": "TX", "county_fips": "22083", "precision": "county"},
            "not a county of TX",
        ),
        ({**CITED, "state_abbr": "TX", "precision": "address"}, "needs lat and lon"),
        ({**CITED, "state_abbr": "TX", "precision": "county"}, "needs county_fips"),
        (
            {
                **CITED,
                "state_abbr": "TX",
                "precision": "locality",
                "city": "Cheyenne",
                "lat": 41.13,
                "lon": -104.8,
            },
            "not inside TX",
        ),
        (  # Abilene's Gazetteer point is in Taylor County, not Shackelford (48417)
            {
                **CITED,
                "state_abbr": "TX",
                "precision": "locality",
                "city": "Abilene",
                "county_fips": "48417",
            },
            "Gazetteer place 'Abilene' is not inside county 48417",
        ),
        (  # coordinates in Texas, but not in the county the entry names (Milam)
            {
                **CITED,
                "state_abbr": "TX",
                "precision": "locality",
                "city": "Abilene",
                "county_fips": "48331",
                "lat": 32.45,
                "lon": -99.73,
            },
            r"\(32\.45, -99\.73\) is not inside county 48331",
        ),
    ],
)
def test_bad_overrides_fail_the_run(
    make_test_context: MakeContext, tmp_path: Path, entry: dict[str, object], message: str
) -> None:
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"OpenAI Stargate Milam": entry}), encoding="utf-8")
    with pytest.raises(FetchError, match=message):
        IMPORTER.run(make_test_context(input_path=EPOCH_ZIP), run_args(path))


def test_license_guard(make_test_context: MakeContext, tmp_path: Path, no_overrides: Path) -> None:
    def edit(name: str, data: bytes) -> bytes:
        return (
            data.replace(b"https://creativecommons.org/licenses/by/4.0/", b"https://example.org/")
            if name == "README.md"
            else data
        )

    edited = rezip(tmp_path, edit)
    with pytest.raises(FetchError, match=r"does not link creativecommons\.org/licenses/by/4\.0"):
        IMPORTER.run(make_test_context(input_path=edited), run_args(no_overrides))


FIXTURE_LICENSE = (
    "Epoch AI's data is free to use, distribute, and reproduce provided the source and authors "
    "are credited under the [Creative Commons Attribution license]"
    "(https://creativecommons.org/licenses/by/4.0/)."
)


@pytest.mark.parametrize(
    ("licensing", "named"),
    [
        (
            "Epoch AI's data may be used for non-commercial purposes only, under the "
            "[Creative Commons Attribution-NonCommercial 4.0 license]"
            "(https://creativecommons.org/licenses/by-nc/4.0/).",
            "non-commercial",
        ),
        (
            "Licensed under the [Creative Commons Attribution-NoDerivatives 4.0 license]"
            "(https://creativecommons.org/licenses/by-nd/4.0/).",
            "NoDeriv",
        ),
        (
            "Licensed under the [Creative Commons Attribution-ShareAlike 4.0 license]"
            "(https://creativecommons.org/licenses/by-sa/4.0/).",
            "ShareAlike",
        ),
        (  # the CC BY 4.0 deed is still linked, but a restriction is added
            FIXTURE_LICENSE + " Commercial use needs a CC BY-NC waiver.",
            "BY-NC",
        ),
    ],
)
def test_license_guard_accepts_only_cc_by_4(
    make_test_context: MakeContext,
    tmp_path: Path,
    no_overrides: Path,
    licensing: str,
    named: str,
) -> None:
    def edit(name: str, data: bytes) -> bytes:
        if name != "README.md":
            return data
        assert FIXTURE_LICENSE.encode() in data
        return data.replace(FIXTURE_LICENSE.encode(), licensing.encode())

    edited = rezip(tmp_path, edit)
    with pytest.raises(FetchError, match=f"names '{named}'"):
        IMPORTER.run(make_test_context(input_path=edited), run_args(no_overrides))


def test_check_license_passes_the_published_readme() -> None:
    with zipfile.ZipFile(EPOCH_ZIP) as zf:
        check_license(zf.read("README.md").decode("utf-8"))  # no error


def test_an_address_without_a_state_is_not_sent_to_the_census(
    make_test_context: MakeContext, no_overrides: Path, tmp_path: Path
) -> None:
    def drop_city(name: str, data: bytes) -> bytes:
        if name != "data_centers.csv":
            return data
        assert b"5420 Tulane Rd, Memphis, TN 38109" in data
        return data.replace(b"5420 Tulane Rd, Memphis, TN 38109", b"5420 Tulane Rd")

    calls: list[httpx.Request] = []
    ctx = make_test_context(input_path=rezip(tmp_path, drop_city), handler=census_handler(calls))
    result = IMPORTER.run(ctx, run_args(no_overrides, no_geocode=False))
    assert "Colossus 2" not in by_name(result.candidates)
    (item,) = [i for i in result.review if i.external_id == "Colossus 2"]
    assert item.kind == "geocode_failed" and "names no state" in item.reason
    assert item.data == {"address": "5420 Tulane Rd"}
    assert [r.url.params["address"] for r in calls] == ["Holly Ridge, LA 71269"]


def test_layout_guards(make_test_context: MakeContext, tmp_path: Path, no_overrides: Path) -> None:
    dropped = rezip(tmp_path, lambda n, d: None if n == "data_center_timelines.csv" else d)
    with pytest.raises(FetchError, match=r"lacks data_center_timelines\.csv"):
        IMPORTER.run(make_test_context(input_path=dropped), run_args(no_overrides))

    def rename(name: str, data: bytes) -> bytes:
        return data.replace(b"Address", b"Location", 1) if name == "data_centers.csv" else data

    renamed = rezip(tmp_path, rename)
    with pytest.raises(FetchError, match="lacks columns"):
        IMPORTER.run(make_test_context(input_path=renamed), run_args(no_overrides))
    not_zip = tmp_path / "not.zip"
    not_zip.write_bytes(b"not a zip")
    with pytest.raises(FetchError, match="refused"):
        IMPORTER.run(make_test_context(input_path=not_zip), run_args(no_overrides))


# ---------------------------------------------------------------------------- with the Census


def test_census_geocoding_and_idempotent_apply(
    make_test_context: MakeContext, no_overrides: Path, tmp_repo: Path
) -> None:
    calls: list[httpx.Request] = []
    ctx = make_test_context(input_path=EPOCH_ZIP, handler=census_handler(calls))
    result = IMPORTER.run(ctx, run_args(no_overrides, no_geocode=False))
    colossus = by_name(result.candidates)["Colossus 2"]
    loc = colossus.location
    assert (loc.precision, loc.geocode_method, loc.county_fips, loc.street, loc.postcode) == (
        "address",
        "census_geocoder",
        "47157",
        "5420 Tulane Rd",
        "38109",
    )
    assert colossus.field_meta["/location"].confidence == 0.90
    # The city is the Census place that contains the point (here the USPS city as well).
    assert (colossus.canonical_name, loc.city) == ("Colossus 2 (Memphis, TN)", "Memphis")
    # Tulane Rd and Holly Ridge are sent; the Madison Mega Site text is over 100 characters.
    assert sorted(r.url.params["address"] for r in calls) == [
        "5420 Tulane Rd, Memphis, TN 38109",
        "Holly Ridge, LA 71269",
    ]
    assert result.metrics["census_requests"] == 2

    store = RecordStore(tmp_repo / "data" / "records")
    review_dir, receipts = tmp_repo / "review" / "queue", tmp_repo / "data" / "imports"
    receipt = apply_import(
        IMPORTER, result, ctx, store=store, review_dir=review_dir, receipts_dir=receipts
    )
    assert receipt.counts["new"] == 2 and receipt.counts["invalid"] == 0
    assert {i.kind for i in read_review_queue(review_dir, "epoch")} == {
        "missing_location",
        "geocode_failed",
    }
    before = snapshot(tmp_repo / "data" / "records", tmp_repo / "review")

    calls.clear()
    again_ctx = make_test_context(
        input_path=EPOCH_ZIP, handler=census_handler(calls), records=store.load()
    )
    again = IMPORTER.run(again_ctx, run_args(no_overrides, no_geocode=False))
    assert calls == [] and again.metrics["census_cache_hits"] == 2  # answered from the cache
    receipt2 = apply_import(
        IMPORTER, again, again_ctx, store=store, review_dir=review_dir, receipts_dir=receipts
    )
    assert receipt2.counts["unchanged"] == 2 and receipt2.counts["updated"] == 0
    # No record or review file changed; the receipt says unchanged=2 instead of new=2.
    assert snapshot(tmp_repo / "data" / "records", tmp_repo / "review") == before


def test_cli_runs_offline_and_a_second_run_changes_no_file(
    tmp_repo: Path,
    repo_root: Path,
    no_overrides: Path,
    capsys: pytest.CaptureFixture[str],
    census_reference_cache: Callable[[Path], Path],
) -> None:
    cache = tmp_repo / ".cache"
    (cache / "census").mkdir(parents=True)
    for path in CENSUS.glob("*.json"):
        shutil.copyfile(path, cache / "census" / path.name)  # the recorded responses
    common = [
        "import",
        "epoch",
        "--input",
        str(EPOCH_ZIP),
        "--overrides",
        str(no_overrides),
        "--records",
        str(tmp_repo / "data" / "records"),
        "--review-dir",
        str(tmp_repo / "review" / "queue"),
        "--receipts-dir",
        str(tmp_repo / "data" / "imports"),
        "--cache-dir",
        str(cache),
        "--counties",
        str(repo_root / "reference" / "census" / "cb_2025_us_county_500k.zip"),
        "--now",
        "2026-10-12T12:00:00Z",
        "--offline",
    ]
    # The place polygons are not in the cache, and --offline forbids the download: the run
    # fails, names the file and says how to get it, and writes nothing.
    assert main(common) == 1
    err = capsys.readouterr().err
    assert "cb_2025_us_place_500k.zip" in err and "without --offline" in err
    assert not (tmp_repo / "data" / "imports" / "epoch.json").exists()
    # --places names the file instead; it is checked against the pin like a downloaded one.
    sample = FIXTURES / "geocode" / "places" / "places_sample.zip"
    assert main([*common, "--places", str(sample)]) == 1
    assert "is not the pinned" in capsys.readouterr().err
    where = census_reference_cache(cache)  # the samples, in place of the downloaded Census files
    places = where / "cb_2025_us_place_500k.zip"
    assert main([*common, "--places", str(places), "--dry-run"]) == 0
    capsys.readouterr()
    assert main(common) == 0
    out = capsys.readouterr().out
    assert "import epoch: candidates=2 new=2" in out
    assert "census_cache_hits=2" in out and "census_requests=0" in out
    records = RecordStore(tmp_repo / "data" / "records").load()
    by_epoch = {r.external_ids["epoch_name"][0]: r for r in records.values()}
    assert by_epoch["Colossus 2"].location.precision == "address"
    assert by_epoch["Colossus 2"].location.city == "Memphis"
    receipt = json.loads((tmp_repo / "data" / "imports" / "epoch.json").read_text("utf-8"))
    assert receipt["source"] == "epoch" and receipt["importer_version"] == "3"
    assert receipt["inputs"][0]["license"] == "CC-BY-4.0"
    records_before = snapshot(tmp_repo / "data" / "records", tmp_repo / "review")

    assert main(common) == 0
    assert "unchanged=2" in capsys.readouterr().out
    assert snapshot(tmp_repo / "data" / "records", tmp_repo / "review") == records_before
    # From the second run on, nothing changes at all, the receipt included.
    everything = snapshot(tmp_repo / "data", tmp_repo / "review")
    assert main(common) == 0
    capsys.readouterr()
    assert snapshot(tmp_repo / "data", tmp_repo / "review") == everything

    # --no-geocode needs no cache at all: without the Census step no place polygons are read.
    shutil.rmtree(cache)
    assert main([*common, "--no-geocode", "--dry-run"]) == 0
    assert "(dry run)" in capsys.readouterr().out


def test_fetch_records_the_etag(
    make_test_context: MakeContext, no_overrides: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("atlas.net._Fetcher._wait_for_host", lambda self, url: None)
    body = EPOCH_ZIP.read_bytes()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == ZIP_URL:
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "application/zip", "etag": '"abc123"'},
                request=request,
            )
        return httpx.Response(404, request=request)  # robots.txt: 404 means allowed

    ctx = make_test_context(handler=handler)
    result = IMPORTER.run(ctx, run_args(no_overrides))
    (snap,) = result.inputs
    assert snap.upstream_version == '"abc123"' and snap.etag == '"abc123"'
    assert ctx.raw_path("epoch", snap.sha256, "zip").read_bytes() == body
    assert seen == ["https://epoch.ai/robots.txt", ZIP_URL]


# ---------------------------------------------------------------------------- postal cities


def with_address(tmp_path: Path, address: str) -> Path:
    """The fixture ZIP with Colossus 2 at another address."""

    def edit(name: str, data: bytes) -> bytes:
        if name != "data_centers.csv":
            return data
        return data.replace(b"5420 Tulane Rd, Memphis, TN 38109", address.encode())

    return rezip(tmp_path, edit)


def geocoded(make_test_context: MakeContext, path: Path, no_overrides: Path) -> Any:
    ctx = make_test_context(input_path=path, handler=census_handler([]))
    return IMPORTER.run(ctx, run_args(no_overrides, no_geocode=False))


def test_a_postal_city_without_a_stated_county_gives_no_point(
    make_test_context: MakeContext, tmp_path: Path, no_overrides: Path
) -> None:
    """n17, n29: AWS Berwick is mailed to Berwick (Columbia County), but the campus is in Salem
    Township, Luzerne County. The Census has no match, and the postal city's point would put the
    record in the wrong county, so the site waits in review for a cited override."""
    address = "1125 Electron Ave, Berwick, PA 18603"
    result = geocoded(make_test_context, with_address(tmp_path, address), no_overrides)
    assert "Colossus 2" not in by_name(result.candidates)
    (item,) = [i for i in result.review if i.external_id == "Colossus 2"]
    assert item.kind == "geocode_failed"
    assert item.reason.startswith("no agreeing Census match, and the address's city (Berwick)")
    assert "config/overrides/epoch.json" in item.reason
    assert item.data == {"address": address}  # the Census returned no match


def test_a_refused_census_match_is_shown_to_the_reviewer(
    make_test_context: MakeContext, tmp_path: Path, no_overrides: Path
) -> None:
    """Anthropic-Amazon New Carlisle: the only match is on Larrison Dr, not Larrison Blvd, so it
    is refused, and New Carlisle is only the postal city."""
    address = "55001 Larrison Blvd, New Carlisle, IN 46552"
    result = geocoded(make_test_context, with_address(tmp_path, address), no_overrides)
    (item,) = [i for i in result.review if i.external_id == "Colossus 2"]
    assert item.kind == "geocode_failed" and "(New Carlisle) is only its postal" in item.reason
    assert item.data["address"] == address
    matches = item.data["census_matches"]
    assert isinstance(matches, list) and len(matches) == 1
    assert "LARRISON DR" in str(matches[0])
    # Answered from the cache on a second run, the item is the same.
    calls: list[httpx.Request] = []
    path = with_address(tmp_path, address)
    again = IMPORTER.run(
        make_test_context(input_path=path, handler=census_handler(calls)),
        run_args(no_overrides, no_geocode=False),
    )
    assert calls == [] and again.metrics["census_cache_hits"] == 2
    assert [i for i in again.review if i.external_id == "Colossus 2"] == [item]


def test_the_city_is_the_census_place_that_contains_the_point(
    make_test_context: MakeContext, tmp_path: Path, no_overrides: Path
) -> None:
    """n22, n38: QTS Cedar Rapids is mailed to Fairfax, IA. Its Census point lies 5 m inside Cedar
    Rapids city on the 1:500,000 lines, too close to tell, so the record names its county, never
    the postal city."""
    address = "6200 76th Ave SW, Fairfax, IA 52228"
    result = geocoded(make_test_context, with_address(tmp_path, address), no_overrides)
    rec = by_name(result.candidates)["Colossus 2"]
    assert rec.location.precision == "address" and rec.location.city is None
    assert rec.canonical_name == "Colossus 2 (Linn County, IA)"
    # Lordstown: mailed to Warren, the point lies in Lordstown village.
    lordstown = "2300 Hallock Young Rd, Warren, OH 44481"
    rec = by_name(
        geocoded(make_test_context, with_address(tmp_path, lordstown), no_overrides).candidates
    )["Colossus 2"]
    assert rec.location.city == "Lordstown" and rec.canonical_name == "Colossus 2 (Lordstown, OH)"


def test_a_county_subdivision_result_names_the_municipality() -> None:
    from atlas.geocode import GeocodeResult
    from atlas.sources.epoch import _location_from_result

    result = GeocodeResult(
        lat=41.843772,
        lon=-72.741002,
        precision="locality",
        method="gazetteer",
        state_abbr="CT",
        county_fips="09110",
        county_name="Capitol",
        city=None,
        postcode=None,
        street=None,
        matched_address="Bloomfield town, CT",
        confidence=0.60,
        municipality="Bloomfield",
    )
    location = _location_from_result(result)
    assert (location["municipality"], location["city"]) == ("Bloomfield", None)


def test_link_source_types_read_the_path_and_the_link_text(
    make_test_context: MakeContext, tmp_path: Path, counties: CountyIndex, today: date
) -> None:
    """n53 (classify_source): a permit on Google Drive and an SEC filing on an investor site are
    not news; an override citing an ISO is an ISO filing."""
    site = Site(
        "Test site",
        "",
        "",
        "",
        "",
        "- [Air Construction Permit](https://drive.google.com/file/d/abc/view)\n"
        "- [SEC 8k Filing](https://ir.applieddigital.com/financial-information/quarterly-results)\n"
        "- [Local story](https://www.example-news.com/x)\n",
        None,
    )
    placed = Placed(
        {"lat": None, "lon": None, "precision": "state", "state_abbr": "TN"},
        None,
        0.60,
        "override",
        None,
    )
    rows = [TimelineRow(date(2025, 1, 1), "Land clearing begins", 0, None, None, None)]
    rec = site_record(
        site, rows, placed, ctx=make_test_context(), retrieved_at="2026-10-12T12:00:00+00:00"
    )
    assert [s.source_type for s in rec.sources[1:]] == [
        "government_record",
        "sec_filing",
        "news",
    ]
    from atlas.sources.epoch import LocationOverride, override_source_type

    ov = LocationOverride.model_validate(
        {
            **CITED,
            "source_url": "https://cdn.misoenergy.org/new-load.pdf",
            "state_abbr": "IA",
            "precision": "county",
            "county_fips": "19153",
        }
    )
    assert override_source_type(ov) == "utility_or_iso_filing"


# ---------------------------------------------------------------------------- live


@pytest.mark.network
def test_live_epoch_zip(tmp_path: Path) -> None:
    from atlas.sources.epoch import COUNTRY, read_zip

    with make_client() as client:
        got = fetch(client, ZIP_URL, allowed_types=("application/zip", "application/octet-stream"))
    path = tmp_path / "data_centers.zip"
    path.write_bytes(got.content)
    readme, sites, timelines = read_zip(path)
    assert "Creative Commons Attribution" in readme
    assert sum(1 for r in sites if r["Country"].strip() == COUNTRY) >= 60
    assert len(timelines) >= 400
