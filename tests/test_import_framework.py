from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

import atlas.sources
from atlas import __version__
from atlas.cli import main
from atlas.commands.importer import discover_importers
from atlas.geo.places import PlaceIndex
from atlas.jsonio import record_json
from atlas.net import FetchError, fetch
from atlas.schema.record import PLACEHOLDER_ID, FacilityRecord
from atlas.sources.base import (
    Candidate,
    ImportContext,
    ImportReceipt,
    ImportResult,
    ReviewItem,
    apply_import,
    classify_source,
    load_input,
    read_review_queue,
)
from atlas.store import RecordStore

PLACES_SAMPLE = (
    Path(__file__).resolve().parent / "fixtures" / "geocode" / "places" / "places_sample.zip"
)

MakeRecord = Callable[..., FacilityRecord]
MakeContext = Callable[..., ImportContext]
LATER = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


class DummyImporter:
    name = "dummy"
    match_key = "dummy_id"
    owned_external_keys = ("dummy_id",)
    review_sources = ("dummy",)
    version = "3"
    help = "a test importer"

    def __init__(self, candidates: list[Candidate], review: list[ReviewItem] | None = None) -> None:
        self.candidates = candidates
        self.review = review or []

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        pass

    def run(self, ctx: ImportContext, args: argparse.Namespace) -> ImportResult:
        return ImportResult(
            "dummy", [], self.candidates, self.review, {"rows": len(self.candidates)}
        )


class RecordingStore(RecordStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.written: list[str] = []

    def write(self, record: FacilityRecord) -> Path:
        self.written.append(record.id)
        return super().write(record)


@pytest.fixture
def scenario(
    tmp_repo: Path, make_record: MakeRecord
) -> tuple[RecordingStore, dict[str, FacilityRecord], list[Candidate]]:
    """Stored records and candidates covering every apply_import outcome."""
    store = RecordingStore(tmp_repo / "data" / "records")

    def stored(key: str, **kw: Any) -> FacilityRecord:
        r = make_record(external_ids={"dummy_id": [key], **kw.pop("external_ids", {})}, **kw)
        store.write(r)
        return r

    def candidate(key: str, **kw: Any) -> Candidate:
        r = make_record(id=PLACEHOLDER_ID, external_ids={"dummy_id": [key]}, **kw)
        return Candidate((key,), r)

    s = {
        "same": stored("A", canonical_name="Same (Ashburn, VA)"),
        "changed": stored(
            "B", canonical_name="Changed (Ashburn, VA)", external_ids={"osm": ["way/1"]}
        ),
        "reviewed": stored("C", review={"state": "human_reviewed"}),
        "merged": stored("D"),
        "twin1": stored("E"),
        "twin2": stored("E"),
        "gone": stored("F"),
    }
    merged = s["merged"].model_copy(update={"merged_into": s["same"].id})
    store.write(merged)
    s["merged"] = merged
    store.written.clear()
    candidates = [
        candidate("A", canonical_name="Same (Ashburn, VA)"),
        candidate("B", canonical_name="Changed (Ashburn, VA)", capacity={"it_mw": 12}),
        candidate("C"),
        candidate("D"),
        candidate("E"),
        candidate("G", canonical_name="New (Ashburn, VA)"),
        candidate("H", capacity={"it_mw": 20_000}),
    ]
    return store, s, candidates


def run_import(
    tmp_repo: Path,
    store: RecordStore,
    candidates: list[Candidate],
    ctx: ImportContext,
    *,
    dry_run: bool = False,
) -> ImportReceipt:
    importer = DummyImporter(
        candidates,
        [
            ReviewItem(
                source="dummy", kind="unmatched", external_id="Z", reason="no match in upstream"
            )
        ],
    )
    return apply_import(
        importer,
        importer.run(ctx, argparse.Namespace()),
        ctx,
        store=store,
        review_dir=tmp_repo / "review" / "queue",
        receipts_dir=tmp_repo / "data" / "imports",
        dry_run=dry_run,
    )


def test_apply_import_outcomes(
    tmp_repo: Path,
    scenario: tuple[RecordingStore, dict[str, FacilityRecord], list[Candidate]],
    make_test_context: MakeContext,
    fixed_now: datetime,
) -> None:
    store, s, candidates = scenario
    ctx = make_test_context(now=LATER, today=LATER.date(), records=store.load())
    receipt = run_import(tmp_repo, store, candidates, ctx)

    assert receipt.counts == {
        "candidates": 7,
        "new": 1,
        "updated": 1,
        "unchanged": 1,
        "held": 2,
        "invalid": 1,
        "conflicts": 1,
        "removed_upstream": 1,
        "review": 6,
    }
    after = store.load()
    assert len(after) == len(s) + 1  # one new record; nothing deleted

    # unchanged: not rewritten
    assert s["same"].id not in store.written
    assert after[s["same"].id] == s["same"]

    # updated: keeps id, created_at, review and foreign external ids; timestamps move
    changed = after[s["changed"].id]
    assert changed.capacity.it_mw == 12
    assert changed.created_at == s["changed"].created_at == fixed_now
    assert changed.updated_at == changed.last_verified_at == LATER
    assert changed.external_ids == {"dummy_id": ["B"], "osm": ["way/1"]}

    # held records are untouched
    assert after[s["reviewed"].id] == s["reviewed"]
    assert after[s["merged"].id] == s["merged"]

    # new: id from ctx.new_id, timestamps from ctx.now
    (new,) = [r for r in after.values() if r.external_ids.get("dummy_id") == ["G"]]
    assert new.id != PLACEHOLDER_ID
    assert new.created_at == new.updated_at == new.last_verified_at == LATER
    assert sorted(store.written) == sorted([changed.id, new.id])

    queue = read_review_queue(tmp_repo / "review" / "queue", "dummy")
    assert [(i.kind, i.external_id) for i in queue] == [
        ("conflict", "E"),
        ("held_human_reviewed", "C"),
        ("held_merged", "D"),
        ("invalid", "H"),
        ("removed_upstream", "F"),
        ("unmatched", "Z"),
    ]
    by_kind = {i.kind: i for i in queue}
    record_ids = by_kind["conflict"].data["record_ids"]
    assert isinstance(record_ids, list)
    assert sorted(str(i) for i in record_ids) == sorted([s["twin1"].id, s["twin2"].id])
    assert by_kind["removed_upstream"].record_id == s["gone"].id
    assert by_kind["held_human_reviewed"].record_id == s["reviewed"].id
    issues = by_kind["invalid"].data["issues"]
    assert isinstance(issues, list) and issues[0] == {
        "rule": "range",
        "pointer": "/capacity/it_mw",
        "message": "it_mw 20000.0 is outside (0, 10000]",
    }
    text = (tmp_repo / "review" / "queue" / "dummy.jsonl").read_text(encoding="utf-8")
    assert text.endswith("\n") and text.count("\n") == 6
    assert json.loads(text.splitlines()[0])["kind"] == "conflict"

    on_disk = ImportReceipt.model_validate_json(
        (tmp_repo / "data" / "imports" / "dummy.json").read_text(encoding="utf-8")
    )
    assert on_disk == receipt
    assert (receipt.source, receipt.importer_version, receipt.atlas_version) == (
        "dummy",
        "3",
        __version__,
    )
    assert receipt.ran_at == LATER
    assert receipt.metrics == {"rows": 7}


def test_second_run_is_unchanged(
    tmp_repo: Path,
    scenario: tuple[RecordingStore, dict[str, FacilityRecord], list[Candidate]],
    make_test_context: MakeContext,
) -> None:
    store, _, candidates = scenario
    run_import(tmp_repo, store, candidates, make_test_context(now=LATER, today=LATER.date()))
    store.written.clear()
    again = run_import(
        tmp_repo, store, candidates, make_test_context(now=LATER, today=LATER.date())
    )
    assert again.counts["new"] == 0
    assert again.counts["updated"] == 0
    assert again.counts["unchanged"] == 3
    assert store.written == []


def test_dry_run_writes_nothing(
    tmp_repo: Path,
    scenario: tuple[RecordingStore, dict[str, FacilityRecord], list[Candidate]],
    make_test_context: MakeContext,
) -> None:
    store, s, candidates = scenario
    receipt = run_import(tmp_repo, store, candidates, make_test_context(), dry_run=True)
    assert receipt.counts["new"] == 1
    assert store.written == []
    assert len(store.load()) == len(s)
    assert not (tmp_repo / "review" / "queue" / "dummy.jsonl").exists()
    assert not (tmp_repo / "data" / "imports" / "dummy.json").exists()


def test_empty_review_file_is_written(tmp_repo: Path, make_test_context: MakeContext) -> None:
    importer = DummyImporter([])
    ctx = make_test_context()
    apply_import(
        importer,
        importer.run(ctx, argparse.Namespace()),
        ctx,
        store=RecordStore(tmp_repo / "data" / "records"),
        review_dir=tmp_repo / "review" / "queue",
        receipts_dir=tmp_repo / "data" / "imports",
    )
    assert (tmp_repo / "review" / "queue" / "dummy.jsonl").read_text(encoding="utf-8") == ""


def test_review_items_must_belong_to_the_importer(
    tmp_repo: Path, make_test_context: MakeContext
) -> None:
    importer = DummyImporter([], [ReviewItem(source="other", kind="unmatched", reason="x")])
    ctx = make_test_context()
    with pytest.raises(ValueError, match="is not in"):
        apply_import(
            importer,
            importer.run(ctx, argparse.Namespace()),
            ctx,
            store=RecordStore(tmp_repo / "data" / "records"),
            review_dir=tmp_repo / "review" / "queue",
            receipts_dir=tmp_repo / "data" / "imports",
        )


def test_candidate_needs_match_values(make_record: MakeRecord) -> None:
    with pytest.raises(ValueError, match="match value"):
        Candidate((), make_record())


def test_load_input_from_file_and_from_fetch(
    tmp_path: Path, make_test_context: MakeContext, fixed_now: datetime
) -> None:
    local = tmp_path / "rows.json"
    local.write_bytes(b"[1, 2]")
    ctx = make_test_context(input_path=local)
    data, snap = load_input(
        ctx,
        name="dummy",
        url="https://example.org/rows.json",
        license="CC0-1.0",
        ext="json",
        fetch=lambda: pytest.fail("must not fetch"),
    )
    assert data == b"[1, 2]"
    assert (snap.bytes, snap.retrieved_at, str(snap.url)) == (
        6,
        fixed_now,
        "https://example.org/rows.json",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, headers={"ETag": '"abc"', "Content-Type": "application/json"}, content=b"[3]"
        )

    ctx = make_test_context(handler=handler)
    data, snap = load_input(
        ctx,
        name="dummy",
        url="https://example.org/rows.json",
        license="CC0-1.0",
        ext=".json",
        fetch=lambda: fetch(ctx.http, "https://example.org/rows.json", sleep=lambda s: None),
        upstream_version="v2",
    )
    assert data == b"[3]"
    assert (snap.etag, snap.upstream_version) == ('"abc"', "v2")
    raw = ctx.raw_path("dummy", snap.sha256, "json")
    assert raw == ctx.cache_dir / "raw" / "dummy" / f"{snap.sha256}.json"
    assert raw.read_bytes() == b"[3]"


DUMMY_MODULE = """
from __future__ import annotations

import argparse
import json

from atlas.net import fetch
from atlas.schema.record import PLACEHOLDER_ID, FacilityRecord
from atlas.schema.rollup import apply_rollup
from atlas.sources.base import Candidate, ImportContext, ImportResult, load_input

URL = "https://example.org/zz-dummy.json"


class _ZzDummy:
    name = "zzdummy"
    match_key = "zzdummy_id"
    owned_external_keys = ("zzdummy_id",)
    review_sources = ("zzdummy",)
    version = "1"
    help = "a discovered test importer"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--mw", type=float, default=10.0)

    def run(self, ctx: ImportContext, args: argparse.Namespace) -> ImportResult:
        data, snap = load_input(ctx, name=self.name, url=URL, license="CC0-1.0", ext="json",
                                fetch=lambda: fetch(ctx.http, URL))
        ts = ctx.now.isoformat()
        candidates = []
        for row in json.loads(data):
            record = FacilityRecord.model_validate({
                "id": PLACEHOLDER_ID, "record_type": "project",
                "canonical_name": row["name"], "status": "announced", "evidence_level": "reported",
                "status_history": [{"seq": 1, "status": "announced", "event": "announced",
                                    "as_of": {"value": "2026-03", "precision": "month"},
                                    "source_ids": ["s1"]}],
                "location": {"precision": "state", "state_abbr": "TX"},
                "capacity": {"it_mw": args.mw},
                "external_ids": {"zzdummy_id": [row["key"]]},
                "sources": [{"id": "s1", "url": URL, "publisher": "Example", "source_type": "open_dataset",
                             "license": "CC0-1.0", "retrieved_at": ts,
                             "supports": ["/canonical_name", "/location", "/capacity"]}],
                "review": {"state": "machine"},
                "created_at": ts, "updated_at": ts, "last_verified_at": ts,
            })
            candidates.append(Candidate((row["key"],), apply_rollup(record)))
        return ImportResult(self.name, [snap], candidates, [], {"rows": len(candidates)})


IMPORTER = _ZzDummy()
"""


@pytest.fixture
def discovered_importer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """atlas/sources gains a module zz_dummy_source.py (from a temp dir) that defines IMPORTER."""
    extra = tmp_path / "extra_sources"
    extra.mkdir()
    (extra / "zz_dummy_source.py").write_text(DUMMY_MODULE, encoding="utf-8")
    monkeypatch.setattr(atlas.sources, "__path__", [*atlas.sources.__path__, str(extra)])
    yield extra
    sys.modules.pop("atlas.sources.zz_dummy_source", None)


def test_atlas_import_discovers_and_runs_an_importer(
    discovered_importer: Path, tmp_repo: Path, repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "zzdummy" in discover_importers()
    rows = tmp_repo / "rows.json"
    rows.write_text(json.dumps([{"key": "k1", "name": "Example One (TX)"}]), encoding="utf-8")
    common = [
        "--records",
        str(tmp_repo / "data" / "records"),
        "--review-dir",
        str(tmp_repo / "review" / "queue"),
        "--receipts-dir",
        str(tmp_repo / "data" / "imports"),
        "--cache-dir",
        str(tmp_repo / ".cache"),
        "--counties",
        str(repo_root / "reference" / "census" / "cb_2025_us_county_500k.zip"),
        "--now",
        "2026-10-12T12:00:00Z",
        "--offline",
    ]
    assert main(["import", "zzdummy", "--input", str(rows), "--mw", "25", *common]) == 0
    out = capsys.readouterr().out
    assert "import zzdummy: candidates=1 new=1" in out
    (record,) = RecordStore(tmp_repo / "data" / "records").load().values()
    assert record.capacity.it_mw == 25
    receipt = json.loads(
        (tmp_repo / "data" / "imports" / "zzdummy.json").read_text(encoding="utf-8")
    )
    assert receipt["inputs"][0]["sha256"] and receipt["ran_at"] == "2026-10-12T12:00:00Z"
    assert (tmp_repo / "review" / "queue" / "zzdummy.jsonl").read_text(encoding="utf-8") == ""

    # --offline without --input: the fetch fails and the command exits 1.
    assert main(["import", "zzdummy", *common]) == 1
    assert "offline" in capsys.readouterr().err

    # A run that produces an invalid record exits 1 and writes nothing for it.
    assert main(["import", "zzdummy", "--input", str(rows), "--mw", "20000", *common]) == 1
    assert "invalid=1" in capsys.readouterr().out
    (again,) = RecordStore(tmp_repo / "data" / "records").load().values()
    assert again.capacity.it_mw == 25
    assert record_json(again) == record_json(record)

    # --now must carry a time zone.
    assert main(["import", "zzdummy", "--now", "2026-10-12T12:00:00", "--offline"]) == 2


# ---------------------------------------------------------------------------- source types


@pytest.mark.parametrize(
    ("url", "title", "expected"),
    [
        # n20: primary documents the seed published as news.
        (
            "https://ir.applieddigital.com/sec-filings/all-sec-filings/content/"
            "0001641172-25-013199/form8-k.htm",
            "SEC 8k Filing with Applied Digital",
            "sec_filing",
        ),
        (
            "https://investors.corescientific.com/sec-filings/all-sec-filings/content/"
            "0001628280-26-031246/q1fy26earningsdeck.htm",
            "Core Scientific - Q1 2026 Fiscal Earnings Call",
            "sec_filing",
        ),
        (
            "https://ir.applieddigital.com/news-events/press-releases/detail/157/"
            "applied-digital-delivers-second-building-at-polaris-forge-1",
            "Applied Digital Delivers Second Building at Polaris Forge 1",
            "company_release",
        ),
        (
            "https://citycouncildocuments.acfw.net/documents/download/2023-12-19/R-23-12-02.pdf",
            "City Council approval",
            "government_record",
        ),
        (
            "https://www.govonlinesaas.com/LCPH/EasyAir/Public/EnSuite/Shared/Pages/util/"
            "StreamDoc.ashx?id=260&type=attachment",
            "Permit application",
            "government_record",
        ),
        (
            "https://cdn.misoenergy.org/NEW%20LOAD%20ANNOUNCEMENTS%20IN%20MISO%20REGIONS%2012062024684954.pdf",
            "MISO new load announcements",
            "utility_or_iso_filing",
        ),
        # Documents on a document store: the link text says what they are.
        (
            "https://drive.google.com/file/d/1dQ5/view",
            "Air Construction Permit",
            "government_record",
        ),
        (
            "https://drive.google.com/file/d/1D4e/view",
            "MISO update with 1.8 GW substation",
            "utility_or_iso_filing",
        ),
        ("https://drive.google.com/file/d/1oHW/view", "Crusoe 2024 Impact Report", "news"),
        ("https://drive.google.com/file/d/1abc/view", "30 sec site walk", "news"),
        ("https://drive.google.com/file/d/1def/view", "Form 10-q, Q2 2025", "sec_filing"),
        (
            "https://www.scribd.com/document/1/x",
            "July 2025 Permit for Gas Turbines",
            "government_record",
        ),
        # Host rules, unchanged.
        ("https://www.sec.gov/Archives/edgar/data/1/x.htm", None, "sec_filing"),
        ("https://www.tdlr.texas.gov/TABS/Projects/TABS2026012091", None, "government_record"),
        ("https://co.armstrong.tx.us/page", None, "government_record"),
        ("https://lnklan.granicus.com/DocumentViewer.php?file=a.pdf", None, "government_record"),
        ("https://www.prnewswire.com/news-releases/x-302836104.html", None, "company_release"),
        (
            "https://investors.coreweave.com/news/news-details/2025/x/default.aspx",
            None,
            "company_release",
        ),
        ("https://www.pjm.com/-/media/planning/x.pdf", None, "utility_or_iso_filing"),
        # A news site's headline is not read: these stay news.
        ("https://www.datacenterdynamics.com/en/news/x/", "County approves permit", "news"),
        ("https://www.reuters.com/x/", "SEC filing shows Meta lease", "news"),
        ("https://www.thelocalfw.com/data-center-diesel-generators/", None, "news"),
        ("https://notsec.gov.example.com/x", None, "news"),
    ],
)
def test_classify_source(url: str, title: str | None, expected: str) -> None:
    assert classify_source(url, title) == expected


# ---------------------------------------------------------------------------- places


def test_context_gives_the_place_polygons(make_test_context: MakeContext) -> None:
    sample = PlaceIndex.load(PLACES_SAMPLE, verify_sha256=False)
    ctx = make_test_context(places=sample)
    assert ctx.places() is sample
    assert ctx.places().city_at(41.143721, -80.883293, "OH") == "Lordstown"  # mailed to Warren
    sample.close()
    # A file given by path (--places) is checked like a downloaded one: the sample is not the
    # Census file, so the run fails with a FetchError (exit 1), not a traceback.
    with pytest.raises(FetchError, match="is not the pinned"):
        make_test_context(places_path=PLACES_SAMPLE).places()
