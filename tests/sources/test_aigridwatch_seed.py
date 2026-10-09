"""AI GridWatch rows of the 2026-10-08 seed that the record checks found mapped wrongly.

tests/fixtures/aigridwatch/seed-2026-10-08.json holds those rows (README.md there says what was
cut). Each test names the bug of the third fix round it guards (scratchpad afix3-bugs.json, "agw").
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from atlas.geo.counties import CountyIndex
from atlas.net import FetchError
from atlas.schema.record import FacilityRecord
from atlas.sources.aigridwatch import (
    IMPORTER,
    AgwDate,
    first_report,
    foreign_ids,
    hearing_held,
    milestone_events,
    parse_agw_date,
    parse_locality,
    resolve_county,
    size_basis,
    withdrawal_reason,
)
from atlas.sources.base import ImportContext, ImportResult, ReviewItem
from atlas.validate import validate_record

SEED = Path(__file__).resolve().parents[1] / "fixtures" / "aigridwatch" / "seed-2026-10-08.json"
MakeContext = Callable[..., ImportContext]


def load_seed() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(SEED.read_text(encoding="utf-8"))
    return doc


def row(pid: str) -> dict[str, Any]:
    found: dict[str, Any] = next(p for p in load_seed()["projects"] if p["id"] == pid)
    return found


def run(
    make_test_context: MakeContext, *, overrides: Path | None = None, path: Path = SEED
) -> ImportResult:
    args = argparse.Namespace(without_epoch=True)
    if overrides is not None:
        args.overrides = overrides
    return IMPORTER.run(make_test_context(input_path=path), args)


def records(result: ImportResult) -> dict[str, FacilityRecord]:
    return {c.match_values[0]: c.record for c in result.candidates}


def items(result: ImportResult, pid: str, kind: str | None = None) -> list[ReviewItem]:
    return [i for i in result.review if i.external_id == pid and kind in (None, i.kind)]


def day(text: str) -> AgwDate:
    found = parse_agw_date(text)
    assert found is not None
    return found


def write_overrides(tmp_path: Path, entries: dict[str, Any]) -> Path:
    path = tmp_path / "aigridwatch-overrides.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


@pytest.fixture
def seed(make_test_context: MakeContext) -> ImportResult:
    return run(make_test_context)


def test_the_seed_rows_make_valid_records(
    seed: ImportResult, counties: CountyIndex, today: Any
) -> None:
    for rec in records(seed).values():
        assert validate_record(rec, counties=counties, today=today) == [], rec.canonical_name


# ---------------------------------------------------------------------------- n27, n68: hearings


def test_a_past_hearing_date_alone_is_not_a_hearing_held(seed: ImportResult) -> None:
    # 900 Conshohocken Road: the Aug. 17 session was "procedural matters only, with no witness
    # testimony; further testimony to be scheduled", and nothing says it took place. Dickerson:
    # the Sept 10-11 hearings were "scheduled", then continued to November.
    got = records(seed)
    consh = got["plymouth-township-conshohocken-road-pa"]
    assert "hearing_held" not in {e.event for e in consh.status_history}
    for pid, day in (
        ("plymouth-township-conshohocken-road-pa", "2026-08-17"),
        ("dickerson-atmosphere-montgomery-md", "2026-09-10"),
    ):
        (item,) = [i for i in items(seed, pid, "unknown_status") if "hearing_date" in i.data]
        assert item.data == {"hearing_date": day} and "no hearing event is imported" in item.reason
    # The metric counts imported records; Dickerson is held (its event log is ahead of its stage).
    assert seed.metrics["hearings_unconfirmed"] == 1


def test_a_past_hearing_is_held_when_a_decision_or_an_event_of_its_day_says_so(
    seed: ImportResult,
) -> None:
    got = records(seed)
    # Antelope: the Planning Commission decided on the hearing's day.
    antelope = got["antelope-data-campus-iron-county-ut"]
    assert ("hearing_held", "2026-06-04") in {
        (e.event, e.as_of.value) for e in antelope.status_history
    }
    # DC Blox: "The MDC hearing examiner held a hearing on the DC Blox petition".
    dc_blox = got["dc-blox-warren-township-in"]
    assert ("hearing_held", "2026-06-11") in {
        (e.event, e.as_of.value) for e in dc_blox.status_history
    }
    assert hearing_held(row("dc-blox-warren-township-in"), day("2026-06-11"))
    # Dickerson's own entry for Sept 10 says the hearings were scheduled.
    assert not hearing_held(row("dickerson-atmosphere-montgomery-md"), day("2026-09-10"))


# ---------------------------------------------------------------------------- n30, n34: counties


def test_a_point_outside_the_named_county_is_not_published(
    seed: ImportResult, counties: CountyIndex
) -> None:
    # EdgeCore: "Shannon Hill Regional Business Park in Louisa County", but (37.87, -78.08) is in
    # Goochland County. Shannon Hill is no Census place: the record is at Louisa County's point.
    edgecore = records(seed)["edgecore-louisa-county-va"].location
    louisa = counties.by_name("VA", "Louisa County")
    assert louisa is not None
    assert (edgecore.lat, edgecore.lon) == counties.centroid(louisa.fips)
    assert (edgecore.precision, edgecore.geocode_method) == ("county", "county_centroid")
    assert (edgecore.county_fips, edgecore.county_name) == (louisa.fips, "Louisa")
    (item,) = items(seed, "edgecore-louisa-county-va", "county_mismatch")
    assert "coordinates are not used" in item.reason and item.data["lat"] == 37.87


def test_dickerson_is_placed_in_montgomery_county_once_released(
    make_test_context: MakeContext, tmp_path: Path, counties: CountyIndex
) -> None:
    # Dickerson (39.26, -77.42) is in Frederick County; the row says Montgomery County.
    overrides = write_overrides(
        tmp_path,
        {
            "dickerson-atmosphere-montgomery-md": {
                "release": ["stage"],
                "reason": "test: the stage is taken as it stands",
                "reviewed_at": "2026-10-09",
            }
        },
    )
    rec = records(run(make_test_context, overrides=overrides))["dickerson-atmosphere-montgomery-md"]
    montgomery = counties.by_name("MD", "Montgomery County")
    assert montgomery is not None
    assert (rec.location.county_fips, rec.location.precision) == (montgomery.fips, "county")
    assert counties.contains(montgomery.fips, rec.location.lat or 0, rec.location.lon or 0)


def test_two_named_counties_near_their_border_give_the_one_the_point_is_in(
    counties: CountyIndex, make_test_context: MakeContext, tmp_path: Path
) -> None:
    # PECO's Limerick project, "(Chester / Montgomery County)" at (40.230703, -75.593175): the
    # Schuylkill is the border, both counties contain the point within the validate tolerance, and
    # Census puts it in Lower Pottsgrove, Montgomery County.
    peco = row("zediker-station-data-center-south-strabane-township-pa")
    loc = parse_locality(peco["locality"], "PA")
    assert loc.county_texts == ("Chester County", "Montgomery County")
    county = resolve_county(loc, "PA", counties, (peco["lat"], peco["lon"]))
    assert county is not None and county.name == "Montgomery"
    overrides = write_overrides(
        tmp_path,
        {
            "zediker-station-data-center-south-strabane-township-pa": {
                "release": ["id"],
                "reason": "test",
                "reviewed_at": "2026-10-09",
            }
        },
    )
    result = run(make_test_context, overrides=overrides)
    rec = records(result)["zediker-station-data-center-south-strabane-township-pa"]
    assert rec.location.county_name == "Montgomery"
    assert not items(
        result, "zediker-station-data-center-south-strabane-township-pa", "county_mismatch"
    )


# ---------------------------------------------------------------------------- n31, n56, n66: MW


def test_size_mw_is_kept_as_stated_and_mapped_only_by_a_stated_basis(seed: ImportResult) -> None:
    got = records(seed)
    # "a 1.1+ GW campus", "120 MW / $3B campus", "24 MW data center": no basis is stated.
    for pid, stated in (
        ("edgecore-louisa-county-va", "AI GridWatch size_mw: 1100"),
        ("midtown-armory-st-louis-mo", "AI GridWatch size_mw: 120"),
        ("deep-green-lansing-mi", "AI GridWatch size_mw: 24"),
    ):
        cap = got[pid].capacity
        assert cap.mw_as_stated == stated, pid
        assert (cap.it_mw, cap.facility_mw, cap.utility_request_mw) == (None, None, None), pid
        assert "/capacity/it_mw" not in got[pid].field_meta


@pytest.mark.parametrize(
    ("size", "note", "basis"),
    [
        (300, "8-building campus (400MW substation, up to 300MW critical IT)", "it"),
        (
            401,
            "reusing the smelter's existing 482 MW grid connection for 401 MW of critical IT load",
            "it",
        ),
        (
            3200,
            "$20B OpenAI campus on 1,400 acres, with 3.2 GW contracted from Georgia Power.",
            "utility_request",
        ),
        (960, "where majority owner Talen Energy agreed to supply 960 MW.", "utility_request"),
        (
            450,
            "$3B facility seeking 450 MW (Phase 1 up to 100 MW) at Tyger River.",
            "utility_request",
        ),
        (835, "Microsoft will buy 100% of the 835MW nuclear output for 20 years", "power_source"),
        (450, "an attached behind-the-meter 450 MW gas-fired power plant", "power_source"),
        (
            800,
            "Billed as 10GW at full buildout (phase 1 is 800MW), paired with a gas plant.",
            "phase",
        ),
        (800, "3GW net-zero AI campus. Phase I: 390 acres/800MW across 8 buildings", "phase"),
        (120, "120 MW / $3B campus near the Armory", None),
        (300, "New campus scalable to 300MW with on-site substation", None),
        (336, "96MW facility (VA11) as first phase of a planned 336MW campus", None),
        (279, "Chip types: TPU v5e. Epoch AI estimates ~279 MW.", None),
        (500, "a 1,000 MW campus", None),
    ],
)
def test_size_basis(size: float, note: str, basis: str | None) -> None:
    assert size_basis(size, note) == basis


def test_a_power_supply_deal_is_out_of_scope_and_its_mw_is_not_the_data_centers(
    seed: ImportResult,
) -> None:
    # "$1.6B power purchase agreement to restart TMI Unit 1 ...; Microsoft will buy 100% of the
    # 835MW nuclear output": not a data center site (07 §2.2), and 835 MW is a reactor's output.
    pid = "microsoft-three-mile-island-pa"
    (scope,) = items(seed, pid, "out_of_scope")
    assert scope.data == {"phrase": "power purchase agreement"}
    (unit,) = items(seed, pid, "unit_parse")
    assert unit.data["basis"] == "power_source"
    # Piketon: "phase 1 is 800MW" of a 10 GW campus.
    (phase,) = items(seed, "softbank-ports-piketon-oh", "unit_parse")
    assert phase.data["basis"] == "phase"


# ---------------------------------------------------------------------------- n33: shifted ids


def test_a_row_under_another_rows_id_is_held(seed: ImportResult) -> None:
    shifted = {
        "zediker-station-data-center-south-strabane-township-pa": "PECO - Limerick Distribution Project",
        "highlands-west-data-center-lower-swatara-swatara-townships-pa": "Zediker Station Data Center",
        "highlands-east-data-center-lower-swatara-swatara-townships-pa": "Highlands West Data Center",
        "londonderry-data-center-londonderry-township-pa": "Highlands East Data Center",
        "fezzik-energy-unnamed-data-center-midland-borough-pa": "Londonderry Data Center",
        "amazon-web-services-unnamed-data-center-center-township-pa": "Unnamed Data Center",
    }
    assert {pid: row(pid)["name"] for pid in shifted} == shifted
    assert set(foreign_ids(load_seed()["projects"])) == set(shifted)
    got = records(seed)
    for pid in shifted:
        assert pid not in got
        (item,) = items(seed, pid, "conflict")
        assert item.data["id_of"] != pid and "held for review" in item.reason
    # The real AWS Center Township row ("-52") keeps its id, and its record.
    assert "amazon-web-services-unnamed-data-center-center-township-pa-52" in got


# ---------------------------------------------------------------------------- n35, n76: months


def test_a_date_on_the_first_is_a_month(seed: ImportResult) -> None:
    got = records(seed)
    # EdgeCore announced "in June 2025" (the release is dated June 25): announced 2025-06-01.
    edgecore = got["edgecore-louisa-county-va"]
    assert (edgecore.dates["announced"].value, edgecore.dates["announced"].precision) == (
        "2025-06",
        "month",
    )
    # King of Prussia "filed in early March 2026": rezoning_filed 2026-03-01.
    kop = got["king-of-prussia-mlp-pa"]
    assert kop.dates["application_filed"].model_dump() == {"value": "2026-03", "precision": "month"}
    assert (day("2026-03-02").precision, day("2026-03-01").precision) == ("day", "month")


# ---------------------------------------------------------------------------- n55, n76: stages


def test_a_stage_behind_the_rows_own_event_log_is_held(seed: ImportResult) -> None:
    got = records(seed)
    reported = {
        # "Person County commissioners voted to approve the rezoning" (2026-08-05), stage In review.
        "microsoft-person-county-nc": ("permitted", "2026-08-05"),
        # "held a groundbreaking ceremony for the PORTS Technology Campus" (2026-03-20).
        "softbank-ports-piketon-oh": ("under_construction", "2026-03-20"),
        # Project Taurus: rezoning_filed 2026-06-01 is the June approval ("administratively
        # approved the Development Plan Modification on June 11, 2026").
        "project-taurus-colorado-springs-co": ("permitted", "2026-06-11"),
        # "voted 5-2 to deny the rezoning request", stage still Awaiting decision.
        "project-iron-spur-rural-hall-nc": ("denied", "2026-07-30"),
    }
    for pid, (status, day) in reported.items():
        assert pid not in got, pid
        (item,) = [i for i in items(seed, pid, "conflict") if "reported_status" in i.data]
        assert (item.data["reported_status"], item.data["event_date"]) == (status, day), pid
        assert "held for review" in item.reason


def test_a_stage_without_as_of_is_not_dated_with_the_file(seed: ImportResult) -> None:
    # Piketon and TMI have no as_of, and no milestone reaches their stage: before, the stage was
    # dated 2026-10-08 (the file's date) and published as "as of 2026-10-08". (Person County, also
    # without as_of, is first reported by its 2026-08-05 rezoning entry, which reaches the stage's
    # status; its event log holds it all the same.)
    for pid in ("softbank-ports-piketon-oh", "microsoft-three-mile-island-pa"):
        assert row(pid)["as_of"] == ""
        (item,) = [i for i in items(seed, pid, "unknown_status") if "stage" in i.data]
        assert "no as_of" in item.reason
        assert pid not in records(seed)
    # Every stage event that is published is dated by the row's own as_of.
    for pid, rec in records(seed).items():
        for e in rec.status_history:
            if e.event == "other":
                assert (
                    e.note == f"AI GridWatch stage '{row(pid)['stage']}' as of {row(pid)['as_of']}"
                )


def test_a_reviewer_can_date_a_stage_without_as_of(
    make_test_context: MakeContext, tmp_path: Path
) -> None:
    pid = "microsoft-three-mile-island-pa"
    overrides = write_overrides(
        tmp_path,
        {
            pid: {
                "release": ["scope"],
                "as_of": "2026-10-09",
                "reason": "test",
                "reviewed_at": "2026-10-09",
            }
        },
    )
    result = run(make_test_context, overrides=overrides)
    rec = records(result)[pid]
    other = rec.status_history[-1]
    assert (other.event, other.as_of.value) == ("other", "2026-10-09")
    assert other.note is not None and "confirmed by a reviewer on 2026-10-09" in other.note
    assert rec.scope == "in_scope"
    (note,) = items(result, pid, "out_of_scope")
    assert note.data["released"] == "scope" and "released by" in note.reason


# ---------------------------------------------------------------------------- n67: first report


def test_first_reported_comes_from_the_earliest_entry_not_the_decision(seed: ImportResult) -> None:
    got = records(seed)
    # Deep Green: only a decision date (the 2026-04-06 withdrawal); the log starts with the
    # 2026-03-04 Planning Commission vote.
    deep_green = got["deep-green-lansing-mi"]
    assert deep_green.dates["first_reported"].value == "2026-03-04"
    assert deep_green.status_history[0].event == "first_reported"
    assert deep_green.status_history[0].status == "announced"
    # 900 Conshohocken Road: the log starts on 2025-10-01 (a month, like every AI GridWatch date
    # on the 1st), not with the 2026-08-17 hearing.
    consh = got["plymouth-township-conshohocken-road-pa"]
    assert consh.dates["first_reported"].model_dump() == {"value": "2025-10", "precision": "month"}
    # A row with an announced date is first reported then, whatever its log starts with (Louisa
    # County bought EdgeCore's business park in 2019).
    assert got["edgecore-louisa-county-va"].dates["first_reported"].value == "2025-06"


def test_site_history_in_the_event_log_does_not_date_the_first_report() -> None:
    # Logs start with the land's history or the place's rules (Louisa County's 2019 park
    # purchase, a township's data center ordinance, the applicant LLC's registration): only an
    # entry that reports on the data center dates the first report.
    today = date(2026, 10, 12)
    project = {
        "name": "Example Campus",
        "decided_date": "2026-04-06",
        "outcome": "withdrawn",
        "events": [
            {
                "date": "2019-02-19",
                "kind": "vote",
                "summary": "Supervisors voted to buy 700 acres.",
            },
            {
                "date": "2025-10-22",
                "kind": "vote",
                "summary": "The township passed its data center ordinance.",
            },
            {
                "date": "2025-12-15",
                "kind": "filing",
                "summary": "The applicant LLC was registered.",
            },
            {
                "date": "2026-03-04",
                "kind": "vote",
                "summary": "The board recommended the data center rezoning.",
            },
        ],
    }
    events = milestone_events(project, today)
    first = first_report(project, "cancelled", events, today)
    assert first is not None and first["as_of"] == {"value": "2026-03-04", "precision": "day"}
    assert (first["status"], first["event"]) == ("announced", "first_reported")
    # Built or being built: an early entry may describe the site as it stands.
    assert first_report(project, "operating", events, today) is None


# ---------------------------------------------------------------------------- n73: withdrawals


def test_withdrawn_is_the_developers_only_when_the_row_says_so(seed: ImportResult) -> None:
    got = records(seed)
    # "Deep Green withdrew its rezoning request".
    assert got["deep-green-lansing-mi"].status_reason == "developer_withdrawal"
    # "Karis notified the village on July 1 that it would withdraw the rezoning request".
    assert got["hoffman-estates-karis-plum-farms"].status_reason == "developer_withdrawal"
    # The mayor "would not pursue or support data center development"; no developer was named.
    riverjump = got["project-riverjump-marion-in"]
    assert (riverjump.status, riverjump.status_reason) == ("cancelled", None)
    assert (
        withdrawal_reason(
            {"note": "host-fee agreement lapsed after nearly 3 years of local opposition"}
        )
        is None
    )
    assert (
        withdrawal_reason(
            {"note": "the rezoning was judicially voided over defective hearing notice"}
        )
        == "litigation"
    )


# ---------------------------------------------------------------------------- NEW-2: releases


def test_the_overrides_file_is_checked(make_test_context: MakeContext, tmp_path: Path) -> None:
    for entries in (
        {"x": {"release": ["everything"], "reason": "r", "reviewed_at": "2026-10-09"}},
        {"x": {"release": [], "reason": "r", "reviewed_at": "2026-10-09"}},
        {"x": {"release": ["epoch"], "reviewed_at": "2026-10-09"}},
        {"x": {"release": ["epoch"], "reason": "r", "reviewed_at": "2026-10-09", "extra": 1}},
        {"x": {"release": ["epoch"], "reason": "mail a@example.com", "reviewed_at": "2026-10-09"}},
    ):
        with pytest.raises(FetchError, match="overrides"):
            run(make_test_context, overrides=write_overrides(tmp_path, entries))
    with pytest.raises(FetchError, match="cannot read"):
        run(make_test_context, overrides=tmp_path / "missing.json")
    result = run(
        make_test_context,
        overrides=write_overrides(
            tmp_path,
            {"no-such-row": {"release": ["id"], "reason": "r", "reviewed_at": "2026-10-09"}},
        ),
    )
    assert (result.metrics["overrides_used"], result.metrics["overrides_unused"]) == (0, 1)
