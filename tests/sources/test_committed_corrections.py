"""The cited corrections committed in config/overrides/osm.json, epoch.json and aigridwatch.json
(mechanism §11.6, docs/corrections.md "Checks on the committed files"): one check of every Fix in
the three files, and of the names and operators the OSM element decisions of fix round 6 give.
The entries' mechanics are tested on the importers' fixtures (test_osm_corrections.py,
test_epoch_corrections.py, test_aigridwatch_corrections.py); the seed run applies the whole files
to the seed inputs."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from atlas.dissolve import GENERIC_NAME_WORDS
from atlas.geo.counties import CountyIndex
from atlas.schema.rollup import STOPPED, period_start
from atlas.sources import aigridwatch, epoch, osm
from atlas.sources.corrections import (
    FIXES,
    STOP_SOURCE_TYPES,
    STOPPED_EVENTS,
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
from atlas.text import find_personal_data, normalize_name

OVERRIDES = Path(__file__).resolve().parents[2] / "config" / "overrides"
FILES = ("osm.json", "epoch.json", "aigridwatch.json")
# The keys every committed Fix gives, non-empty (source_type and issue_url are optional in the
# model; the committed files give them).
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
# The review PR the corrections of fix round 6 (reviewed from FIX_ROUND_6 on) were decided on.
REVIEW_PR = "https://github.com/moonspells/gigawatt-atlas/pull/2"
FIX_ROUND_6 = date(2026, 10, 10)
WAYBACK = "https://web.archive.org/web/"
OSM_ELEMENT = ("https://www.openstreetmap.org/way/", "https://www.openstreetmap.org/node/")

# A value a Fix writes is verbatim in its quote, except these, each found in the page another way
# (the reading named; see stated_otherwise). Each stays only while it is needed.
STATED_OTHERWISE: dict[tuple[str, str, str], str] = {
    # OSM names and organizations in the atlas's form, from the words of the page and its
    # publisher (an operator's page names its sites without its own name; "DXC" for DXC
    # Technology).
    ("osm.json", "node/10910879064", "Digital Realty ORD10"): "words",
    ("osm.json", "way/1343774537", "CloudHQ LC Campus"): "words",
    ("osm.json", "way/1387391200", "Compass Loudoun County I"): "words",
    ("osm.json", "way/190868312", "NTT Sacramento CA1 to CA3"): "words",
    ("osm.json", "way/597876719", "Digital Realty Dallas"): "words",
    ("osm.json", "way/818790301", "DXC Technology"): "words",
    ("osm.json", "way/818790301", "DXC Technology Clifton Data Center"): "words",
    # Streets the page abbreviates (Rd, Dr, E).
    ("osm.json", "way/1238705019", "7300 East Pleasant Valley Road"): "street",
    ("osm.json", "way/1276518687", "22588 Relocation Drive"): "street",
    ("osm.json", "way/1486780799", "601 North Star Road"): "street",
    # The county a cited OpenStreetMap element's outline gives, named in the Fix's reason.
    ("epoch.json", "Meta Kuna", "Ada County"): "reason",
    # The county the entry's Fix that sets it names (where Kearney's taxes go), which the Fix
    # that takes the city off restates.
    ("aigridwatch.json", "kearney-data-center-ne", "Buffalo County"): "entry",
}
# The year of a Fix's date is in its quote, its published_at or its URL, except where the page
# gives the date another way; each exception says how, and stays only while it is needed.
YEAR_ELSEWHERE: dict[tuple[str, str, int], str] = {
    ("epoch.json", "Google Mesa", 0): (
        "GovTech's byline dates the story June 28, 2019, with a time in no zone"
    ),
    ("epoch.json", "Meta Rosemount", 0): (
        "the page of 2023-09-02 dates Xcel's first petition to 'last year'"
    ),
}

# Words a written name or organization need not find in its page: generic words, connectors and
# words of a company's legal or trading name ("DXC Technology" is "DXC" on its own page).
_UNQUOTED = GENERIC_NAME_WORDS | {"to", "technology", "technologies", "infrastructure", "group"}
# Street suffixes and directions as pages abbreviate them.
_STREET_WORDS = {
    "rd": "road",
    "dr": "drive",
    "st": "street",
    "ave": "avenue",
    "blvd": "boulevard",
    "ct": "court",
    "pkwy": "parkway",
    "cir": "circle",
    "ln": "lane",
    "e": "east",
    "w": "west",
    "n": "north",
    "s": "south",
}


def _street(text: str) -> str:
    return " ".join(_STREET_WORDS.get(w, w) for w in normalize_name(text).split())


def _quoted(value: str, quote: str, publisher: str) -> bool:
    """Every word of a written name or organization is in the quote, or names its publisher."""
    have = set(normalize_name(quote).split()) | set(normalize_name(publisher).split())
    return set(normalize_name(value).split()) - _UNQUOTED <= have


def entries(file: str) -> Mapping[str, Sequence[Fix]]:
    """A committed file's Fixes by entry, as its importer loads them."""
    path = OVERRIDES / file
    if file == "osm.json":
        return {ref: ov.corrections for ref, ov in osm.load_overrides(path).items()}
    if file == "epoch.json":
        return {name: ov.corrections for name, ov in epoch.load_overrides(path).items()}
    return {pid: ov.corrections for pid, ov in aigridwatch.load_releases(path).items()}


def written(fix_: Fix, counties: CountyIndex) -> list[str]:
    """What a Fix's ops write into the record, as text: a value, a name, an organization, the
    place and address of a set_location."""
    out: list[str] = []
    for op in fix_.ops:
        if isinstance(op, SetValue) and op.value is not None:
            value = op.value
            out.append(f"{value:g}" if isinstance(value, int | float) else str(value))
        elif isinstance(op, Rename):
            out.append(op.base)
        elif isinstance(op, AddAlias | AddParty):
            out.append(op.name)
        elif isinstance(op, SetParty):
            out += op.names
        elif isinstance(op, RenameParty):
            out.append(op.to)
        elif isinstance(op, SetLocation):
            out += [p for p in (op.city, op.municipality, op.street, op.postcode) if p is not None]
            county = counties.get(op.county_fips) if op.county_fips is not None else None
            if county is not None:
                out.append(f"{county.name} County")
    return out


def stated_otherwise(reading: str, value: str, fix_: Fix, others: Sequence[Fix]) -> bool:
    """Whether a value its quote does not hold verbatim is in the page by its reading: "words"
    (_quoted), "street" (abbreviations spelled out), "reason" (a place the outline of the cited
    OpenStreetMap element gives, named in the reason), "entry" (a county in the quote of a Fix of
    the same entry whose set_location gives a county)."""
    if reading == "words":
        return _quoted(value, fix_.quote, fix_.publisher)
    if reading == "street":
        return _street(value) in _street(fix_.quote)
    if reading == "reason":
        return str(fix_.source_url).startswith(OSM_ELEMENT) and value in fix_.reason
    assert reading == "entry", reading
    return value.endswith(" County") and any(
        value in f.quote
        for f in others
        if any(isinstance(op, SetLocation) and op.county_fips is not None for op in f.ops)
    )


def dated(fix_: Fix) -> list[str]:
    """The dates of the events a Fix's ops add or redate, other than planned ones."""
    out: list[str] = []
    for op in fix_.ops:
        if (isinstance(op, AddEvent) and not op.planned) or isinstance(op, Observe | FirstReportOp):
            out.append(op.as_of)
        elif isinstance(op, ReplaceEvent) and op.set.as_of is not None and not op.set.planned:
            out.append(op.set.as_of)
    return out


def announces(fix_: Fix) -> bool:
    """Whether a Fix's ops give an announced event: one they add, or one a replace_event makes
    an announcement (a retype) or dates anew."""
    return any(
        (isinstance(op, AddEvent) and op.event == "announced")
        or (
            isinstance(op, ReplaceEvent)
            and (op.set.event or op.match.event) == "announced"
            and (op.set.event is not None or op.set.as_of is not None)
        )
        for op in fix_.ops
    )


def stops(fix_: Fix) -> bool:
    """Whether a Fix's ops give a paused, denied or cancelled status: an event they add, or one a
    replace_event changes, by its new status or by the stopped status or event its match names
    (a new date of a denial makes the Fix its only source)."""
    return any(
        (isinstance(op, AddEvent | Observe) and op.status in STOPPED)
        or (
            isinstance(op, ReplaceEvent)
            and (
                op.set.status in STOPPED
                or op.match.status in STOPPED
                or op.match.event in STOPPED_EVENTS
            )
        )
        for op in fix_.ops
    )


@pytest.mark.parametrize("file", FILES)
def test_every_committed_correction_is_cited(file: str, counties: CountyIndex) -> None:
    """Each Fix of the file (mechanism §11.6): its citation fields; a quote of at most 300
    characters, read before now (a UTC time); a decision on or after that day and no later than
    today, from the review PR of its round; no personal data; a Wayback snapshot of source_url
    when the page refused the project's client; a stopped status only from a source 07 §6.4 gate
    6 allows. What it writes is verbatim in its quote (or in the page as STATED_OTHERWISE says),
    and every date it gives is no later than today, its year in the quote, the page's date or the
    URL. The file is canonical JSON."""
    text = (OVERRIDES / file).read_text(encoding="utf-8")
    raw: dict[str, Any] = json.loads(text)
    assert text == json.dumps(raw, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    now = datetime.now(UTC)
    today = min(now.date(), date.today())  # the importer's day (UTC) and the local one
    otherwise: set[tuple[str, str, str]] = set()
    elsewhere: set[tuple[str, str, int]] = set()
    fixes = 0
    for key, corrections in entries(file).items():
        for i, fix_ in enumerate(corrections):
            fixes += 1
            where = (file, key, i)
            for name in FIX_KEYS:
                assert raw[key]["corrections"][i].get(name), (where, name)
            assert 0 < len(fix_.quote) <= 300 and fix_.retrieved_at <= now, where
            assert fix_.retrieved_at.utcoffset() == timedelta(0), where  # a UTC fetch time
            assert fix_.retrieved_at.date() <= fix_.reviewed_at <= today, where
            assert str(fix_.issue_url).startswith(REVIEWS), where
            if fix_.reviewed_at >= FIX_ROUND_6:
                assert str(fix_.issue_url) == REVIEW_PR, where
            for words in (fix_.quote, fix_.reason, fix_.publisher, fix_.title or ""):
                assert find_personal_data(words) == [], where
            if fix_.archive_url is not None:
                assert str(fix_.archive_url).startswith(WAYBACK), where
                assert str(fix_.archive_url).endswith(str(fix_.source_url)), where
            if stops(fix_):
                assert fix_.source_type in STOP_SOURCE_TYPES, where
            for value in written(fix_, counties):
                if value in fix_.quote:
                    continue
                # Each exception is needed by a Fix of its entry (the set check below); another
                # Fix of the entry may quote the value (Kearney's county).
                reading = STATED_OTHERWISE.get((file, key, value))
                assert reading is not None, (where, value)
                assert stated_otherwise(reading, value, fix_, corrections), (where, value)
                otherwise.add((file, key, value))
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
    assert otherwise == {k for k in STATED_OTHERWISE if k[0] == file}
    assert elsewhere == {k for k in YEAR_ELSEWHERE if k[0] == file}
    assert fixes > 0


def test_an_announcement_quotes_the_plan() -> None:
    """entries-5 (review of fix round 6): an announced event is a public statement that a project
    is planned (07 §2.3), so a Fix that gives one quotes the plan. A quote that reports a
    purchase, a sale or a transfer also states a data center plan, as AI GridWatch reads a row's
    property deal (aigridwatch.property_deal: a deal states no plan): CoreWeave Lancaster's Fix
    quotes the data centers proposed in the printing plants, not the plants' purchase."""
    announcements = 0
    for file in FILES:
        for key, corrections in entries(file).items():
            for i, fix_ in enumerate(corrections):
                if not announces(fix_):
                    continue
                announcements += 1
                deal = aigridwatch._PROPERTY_RE.search(fix_.quote)
                assert deal is None or aigridwatch._PLAN_RE.search(fix_.quote), (file, key, i)
    assert announcements > 0


def test_the_osm_element_decisions_of_fix_round_6_are_quoted() -> None:
    """The names and operators that the element decisions of fix round 6 give are in their quote,
    read by words (an operator's own page names its sites without its own name). The decisions'
    citations are checked by test_osm_fix4.py::test_every_committed_override_is_cited."""
    decisions = 0
    for ref, ov in osm.load_overrides(OVERRIDES / "osm.json").items():
        if not ov.decides or ov.reviewed_at < FIX_ROUND_6:
            continue
        for key in ("name", "operator"):
            value = getattr(ov, key)
            if key in ov.model_fields_set and value is not None:
                decisions += 1
                assert _quoted(value, ov.quote, ov.publisher), (ref, value)
    assert decisions > 0


def test_the_readings_of_a_value_stated_otherwise() -> None:
    """The readings STATED_OTHERWISE names: words, not spelling or legal form; a street's
    abbreviations, not its number."""
    assert _quoted("NTT Sacramento CA1 to CA3", "Sacramento CA1 Data Center Sacramento CA3", "NTT")
    assert _quoted("DXC Technology", "Concurrently, DXC, through its subsidiary", "The Register")
    assert not _quoted("Zayo Baltimore", "Crown Castle Baltimore", "PeeringDB")
    assert _street("22588 Relocation Drive") in _street("22588 RELOCATION DR, STERLING")
    assert _street("601 North Star Road") not in _street("401 North Star Rd.")


@pytest.mark.parametrize(
    ("op", "expected"),
    [
        ({"op": "add_event", "event": "denied", "status": "denied", "as_of": "2026-08"}, True),
        ({"op": "observe", "status": "paused", "as_of": "2026-08"}, True),
        (
            {"op": "add_event", "event": "approved", "status": "permitted", "as_of": "2026-08"},
            False,
        ),
        (
            {"op": "replace_event", "match": {"event": "denied"}, "set": {"as_of": "2026-08-13"}},
            True,
        ),
        (
            {"op": "replace_event", "match": {"status": "cancelled"}, "set": {"note": "per X"}},
            True,
        ),
        (
            {"op": "replace_event", "match": {"event": "other"}, "set": {"status": "paused"}},
            True,
        ),
        (
            {"op": "replace_event", "match": {"event": "announced"}, "set": {"as_of": "2026"}},
            False,
        ),
    ],
)
def test_stops_names_every_stopped_status(op: dict[str, Any], expected: bool) -> None:
    """mechanism-2 (review of fix round 6): the committed-files check of gate 6 counts a
    replace_event whose match names a stopped status or event, so a redate of a denial from a
    source gate 6 does not allow fails CI too."""
    (parsed,) = FIXES.validate_python(
        [
            {
                "source_url": "https://example.org/a",
                "publisher": "Example News",
                "source_type": "news",
                "quote": "a quote",
                "basis": "upstream_error",
                "reason": "The reviewer read the cited page",
                "retrieved_at": "2026-10-10T12:00:00Z",
                "reviewed_at": "2026-10-10",
                "ops": [op],
            }
        ]
    )
    assert stops(parsed) is expected


@pytest.mark.parametrize(
    ("op", "expected"),
    [
        ({"op": "add_event", "event": "announced", "status": "announced", "as_of": "2025"}, True),
        ({"op": "add_event", "event": "approved", "status": "permitted", "as_of": "2025"}, False),
        # DC Blox's redate of its announcement; Google Botetourt's first report retyped to one.
        (
            {"op": "replace_event", "match": {"event": "announced"}, "set": {"as_of": "2026-04"}},
            True,
        ),
        (
            {
                "op": "replace_event",
                "match": {"event": "first_reported"},
                "set": {"event": "announced", "as_of": "2025-06-24"},
            },
            True,
        ),
        # STACK Berry Hill's announcement that is a first report, and a note, announce nothing.
        (
            {
                "op": "replace_event",
                "match": {"event": "announced"},
                "set": {"event": "first_reported"},
            },
            False,
        ),
        ({"op": "replace_event", "match": {"event": "announced"}, "set": {"note": "per X"}}, False),
    ],
)
def test_announces_names_every_announcement(op: dict[str, Any], expected: bool) -> None:
    """entries-5: the Fixes test_an_announcement_quotes_the_plan reads, by what their ops give."""
    (parsed,) = FIXES.validate_python(
        [
            {
                "source_url": "https://example.org/a",
                "publisher": "Example News",
                "source_type": "news",
                "quote": "a quote",
                "basis": "upstream_error",
                "reason": "The reviewer read the cited page",
                "retrieved_at": "2026-10-10T12:00:00Z",
                "reviewed_at": "2026-10-10",
                "ops": [op],
            }
        ]
    )
    assert announces(parsed) is expected
