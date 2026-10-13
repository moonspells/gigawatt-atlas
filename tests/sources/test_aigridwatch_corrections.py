"""The cited corrections and guards of config/overrides/aigridwatch.json (fix round 6;
docs/corrections.md, docs/sources/epoch-aigridwatch.md "Reviewer entries"): corrections applied
where a row is imported, after its holds; expect; rows the file does not have; reason and
reviewed_at only with a release; a cited first report that lifts the first-report hold; and a
reviewer's event that would date a first report it is not, which holds the row. The rows are the
fixtures' (Microsoft Cheyenne Expansion, Kearney, Amazon Bridgefield); the corrections are the
reviewers' kind, cited to an example page. tests/sources/test_corrections.py tests the shared
ops themselves, and tests/sources/test_committed_corrections.py the citation of every committed
Fix."""

from __future__ import annotations

import argparse
import copy
import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from atlas.geo.counties import CountyIndex
from atlas.net import FetchError
from atlas.schema.record import FacilityRecord
from atlas.sources.aigridwatch import IMPORTER, RowRelease, load_releases
from atlas.sources.base import ImportContext, ImportResult, ReviewItem, merge_import
from atlas.store import RecordStore
from atlas.validate import validate_record

MakeContext = Callable[..., ImportContext]
SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "aigridwatch"
FIX6 = SAMPLES / "fix6-2026-10-08.json"
PROJECTS = SAMPLES / "projects.json"
REPO_OVERRIDES = Path(__file__).resolve().parents[2] / "config" / "overrides" / "aigridwatch.json"
NOW = datetime(2026, 10, 9, 9, 52, 35, tzinfo=UTC)
TODAY = NOW.date()
PAGE = "https://example.com/news/project"
CHEYENNE = "microsoft-cheyenne-expansion"
KEARNEY = "kearney-data-center-ne"
BRIDGEFIELD = "amazon-bridgefield-ashburn-va"
GATEWAY = "pw-digital-gateway-va"
RELEASE = {"reason": "test: the reviewer read the county's record", "reviewed_at": "2026-10-09"}


def fix(*ops: dict[str, Any], url: str = PAGE, **extra: Any) -> dict[str, Any]:
    """One Fix as the overrides file writes it, read on the import's day."""
    return {
        "source_url": url,
        "publisher": "Example News",
        "source_type": "news",
        "quote": "A sentence that states the correction.",
        "basis": "out_of_date",
        "reason": "The reviewer read the cited page",
        "retrieved_at": "2026-10-09T08:00:00Z",
        "reviewed_at": "2026-10-09",
        "ops": list(ops),
        **extra,
    }


def event(kind: str, status: str, as_of: str) -> dict[str, Any]:
    return {"op": "add_event", "event": kind, "status": status, "as_of": as_of}


def row(pid: str, path: Path = FIX6) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    found: dict[str, Any] = next(p for p in doc["projects"] if p["id"] == pid)
    return found


@pytest.fixture
def run(make_test_context: MakeContext, tmp_path: Path) -> Callable[..., ImportResult]:
    """The importer on a fixture on the import's day, without Epoch records, with the entries as
    its overrides file (ctx: other ImportContext arguments, such as the stored records)."""

    def go(entries: dict[str, Any], path: Path = FIX6, **ctx: Any) -> ImportResult:
        overrides = tmp_path / "aigridwatch.json"
        overrides.write_text(json.dumps(entries), encoding="utf-8")
        args = argparse.Namespace(without_epoch=True, overrides=overrides)
        context = make_test_context(**{"input_path": path, "now": NOW, "today": TODAY, **ctx})
        return IMPORTER.run(context, args)

    return go


Run = Callable[..., ImportResult]


def record(result: ImportResult, pid: str) -> FacilityRecord:
    (found,) = [c.record for c in result.candidates if c.match_values == (pid,)]
    return found


def items(result: ImportResult, pid: str) -> list[ReviewItem]:
    return [i for i in result.review if i.external_id == pid]


def events(rec: FacilityRecord) -> list[tuple[str, str, str]]:
    return [(e.event, e.status, e.as_of.value) for e in rec.status_history]


# ---------------------------------------------------------------------------- the entry model


def test_reason_and_reviewed_at_go_with_a_release(tmp_path: Path) -> None:
    hold = fix({"op": "hold"})
    only = RowRelease.model_validate({"corrections": [hold]})
    assert not only.decides and only.release == [] and only.reason is None
    with pytest.raises(ValueError, match="only corrections"):
        _ = only.decision
    released = RowRelease.model_validate({"release": ["stage"], **RELEASE, "corrections": [hold]})
    assert released.decision == (date(2026, 10, 9), RELEASE["reason"])
    # The legacy, uncited first_reported keeps loading.
    legacy = RowRelease.model_validate({"first_reported": "2026-08-23", **RELEASE})
    assert legacy.first_reported == date(2026, 8, 23)
    first = {"op": "first_report", "from": None, "as_of": "2026-08", "status": "announced"}
    for entry, message in (
        ({"corrections": [hold], **RELEASE}, "has no reason or reviewed_at"),
        ({"release": ["stage"], "reason": "r"}, "needs reviewed_at"),
        ({"as_of": "2026-10-01"}, "needs reason and reviewed_at"),
        ({"first_reported": "2026-08-23", **RELEASE, "corrections": [fix(first)]}, "not both"),
        ({"expect": {"stage": "In review"}}, "or carries corrections"),
        ({"corrections": []}, "or carries corrections"),
    ):
        path = tmp_path / "aigridwatch.json"
        path.write_text(json.dumps({"x": entry}), encoding="utf-8")
        with pytest.raises(FetchError, match=message):
            load_releases(path)


def test_the_committed_file_gives_cited_first_reports() -> None:
    """Every committed entry loads, and none uses the uncited first_reported or as_of: a new entry
    dates a first report with a cited first_report op and confirms a stage with a cited observe
    op (docs/sources/epoch-aigridwatch.md, "Reviewer entries")."""
    raw = json.loads(REPO_OVERRIDES.read_text(encoding="utf-8"))
    assert set(load_releases(REPO_OVERRIDES)) == set(raw)
    assert [pid for pid, entry in raw.items() if {"first_reported", "as_of"} & set(entry)] == []


def test_fixture_runs_lose_no_committed_entry() -> None:
    """The fixture runs read no committed entry (NO_ENTRIES in the fixture tests): every one
    carries cited corrections, read after the fixtures and their run date, and a release there
    goes with its corrections. An entry without corrections would have to run on the fixtures."""
    raw = json.loads(REPO_OVERRIDES.read_text(encoding="utf-8"))
    assert raw and [pid for pid, entry in raw.items() if not entry.get("corrections")] == []


# ---------------------------------------------------------------------------- against the input


def test_an_entry_for_a_row_the_file_does_not_have_stops_the_run(run: Run) -> None:
    with pytest.raises(FetchError, match="names 'no-such-row', which this input does not have"):
        run({"no-such-row": {"corrections": [fix({"op": "hold"})]}})


def test_the_expect_guard_reads_the_row(run: Run) -> None:
    project = row(BRIDGEFIELD)
    guard = {"stage": project["stage"], "acres": project["acres"], "size_mw": project["size_mw"]}
    assert guard == {"stage": "In review", "acres": 122, "size_mw": ""}
    op = {"op": "set", "path": "/site/acreage", "from": 122, "value": 120}
    entry = {"expect": guard, "corrections": [fix(op)]}
    assert record(run({BRIDGEFIELD: entry}), BRIDGEFIELD).site.acreage == 120
    with pytest.raises(FetchError, match=r"expect \{'stage': 'Approved'\}, but the row has"):
        run({BRIDGEFIELD: {**entry, "expect": {"stage": "Approved"}}})
    with pytest.raises(FetchError, match=r"expect names \['stag'\], which the row does not have"):
        run({BRIDGEFIELD: {**entry, "expect": {"stag": "In review"}}})


def test_a_correction_dated_after_the_run_stops_it(run: Run) -> None:
    late = fix({"op": "hold"}, retrieved_at="2026-10-09T10:00:00Z")
    with pytest.raises(FetchError, match=r"corrections\[0\]: retrieved_at is after today"):
        run({BRIDGEFIELD: {"corrections": [late]}})


# ---------------------------------------------------------------------------- held rows


def test_corrections_need_the_release_of_the_check_that_holds_the_row(
    run: Run, counties: CountyIndex
) -> None:
    """Microsoft Cheyenne Expansion's kind: AI GridWatch's Operating stage is ahead of its row
    (held, stage_ahead), and the reviewer's pages give its announcement and its approval. The
    corrections alone build nothing, so the run stops; with the stage released they replace the
    stage's observation."""
    ops = [
        {"op": "remove_event", "match": {"event": "other", "status": "operating"}},
        event("announced", "announced", "2026-04-14"),
    ]
    approval = fix(
        event("approved", "permitted", "2026-07-13"), url="https://example.com/news/county"
    )
    corrections = [fix(*ops, source_type="company_release"), approval]
    with pytest.raises(
        FetchError,
        match=f"no record was built for {CHEYENNE} \\(held: .*; conflict: AI GridWatch's stage "
        "'Operating' \\(operating\\) is ahead of its own row",
    ):
        run({CHEYENNE: {"corrections": corrections}})
    result = run({CHEYENNE: {"release": ["stage"], **RELEASE, "corrections": corrections}})
    rec = record(result, CHEYENNE)
    assert events(rec) == [
        ("announced", "announced", "2026-04-14"),
        ("approved", "permitted", "2026-07-13"),
    ]
    assert rec.status == "permitted" and rec.status_reason is None
    assert {k: v.value for k, v in rec.dates.items()} == {
        "first_reported": "2026-04-14",
        "announced": "2026-04-14",
        "approved": "2026-07-13",
    }
    meta = rec.field_meta["/status"]
    assert (meta.method, meta.source_ids) == ("human_entered", [rec.sources[-1].id])
    assert validate_record(rec, counties=counties, today=TODAY) == []
    # The release is noted, and the item says the record is corrected.
    (released,) = [i for i in items(result, CHEYENNE) if i.data.get("released") == "stage"]
    assert f"(reviewed 2026-10-09: {RELEASE['reason']}), so the row is imported" in released.reason
    corrected_by = released.data["corrected_by"]
    assert isinstance(corrected_by, str)
    assert corrected_by.endswith("(reviewed 2026-10-09): The reviewer read the cited page")
    assert result.metrics["records_corrected"] == 1 and result.metrics["corrections_ops"] == 3


def test_a_hold_of_a_row_the_importer_holds_is_fine(run: Run) -> None:
    # Sarasota County's moratorium holds the row already (owner's correction: hold it).
    pid = "sarasota-county-moratorium-fl"
    result = run({pid: {"corrections": [fix({"op": "hold"})]}})
    assert pid not in {v for c in result.candidates for v in c.match_values}
    with pytest.raises(FetchError, match=f"no record was built for {pid}"):
        run({pid: {"corrections": [fix(event("approved", "permitted", "2026-09-01"))]}})


def test_a_hold_correction_keeps_the_record_out_of_scope(run: Run) -> None:
    result = run({BRIDGEFIELD: {"corrections": [fix({"op": "hold", "kind": "unknown_status"})]}})
    rec = record(result, BRIDGEFIELD)
    assert rec.scope == "out_of_scope"
    held = [i for i in items(result, BRIDGEFIELD) if i.kind == "unknown_status"]
    assert len(held) == 1 and held[0].data["source_url"] == PAGE
    # The importer's own items about the row say it is corrected.
    own = [i for i in items(result, BRIDGEFIELD) if i.kind != "unknown_status"]
    assert own and all("corrected_by" in i.data for i in own)
    assert result.metrics["corrections_held"] == 1


# ---------------------------------------------------------------------------- first reports


def edited(tmp_path: Path, pid: str, **fields: Any) -> Path:
    """projects.json with fields of one row replaced."""
    doc = copy.deepcopy(json.loads(PROJECTS.read_text(encoding="utf-8")))
    for p in doc["projects"]:
        if p["id"] == pid:
            p.update(fields)
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def without_log(tmp_path: Path) -> Path:
    """projects.json with PW Digital Gateway's event log cut: its only milestone is then its
    withdrawal, which never dates the first report, so the row is held for it."""
    return edited(tmp_path, GATEWAY, events=[])


def test_a_cited_first_report_lifts_the_first_report_hold(
    run: Run, tmp_path: Path, counties: CountyIndex
) -> None:
    projects = without_log(tmp_path)
    held = run({}, projects)
    assert GATEWAY not in {v for c in held.candidates for v in c.match_values}
    (item,) = [i for i in items(held, GATEWAY) if i.kind == "conflict"]
    assert "its withdrawn event of 2026-04" in item.reason and "held for review" in item.reason
    first = {"op": "first_report", "from": "2026-04", "as_of": "2023-12-13", "status": "announced"}
    result = run({GATEWAY: {"corrections": [fix(first)]}}, projects)
    rec = record(result, GATEWAY)
    assert events(rec) == [
        ("withdrawn", "cancelled", "2026-04"),
        ("first_reported", "announced", "2023-12-13"),
    ]
    assert rec.dates["first_reported"].value == "2023-12-13" and rec.status == "cancelled"
    assert rec.status_reason == "litigation"  # the importer's, for the stopped status it keeps
    (lifted,) = [i for i in items(result, GATEWAY) if i.kind == "conflict"]
    assert "a cited first_report correction in config/overrides/aigridwatch.json dates the " in (
        lifted.reason
    )
    assert "corrected_by" in lifted.data
    assert validate_record(rec, counties=counties, today=TODAY) == []


def test_a_reviewer_event_never_dates_a_first_report_it_is_not(run: Run) -> None:
    """Kearney's kind (agw-map): the row has no dated milestone, and the reviewer's page gives the
    council's approval. Imported, the approval would date its first report, but the project was
    public before it was approved: the row is held, and the run stops, until a cited first report
    is given. A reviewer's announcement is a report, and dates it."""
    approval = event("approved", "permitted", "2026-09-08")
    with pytest.raises(
        FetchError,
        match=f"no record was built for {KEARNEY} \\(held: .*; conflict: the earliest dated "
        "event of the corrected record would be the approved event of 2026-09-08",
    ):
        run({KEARNEY: {"corrections": [fix(approval)]}})
    first = {"op": "first_report", "from": None, "as_of": "2026-08-18", "status": "proposed"}
    rec = record(run({KEARNEY: {"corrections": [fix(first, approval)]}}), KEARNEY)
    assert (rec.dates["first_reported"].value, rec.status) == ("2026-08-18", "permitted")
    announced = event("announced", "announced", "2026-08-01")
    rec = record(run({KEARNEY: {"corrections": [fix(announced, approval)]}}), KEARNEY)
    assert (rec.dates["first_reported"].value, rec.status) == ("2026-08-01", "permitted")


def test_a_cited_first_report_lifts_the_hold_for_a_report_later_than_the_row_shows(
    run: Run, tmp_path: Path
) -> None:
    # A hearing set for 2025-06-10 shows Project Delta public before its earliest milestone that
    # dates anything (the 2026-07-20 filing, once the later announcement is left out): held.
    projects = edited(tmp_path, "stokes-county-project-delta", hearing_date="2025-06-10")
    pid = "stokes-county-project-delta"
    assert pid not in {v for c in run({}, projects).candidates for v in c.match_values}
    first = {"op": "first_report", "from": "2026-07-20", "as_of": "2025-06", "status": "proposed"}
    rec = record(run({pid: {"corrections": [fix(first)]}}, projects), pid)
    assert rec.dates["first_reported"].model_dump() == {"value": "2025-06", "precision": "month"}


def test_a_released_milestone_still_dates_the_first_report_of_a_corrected_record(
    run: Run, tmp_path: Path
) -> None:
    # The reviewer accepts PW Digital Gateway's withdrawal as its first report (release) and
    # corrects another value: the withdrawal is AI GridWatch's own event, which the importer's
    # checks read, so the corrections do not hold the row.
    alias = {"op": "add_alias", "name": "Digital Gateway", "kind": "press_name"}
    entry = {"release": ["first_report"], **RELEASE, "corrections": [fix(alias)]}
    rec = record(run({GATEWAY: entry}, without_log(tmp_path)), GATEWAY)
    assert rec.dates["first_reported"].value == "2026-04"
    assert [a.name for a in rec.aliases] == ["Digital Gateway"]


# ---------------------------------------------------------------------------- review of round 6


WEEK2 = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
OBSERVED = {"event": "other", "status": "operating"}


def generated_on(tmp_path: Path, day: str, path: Path = FIX6) -> Path:
    """A copy of a fixture as a later file gives it: the same rows, generated on day."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["generated"] = day
    out = tmp_path / f"projects-{day}.json"
    out.write_text(json.dumps(doc), encoding="utf-8")
    return out


def merge(
    result: ImportResult, ctx: ImportContext, root: Path
) -> tuple[dict[str, int], RecordStore]:
    store = RecordStore(root / "data" / "records")
    receipt, _ = merge_import(
        IMPORTER,
        result,
        ctx,
        store=store,
        review_dir=root / "review" / "queue",
        receipts_dir=root / "data" / "imports",
    )
    return receipt.counts, store


@pytest.mark.parametrize(
    "ops",
    [
        [{"op": "remove_event", "match": OBSERVED}, event("announced", "announced", "2026-04-14")],
        [{"op": "replace_event", "match": OBSERVED, "set": {"as_of": "2026-09-01"}}],
        [{"op": "support", "event": OBSERVED}],
    ],
    ids=["remove_event", "replace_event", "support"],
)
def test_a_corrected_stage_observation_is_the_same_in_every_file(
    make_test_context: MakeContext,
    run: Run,
    tmp_repo: Path,
    tmp_path: Path,
    ops: list[dict[str, Any]],
) -> None:
    """mechanism-0 (review of fix round 6): AI GridWatch dates an undated stage with its file's
    generated day, and the first file that showed it keeps that date. A correction that removes
    the observation (Microsoft Cheyenne Expansion), redates it or cites a Fix for it summarises it
    with that date. The rebuilt observation keeps its first-seen date also when the stored record
    has it only among its lost events (corrections.lost_events), so the correction, and the
    record, are the same with a later file: the next weekly run changes nothing."""
    entries = {CHEYENNE: {"release": ["stage"], **RELEASE, "corrections": [fix(*ops)]}}
    counts, store = merge(run(entries), make_test_context(now=NOW, today=TODAY), tmp_repo)
    assert counts["new"] == counts["candidates"]
    records = store.load()
    (stored,) = [r for r in records.values() if r.external_ids["aigridwatch_id"] == [CHEYENNE]]
    later = generated_on(tmp_path, "2026-10-15")
    result = run(entries, later, records=records, now=WEEK2, today=WEEK2.date())
    again = record(result, CHEYENNE)
    assert again.status_history == stored.status_history
    assert again.field_meta == stored.field_meta and again.corrections == stored.corrections
    counts, store = merge(
        result, make_test_context(records=records, now=WEEK2, today=WEEK2.date()), tmp_repo
    )
    assert (counts["updated"], counts["unchanged"]) == (0, counts["candidates"])
    assert store.load()[stored.id].corrections == stored.corrections


@pytest.mark.parametrize(
    "op",
    [
        {"op": "remove_event", "match": {"event": "first_reported"}},
        {
            "op": "replace_event",
            "match": {"event": "first_reported"},
            "set": {"event": "announced", "as_of": "2026-05-01"},
        },
    ],
    ids=["remove_event", "replace_event"],
)
def test_a_correction_that_leaves_the_rows_withdrawal_first_holds_the_row(
    run: Run, op: dict[str, Any]
) -> None:
    """epoch-agw-0: PW Digital Gateway is built [first_reported 2023-12-13, withdrawn 2026-04],
    and without its first report the importer holds it (its withdrawal would date the first
    report: n41). A correction that removes the first report, or moves it after the withdrawal,
    leaves the row's own withdrawal as the earliest dated event of the corrected record. The
    withdrawal is AI GridWatch's, but not the event the importer's checks read as the earliest, so
    the row is held the same way, and the run stops, until a cited first_report op dates the
    first report."""
    base = record(run({}, PROJECTS), GATEWAY)
    assert events(base) == [
        ("first_reported", "announced", "2023-12-13"),
        ("withdrawn", "cancelled", "2026-04"),
    ]
    with pytest.raises(
        FetchError,
        match=f"no record was built for {GATEWAY} \\(held: conflict: "
        + re.escape(
            "the earliest dated event of the corrected record would be the withdrawn event of "
            "2026-04, AI GridWatch's own, which a correction in "
        )
        + ".*aigridwatch.json"
        + re.escape(
            " leaves first (it removes the events before it or moves them later), and the row "
            "has no first report before it"
        ),
    ):
        run({GATEWAY: {"corrections": [fix(op)]}}, PROJECTS)


def test_a_cited_first_report_lifts_the_hold_of_a_removed_first_report(run: Run) -> None:
    # epoch-agw-0: the reviewer who removes PW Digital Gateway's first report gives a cited one.
    remove = {"op": "remove_event", "match": {"event": "first_reported"}}
    first = {"op": "first_report", "from": "2023-12-13", "as_of": "2024-01", "status": "announced"}
    rec = record(run({GATEWAY: {"corrections": [fix(remove, first)]}}, PROJECTS), GATEWAY)
    assert events(rec) == [
        ("withdrawn", "cancelled", "2026-04"),
        ("first_reported", "announced", "2024-01"),
    ]
    assert rec.dates["first_reported"].value == "2024-01" and rec.status == "cancelled"
    # A correction that leaves the row's own earliest event first imports the row as before.
    alias = {"op": "add_alias", "name": "Digital Gateway", "kind": "press_name"}
    rec = record(run({GATEWAY: {"corrections": [fix(alias)]}}, PROJECTS), GATEWAY)
    assert rec.dates["first_reported"].value == "2023-12-13"
