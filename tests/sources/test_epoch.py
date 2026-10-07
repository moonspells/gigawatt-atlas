"""The Epoch AI importer (atlas/sources/epoch.py) against the committed fixture ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import zipfile
from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx
import pytest

from atlas.cli import main
from atlas.geo.counties import CountyIndex
from atlas.net import FetchError, fetch, make_client
from atlas.schema.record import FacilityRecord
from atlas.sources.base import Candidate, ImportContext, apply_import, read_review_queue
from atlas.sources.epoch import (
    IMPORTER,
    ZIP_URL,
    TimelineRow,
    event_note,
    link_source_type,
    selected_links,
    status_events,
    strip_links,
    tagged_names,
)
from atlas.store import RecordStore
from atlas.validate import validate_record

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EPOCH_ZIP = FIXTURES / "epoch" / "data_centers.zip"
CENSUS = FIXTURES / "census"
REPO_OVERRIDES = Path(__file__).resolve().parents[2] / "config" / "overrides" / "epoch.json"
MILAM_SOURCE = (
    "https://www.datacenterdynamics.com/en/news/softbanks-sb-energy-to-build-and-operate-"
    "openais-12gw-stargate-data-center-in-milam-county-texas/"
)
MakeContext = Callable[..., ImportContext]


def census_handler(calls: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    """Answers the Census Geocoder from tests/fixtures/census; everything else is 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host != "geocoding.geo.census.gov":
            return httpx.Response(404, request=request)
        digest = hashlib.sha256(request.url.params["address"].encode()).hexdigest()
        path = CENSUS / f"{digest}.json"
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
    assert link_source_type("https://www.sec.gov/Archives/edgar/data/1/x.htm") == "sec_filing"
    assert link_source_type("https://www.tdlr.texas.gov/TABS/Search/Project/X") == (
        "government_record"
    )
    assert link_source_type("https://example.us/permit") == "government_record"
    assert link_source_type("https://www.mlgw.com/x.pdf") == "news"
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


# ---------------------------------------------------------------------------- the fixture run


def test_fixture_run_without_geocoding(
    make_test_context: MakeContext, no_overrides: Path, counties: CountyIndex, today: date
) -> None:
    ctx = make_test_context(input_path=EPOCH_ZIP)
    result = IMPORTER.run(ctx, run_args(no_overrides))
    records = by_name(result.candidates)
    assert sorted(records) == ["Amazon Madison Mega Site", "Colossus 2"]
    review = {(i.kind, i.external_id) for i in result.review}
    assert review == {
        ("missing_location", "OpenAI Stargate Milam"),
        ("geocode_failed", "Meta Hyperion"),
    }
    assert result.metrics["us_sites"] == 4 and result.metrics["sites"] == 5
    assert result.metrics["timeline_rows"] == 26
    assert "census_requests" not in result.metrics
    (snap,) = result.inputs
    assert snap.license == "CC-BY-4.0" and snap.upstream_version is None
    assert snap.sha256 == hashlib.sha256(EPOCH_ZIP.read_bytes()).hexdigest()
    for c in result.candidates:
        assert validate_record(c.record, counties=counties, today=today) == []
        assert c.record.created_at == ctx.now


def test_record_mapping(make_test_context: MakeContext, no_overrides: Path) -> None:
    result = IMPORTER.run(make_test_context(input_path=EPOCH_ZIP), run_args(no_overrides))
    colossus = by_name(result.candidates)["Colossus 2"]
    assert colossus.canonical_name == "Colossus 2 (Memphis, TN)"
    rec = colossus  # the latest row on or before 2026-10-12 is 2026-06-15
    assert rec.record_type == "campus" and rec.status == "operating"
    assert (rec.capacity.it_mw, rec.capacity.facility_mw) == (946.0, 1276.0)
    assert rec.money.investment_usd == 35_836_372_000.0
    assert rec.money.investment_basis == "estimate" and rec.money.currency_year == 2025
    assert rec.cooling.water_use_mgd is None
    assert [o.name for o in rec.parties.operator] == ["SpaceXAI"]
    assert [o.name for o in rec.parties.tenant] == ["Anthropic", "Cursor", "SpaceXAI"]
    assert rec.external_ids == {"epoch_name": ["Colossus 2"]}
    assert rec.evidence_level == "reported" and rec.purpose == "unknown"
    assert rec.location.precision == "locality" and rec.location.geocode_method == "gazetteer"
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
    assert madison.parties.tenant == []  # Anthropic #speculative
    assert madison.aliases == []  # Project Rainier #speculative


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
    urls = [str(s.url) for s in rec.sources]
    assert len(urls) == len(set(urls))
    assert rec.field_meta["/location"].method == "stated"
    assert rec.field_meta["/location"].source_ids == ["s2"]
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
                    "state_abbr": "TX",
                    "county_fips": milam.fips,
                    "precision": "county",
                    "source_url": MILAM_SOURCE,
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
    assert validate_record(rec, counties=counties, today=today) == []
    assert "missing_location" not in {i.kind for i in result.review}
    assert (result.metrics["overrides_used"], result.metrics["overrides_unused"]) == (1, 0)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"state_abbr": "TX", "precision": "county", "note": "x"}, "not a valid overrides file"),
        (
            {
                "state_abbr": "TX",
                "county_fips": "22083",
                "precision": "county",
                "source_url": MILAM_SOURCE,
                "note": "x",
            },
            "not a county of TX",
        ),
        (
            {"state_abbr": "TX", "precision": "address", "source_url": MILAM_SOURCE, "note": "x"},
            "needs lat and lon",
        ),
        (
            {"state_abbr": "TX", "precision": "county", "source_url": MILAM_SOURCE, "note": "x"},
            "needs county_fips",
        ),
        (
            {
                "state_abbr": "TX",
                "precision": "locality",
                "city": "Cheyenne",
                "lat": 41.13,
                "lon": -104.8,
                "source_url": MILAM_SOURCE,
                "note": "x",
            },
            "not inside TX",
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
            data.replace(b"Creative Commons Attribution", b"All rights reserved")
            if name == "README.md"
            else data
        )

    edited = rezip(tmp_path, edit)
    with pytest.raises(FetchError, match="Creative Commons Attribution"):
        IMPORTER.run(make_test_context(input_path=edited), run_args(no_overrides))


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
    tmp_repo: Path, repo_root: Path, no_overrides: Path, capsys: pytest.CaptureFixture[str]
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
        str(repo_root / "reference" / "census" / "cb_2025_us_county_5m.zip"),
        "--now",
        "2026-10-12T12:00:00Z",
        "--offline",
    ]
    assert main(common) == 0
    out = capsys.readouterr().out
    assert "import epoch: candidates=2 new=2" in out
    assert "census_cache_hits=2" in out and "census_requests=0" in out
    records = RecordStore(tmp_repo / "data" / "records").load()
    by_epoch = {r.external_ids["epoch_name"][0]: r for r in records.values()}
    assert by_epoch["Colossus 2"].location.precision == "address"
    receipt = json.loads((tmp_repo / "data" / "imports" / "epoch.json").read_text("utf-8"))
    assert receipt["source"] == "epoch" and receipt["importer_version"] == "1"
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

    # --no-geocode needs no cache at all.
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
