"""The cited corrections and guards of config/overrides/epoch.json (fix round 6; docs/corrections.md,
docs/sources/epoch-aigridwatch.md "Overrides policy"): corrections applied to the campus record
right after it is built, expect, entries the input does not have, a first report given as a part
or as an op, year and quarter first reports, the shared confidence table of the legacy parts,
and the citation of every committed Fix (mechanism §11.6). The sites are the fixture ZIPs'; the
corrections are the reviewers' kind (Amazon Ridgeland's operating row that is not, Meta Aiken's
codename), cited to an example page. tests/sources/test_corrections.py tests the shared ops
themselves."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from atlas.geo.counties import CountyIndex
from atlas.net import FetchError
from atlas.schema.record import FacilityRecord
from atlas.schema.rollup import period_start
from atlas.sources.base import ImportContext, ImportResult, ReviewItem
from atlas.sources.corrections import (
    AddAlias,
    AddEvent,
    AddParty,
    FirstReportOp,
    Fix,
    Observe,
    Rename,
    RenameParty,
    ReplaceEvent,
    SetLocation,
    SetParty,
    SetValue,
    fuzzy_date,
)
from atlas.sources.epoch import IMPORTER, SiteOverride, clean_text, load_overrides, read_zip
from atlas.text import find_personal_data
from atlas.validate import validate_record

MakeContext = Callable[..., ImportContext]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "epoch"
EPOCH_ZIP = FIXTURES / "data_centers.zip"
CASES_ZIP = FIXTURES / "cases.zip"
REPO_OVERRIDES = Path(__file__).resolve().parents[2] / "config" / "overrides" / "epoch.json"
# The committed entries without their corrections and expect guards (fix round 6), which were read
# and reviewed against today's sources, after these fixture rows and their run date (an entry with
# only corrections is left out); the tests below give each site the corrections they test.
COMMITTED: dict[str, Any] = {
    name: parts
    for name, entry in json.loads(REPO_OVERRIDES.read_text(encoding="utf-8")).items()
    if (parts := {k: v for k, v in entry.items() if k not in ("corrections", "expect")})
}
MADISON = "Amazon Madison Mega Site"
MILAM = "OpenAI Stargate Milam"
HYPERION = "Meta Hyperion"
PAGE = "https://example.com/news/site"


def fix(*ops: dict[str, Any], url: str = PAGE, **extra: Any) -> dict[str, Any]:
    """One Fix as the overrides file writes it."""
    return {
        "source_url": url,
        "publisher": "Example News",
        "source_type": "news",
        "quote": "A sentence that states the correction.",
        "basis": "upstream_error",
        "reason": "The reviewer read the cited page",
        "retrieved_at": "2026-10-10T12:00:00Z",
        "reviewed_at": "2026-10-10",
        "ops": list(ops),
        **extra,
    }


def committed(*names: str) -> dict[str, Any]:
    """Copies of the committed entries for these sites."""
    return {n: json.loads(json.dumps(COMMITTED[n])) for n in names}


@pytest.fixture
def run(make_test_context: MakeContext, tmp_path: Path) -> Callable[..., ImportResult]:
    """The importer on a fixture ZIP, without geocoding, with the entries as its overrides file."""

    def go(entries: dict[str, Any], zip_path: Path = EPOCH_ZIP, **ctx: Any) -> ImportResult:
        path = tmp_path / "epoch.json"
        path.write_text(json.dumps(entries), encoding="utf-8")
        args = argparse.Namespace(overrides=path, no_geocode=True)
        return IMPORTER.run(make_test_context(input_path=zip_path, **ctx), args)

    return go


Run = Callable[..., ImportResult]


def record(result: ImportResult, name: str) -> FacilityRecord:
    (found,) = [c.record for c in result.candidates if name in c.match_values]
    return found


def items(result: ImportResult, name: str) -> list[ReviewItem]:
    return [i for i in result.review if i.external_id == name]


def csv_row(name: str, zip_path: Path = EPOCH_ZIP) -> dict[str, str]:
    (found,) = [r for r in read_zip(zip_path)[1] if clean_text(r["Name"]) == name]
    return found


# ---------------------------------------------------------------------------- the entry model


def test_an_entry_with_only_corrections_loads(tmp_path: Path) -> None:
    path = tmp_path / "epoch.json"
    op = {"op": "add_alias", "name": "Project Sabal", "kind": "codename"}
    path.write_text(json.dumps({"Meta Aiken": {"corrections": [fix(op)]}}), encoding="utf-8")
    (entry,) = load_overrides(path).values()
    assert entry.location is None and entry.citations() == [] and len(entry.corrections) == 1
    # expect alone is no entry: it only guards what a part or a correction changes.
    path.write_text(json.dumps({"Meta Aiken": {"expect": {"Owner": "Meta"}}}), encoding="utf-8")
    with pytest.raises(FetchError, match=r"needs a location, .* milestones or corrections"):
        load_overrides(path)


def test_a_first_report_part_and_a_first_report_op_exclude_each_other(tmp_path: Path) -> None:
    entry = committed(MILAM)[MILAM]
    entry["first_report"] = {
        **{k: entry[k] for k in ("source_url", "quote", "retrieved_at", "note")},
        "published_at": "2025-09-23",
        "status": "announced",
    }
    op = {"op": "first_report", "from": "2025-09-27", "as_of": "2025-09-23", "status": "announced"}
    entry["corrections"] = [fix(op)]
    path = tmp_path / "epoch.json"
    path.write_text(json.dumps({MILAM: entry}), encoding="utf-8")
    with pytest.raises(FetchError, match=r"a first_report part or a first_report op .*, not both"):
        load_overrides(path)


@pytest.mark.parametrize(
    ("published_at", "as_of"),
    [
        ("2024", {"value": "2024", "precision": "year"}),  # Meta Rosemount: "last year"
        ("2025-Q2", {"value": "2025-Q2", "precision": "quarter"}),
        ("2025-06", {"value": "2025-06", "precision": "month"}),
    ],
)
def test_a_first_report_dated_to_a_year_or_a_quarter(
    run: Run, published_at: str, as_of: dict[str, str], counties: CountyIndex, today: date
) -> None:
    entries = committed(MILAM)
    entry = entries[MILAM]
    entry["first_report"] = {
        **{k: entry[k] for k in ("source_url", "quote", "retrieved_at", "note")},
        "published_at": published_at,
        "status": "announced",
    }
    rec = record(run(entries), MILAM)
    assert rec.dates["first_reported"].model_dump() == as_of
    first = rec.status_history[0]
    assert (first.event, first.as_of.model_dump()) == ("first_reported", as_of)
    assert validate_record(rec, counties=counties, today=today) == []


@pytest.mark.parametrize("published_at", [2024, "1700000000"])
def test_a_first_report_date_is_never_a_number(published_at: object) -> None:
    # A JSON number (or a string of digits) would read as a Unix time: 2024 is 1970-01-01.
    entry = committed(MILAM)[MILAM]
    first_report = {
        **{k: entry[k] for k in ("source_url", "quote", "retrieved_at", "note")},
        "published_at": published_at,
        "status": "announced",
    }
    with pytest.raises(ValueError, match="not a number"):
        SiteOverride.model_validate({**entry, "first_report": first_report})


# ---------------------------------------------------------------------------- against the input


def test_an_entry_for_a_site_the_input_does_not_have_stops_the_run(run: Run) -> None:
    for entry in ({"corrections": [fix({"op": "hold"})]}, COMMITTED["Meta Aiken"]):
        with pytest.raises(FetchError, match="names 'Meta Aiken', which this input does not have"):
            run({**committed(MILAM), "Meta Aiken": entry})


def test_the_expect_guard_reads_the_sites_csv_row(run: Run) -> None:
    owner = clean_text(csv_row(MADISON)["Owner"])
    assert owner == "Amazon #confident"  # the cell as the CSV has it, tags and all
    guard = {"Owner": owner, "Investors": None}  # an empty cell reads as null
    op = {"op": "add_alias", "name": "Madison campus", "kind": "press_name"}
    entry = {"expect": guard, "corrections": [fix(op)]}
    assert [a.name for a in record(run({MADISON: entry}), MADISON).aliases] == ["Madison campus"]
    with pytest.raises(
        FetchError, match=r"expect \{'Owner': 'Microsoft'\}, but the site's CSV row"
    ):
        run({MADISON: {**entry, "expect": {"Owner": "Microsoft"}}})
    with pytest.raises(FetchError, match=r"expect names \['Ownr'\], which the site's CSV row"):
        run({MADISON: {**entry, "expect": {"Ownr": owner}}})


def test_a_correction_dated_after_the_run_stops_it(run: Run) -> None:
    late = fix({"op": "hold"}, retrieved_at="2026-10-12T12:30:00Z", reviewed_at="2026-10-12")
    with pytest.raises(FetchError, match=r"corrections\[0\]: retrieved_at is after today"):
        run({MADISON: {"corrections": [late]}})


@pytest.mark.parametrize(
    ("name", "held"),
    [
        ("Colossus 2", "held: geocode_failed: no Census step"),  # no place, so no record
        ("CoreWeave Norway", "held: no review item names it"),  # outside the US
    ],
)
def test_a_correction_for_a_site_with_no_record_stops_the_run(
    run: Run, name: str, held: str
) -> None:
    op = {"op": "add_alias", "name": "Example", "kind": "press_name"}
    with pytest.raises(FetchError, match=f"no record was built for {name} \\({held}"):
        run({name: {"corrections": [fix(op)]}})
    # A hold of a site the importer did not build is fine.
    held_only = run({name: {"corrections": [fix({"op": "hold"})]}})
    assert name not in {v for c in held_only.candidates for v in c.match_values}


# ---------------------------------------------------------------------------- the corrected record


def test_a_correction_removes_an_operating_row_that_is_not(
    run: Run, counties: CountyIndex, today: date
) -> None:
    """Amazon Ridgeland's kind: Epoch's row says the buildings operate, a cited page says they
    are under construction. The energized event goes (into field_meta conflicts), the record is
    a project again (the record_type hook), and Epoch's modelled capacity is withheld."""
    plain = record(run({}), MADISON)
    assert (plain.status, plain.record_type) == ("operating", "campus")
    ops: list[dict[str, Any]] = [
        {"op": "remove_event", "match": {"event": "energized", "as_of": "2025-06-23"}},
        {"op": "set", "path": "/capacity/it_mw", "from": 284, "value": None},
        {"op": "set", "path": "/capacity/facility_mw", "from": 341, "value": None},
    ]
    result = run({MADISON: {"corrections": [fix(*ops, states_status="under_construction")]}})
    rec = record(result, MADISON)
    assert (rec.status, rec.record_type) == ("under_construction", "project")
    assert "operating_since" not in rec.dates and rec.capacity.it_mw is None
    assert [e.event for e in rec.status_history] == ["construction_start"]
    (lost,) = rec.field_meta["/status_history"].conflicts
    assert lost["value"] == {
        "event": "energized",
        "status": "operating",
        "as_of": "2025-06-23",
        "phase_id": None,
        "planned": False,
    }
    assert lost["source_ids"] == ["s1"]
    s1 = rec.sources[0]
    # Epoch's s1 still backs the event it gave, and no longer the capacity it modelled.
    assert "/status_history" in s1.supports and not any("/capacity" in p for p in s1.supports)
    assert [c.pointer for c in rec.corrections] == [
        "/status_history",
        "/capacity/it_mw",
        "/capacity/facility_mw",
    ]
    cited = rec.sources[-1]
    assert (str(cited.url), cited.quote_match) == (PAGE, "human")
    assert rec.field_meta["/capacity/it_mw"].method == "human_entered"
    assert validate_record(rec, counties=counties, today=today) == []
    # The page states the record's status: no conflict item; the receipt counts the correction.
    assert not [i for i in items(result, MADISON) if "states the facility" in i.reason]
    assert (result.metrics["corrections_entries"], result.metrics["corrections_ops"]) == (1, 3)
    assert result.metrics["records_corrected"] == 1
    assert (result.metrics["overrides_used"], result.metrics["overrides_unused"]) == (1, 0)


def test_a_cited_first_report_correction_clears_the_first_report_doubt(run: Run) -> None:
    # Milam's first_reported is Epoch's first observation, and its TDLR registration may date
    # an earlier report: an unknown_status item, which a cited first report clears.
    (doubt,) = items(run(committed(MILAM)), MILAM)
    assert doubt.kind == "unknown_status" and "first observation" in doubt.reason
    entries = committed(MILAM)
    op = {"op": "first_report", "from": "2025-09-27", "as_of": "2025-09-23", "status": "announced"}
    entries[MILAM]["corrections"] = [fix(op)]
    result = run(entries)
    assert items(result, MILAM) == []
    rec = record(result, MILAM)
    assert rec.dates["first_reported"].value == "2025-09-23"
    first = rec.status_history[-1]
    assert (first.event, first.status, first.source_ids) == ("first_reported", "announced", ["s5"])
    assert rec.dates["construction_start"].value == "2025-09-27"  # Epoch's row stays the start


def test_the_items_about_a_corrected_record_say_so(run: Run, tmp_path: Path) -> None:
    entries = committed(MILAM)
    op = {"op": "add_alias", "name": "Stargate Milam County", "kind": "press_name"}
    entries[MILAM]["corrections"] = [fix(op, reason="The developer's page names the campus")]
    (doubt,) = items(run(entries), MILAM)
    assert doubt.kind == "unknown_status"  # the correction does not answer it, and says so
    assert doubt.data["corrected_by"] == (
        f"{tmp_path / 'epoch.json'} (reviewed 2026-10-10): The developer's page names the campus"
    )


def test_a_cited_page_that_states_another_status_is_a_conflict(run: Run) -> None:
    op = {"op": "support", "event": {"event": "construction_start"}}
    entries = committed(HYPERION)
    entries[HYPERION]["corrections"] = [fix(op, states_status="operating")]
    result = run(entries)
    rec = record(result, HYPERION)
    assert rec.status == "under_construction"
    (item,) = [i for i in items(result, HYPERION) if "states the facility" in i.reason]
    assert item.kind == "conflict" and item.data["source_url"] == PAGE
    assert item.data["states_status"] == "operating"


def test_a_hold_keeps_the_record_out_of_scope_with_an_item(run: Run) -> None:
    result = run({MADISON: {"corrections": [fix({"op": "hold", "kind": "unknown_status"})]}})
    rec = record(result, MADISON)
    assert rec.scope == "out_of_scope"
    assert [(c.pointer, c.old, c.new) for c in rec.corrections] == [
        ("/scope", "in_scope", "out_of_scope")
    ]
    (item,) = items(result, MADISON)
    assert item.kind == "unknown_status" and item.data["source_url"] == PAGE
    assert "held by" in item.reason and result.metrics["corrections_held"] == 1


def test_a_shared_campus_takes_its_sites_corrections_in_csv_order(run: Run) -> None:
    # OpenAI Stargate Abilene comes before Crusoe Abilene Expansion in the CSV (and after it by
    # name): the second rename's from is the first's base.
    names = {clean_text(r["Name"]) for r in read_zip(CASES_ZIP)[1]}
    entries = committed(*(n for n in COMMITTED if n in names))
    first = {"op": "rename", "from": "OpenAI Stargate Abilene", "base": "Stargate Abilene"}
    second = {"op": "rename", "from": "Stargate Abilene", "base": "Stargate Abilene Campus"}
    entries["OpenAI Stargate Abilene"]["corrections"] = [fix(first)]
    entries["Crusoe Abilene Expansion"] = {"corrections": [fix(second, url=PAGE + "/2")]}
    rec = record(run(entries, CASES_ZIP), "Crusoe Abilene Expansion")
    assert rec.external_ids["epoch_name"] == ["OpenAI Stargate Abilene", "Crusoe Abilene Expansion"]
    assert rec.canonical_name == "Stargate Abilene Campus (Taylor County, TX)"
    assert [c.new for c in rec.corrections] == [
        "Stargate Abilene (Taylor County, TX)",
        "Stargate Abilene Campus (Taylor County, TX)",
    ]


def test_a_corrected_place_follows_the_override_rules(
    run: Run, counties: CountyIndex, today: date
) -> None:
    # A city that is also the postal city of Epoch's address (Holly Ridge, LA) needs place_check,
    # as a location part does.
    entries = committed(HYPERION)
    move = {
        "op": "set_location",
        "from": {"county_fips": "22083"},
        "state_abbr": "LA",
        "city": "Holly Ridge",
        "precision": "locality",
    }
    entries[HYPERION]["corrections"] = [fix(move)]
    with pytest.raises(FetchError, match="'Holly Ridge' is also the postal city"):
        run(entries)
    # A township is placed at its county subdivision's point: the import loads that Gazetteer
    # (the test's sample has Salem Township, Luzerne County, PA), and the name follows the place.
    township = {
        "op": "set_location",
        "from": {"state_abbr": "MS"},
        "state_abbr": "PA",
        "county_fips": "42079",
        "municipality": "Salem Township",
        "precision": "locality",
    }
    rec = record(run({MADISON: {"corrections": [fix(township)]}}), MADISON)
    assert rec.canonical_name == "Amazon Madison Mega Site (Salem Township, PA)"
    assert (rec.location.county_fips, rec.location.geocode_method) == ("42079", "gazetteer")
    assert validate_record(rec, counties=counties, today=today) == []


# ---------------------------------------------------------------------------- legacy parts


def test_a_legacy_part_takes_the_shared_source_adjustment(run: Run) -> None:
    # 07 §3.4: stated with a verbatim quote, 0.85, plus the source adjustment, now the shared
    # table's: a commercial aggregator's place is 0.75, a news report's 0.85.
    entries = committed(MILAM)
    assert record(run(entries), MILAM).field_meta["/location"].confidence == 0.90  # company
    entries[MILAM]["source_type"] = "commercial_aggregator"
    assert record(run(entries), MILAM).field_meta["/location"].confidence == 0.75


# ---------------------------------------------------------------------------- the committed file

# The year of a committed correction's date is in its quote, its published_at or its URL, except
# where the page gives the date another way; each exception says how, and stays only while needed.
YEAR_ELSEWHERE = {
    ("Google Mesa", 0): "GovTech's byline dates the story June 28, 2019, with a time in no zone",
    ("Meta Rosemount", 0): "the page of 2023-09-02 dates Xcel's first petition to 'last year'",
}
FIX_KEYS = (
    "source_url",
    "publisher",
    "source_type",
    "quote",
    "retrieved_at",
    "basis",
    "reason",
    "reviewed_at",
    "issue_url",
)
REVIEWS = "https://github.com/moonspells/gigawatt-atlas/"
WAYBACK = "https://web.archive.org/web/"
OSM_ELEMENT = ("https://www.openstreetmap.org/way/", "https://www.openstreetmap.org/node/")


def written(fix_: Fix, counties: CountyIndex) -> tuple[list[str], list[str]]:
    """What a Fix's ops write into the record, as text: the values its quote must state, and the
    places a cited OpenStreetMap element gives by its geometry (its reason names them)."""
    stated: list[str] = []
    places: list[str] = []
    for op in fix_.ops:
        if isinstance(op, SetValue) and op.value is not None:
            value = op.value
            stated.append(f"{value:g}" if isinstance(value, int | float) else str(value))
        elif isinstance(op, Rename):
            stated.append(op.base)
        elif isinstance(op, AddAlias | AddParty):
            stated.append(op.name)
        elif isinstance(op, SetParty):
            stated += op.names
        elif isinstance(op, RenameParty):
            stated.append(op.to)
        elif isinstance(op, SetLocation):
            named = [p for p in (op.city, op.municipality) if p is not None]
            county = counties.get(op.county_fips) if op.county_fips is not None else None
            if county is not None:
                named.append(f"{county.name} County")
            from_geometry = str(fix_.source_url).startswith(OSM_ELEMENT)
            (places if from_geometry else stated).extend(named)
    return stated, places


def dated(fix_: Fix) -> list[str]:
    """The dates of the events a Fix's ops add or redate, other than planned ones."""
    out: list[str] = []
    for op in fix_.ops:
        if (isinstance(op, AddEvent) and not op.planned) or isinstance(op, Observe | FirstReportOp):
            out.append(op.as_of)
        elif isinstance(op, ReplaceEvent) and op.set.as_of is not None and not op.set.planned:
            out.append(op.set.as_of)
    return out


def test_every_committed_correction_is_cited(counties: CountyIndex) -> None:
    """Each Fix of config/overrides/epoch.json (mechanism §11.6): its citation fields, a verbatim
    quote of at most 300 characters read before now, a decision on or after that day and no
    later than today, the review it came from, no personal data, a Wayback snapshot of the page
    when it refused the project's client. The values it writes are in its quote (a county that a
    cited OpenStreetMap element's outline gives is named in its reason), and every date it gives
    is no later than today, its year in the quote, the page's date or the URL. The file is
    canonical JSON."""
    text = REPO_OVERRIDES.read_text(encoding="utf-8")
    raw = json.loads(text)
    assert text == json.dumps(raw, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    now, today = datetime.now(UTC), date.today()
    elsewhere: set[tuple[str, int]] = set()
    fixes = 0
    for name, entry in load_overrides(REPO_OVERRIDES).items():
        for i, fix_ in enumerate(entry.corrections):
            fixes += 1
            where = (name, i)
            for key in FIX_KEYS:
                assert raw[name]["corrections"][i].get(key), (where, key)
            assert 0 < len(fix_.quote) <= 300 and fix_.retrieved_at <= now, where
            assert fix_.retrieved_at.astimezone(UTC).date() <= fix_.reviewed_at <= today, where
            assert str(fix_.issue_url).startswith(REVIEWS), where
            assert find_personal_data(fix_.quote) == find_personal_data(fix_.reason) == [], where
            if fix_.archive_url is not None:
                assert str(fix_.archive_url).startswith(WAYBACK), where
                assert str(fix_.archive_url).endswith(str(fix_.source_url)), where
            stated, places = written(fix_, counties)
            for value in stated:
                assert value in fix_.quote, (where, value)
            for place in places:
                assert place in fix_.reason, (where, place)
            for as_of in dated(fix_):
                assert period_start(fuzzy_date(as_of)) <= today, (where, as_of)
                year = as_of[:4]
                published = fix_.published_at is not None and fix_.published_at.year == int(year)
                found = year in fix_.quote or year in str(fix_.source_url) or published
                if where in YEAR_ELSEWHERE:
                    assert not found, (where, "the exception is no longer needed")
                    elsewhere.add(where)
                else:
                    assert found, (where, as_of)
    assert elsewhere == set(YEAR_ELSEWHERE) and fixes > 0
