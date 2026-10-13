"""Regression tests for the OSM naming rules of fix round 7, each measured on the seed's inputs
against the round 6 sample (the record a rule fixes is named by its record id there). The elements
are in tests/fixtures/osm/overpass-fix7.json, copied from the seed's Overpass response; the
first-report stop of the same round is in tests/sources/test_osm_corrections.py."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from atlas.dissolve import Bounds, OsmObject, operator_words
from atlas.geo.counties import CountyIndex
from atlas.schema.record import FacilityRecord
from atlas.sources.base import ImportContext, ImportResult
from atlas.sources.osm import (
    OsmImporter,
    _canonical_name,
    _numbered_name,
    _starts_with_operator,
)
from atlas.validate import validate_record

MakeContext = Callable[..., ImportContext]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIX7 = FIXTURES / "osm" / "overpass-fix7.json"
NO_OVERRIDES = FIXTURES / "osm" / "overrides-empty.json"


@pytest.fixture
def run(make_test_context: MakeContext) -> ImportResult:
    """The importer on the fix7 fixture, without PNNL and without overrides."""
    parser = argparse.ArgumentParser()
    OsmImporter().add_arguments(parser)
    args = parser.parse_args(["--no-pnnl", "--overrides", str(NO_OVERRIDES)])
    return OsmImporter().run(make_test_context(input_path=FIX7), args)


def record(result: ImportResult, ref: str) -> FacilityRecord:
    (found,) = [c.record for c in result.candidates if ref in c.match_values]
    return found


def aliases(rec: FacilityRecord) -> list[str]:
    return [a.name for a in rec.aliases]


def member(ref: str, name: str, operator: str, kind: str = "building") -> OsmObject:
    """A member 40 m square (a point has no box), named and run by operator."""
    d = 40.0 / 111_195
    bounds = None if kind == "point" else Bounds(40.0, -100.0, 40.0 + d, -100.0 + d)
    tags = {"name": name, "operator": operator, "telecom": "data_center"}
    return OsmObject(ref, 40.0, -100.0, bounds, tags, kind)  # type: ignore[arg-type]


def name_of(members: list[OsmObject], operator: str) -> str:
    ops = frozenset({operator.casefold()})
    return _canonical_name(members[0], members, operator, "Place", "VA", ops)[0]


def test_every_record_validates(run: ImportResult, counties: CountyIndex, today: date) -> None:
    for c in run.candidates:
        assert validate_record(c.record, counties=counties, today=today) == [], c.match_values


# ---------------------------------------------------------------------------- numbered buildings


def test_buildings_named_only_by_their_number_name_their_campus_as_a_series(
    run: ImportResult,
) -> None:
    """Three buildings 'Microsoft Data Center 1' to '3' were one record called after the first
    (round 6 sample, gwa-01m4tabmf184zm5sfyrmq6vjge): specific_name finds no word of their own,
    so the campus took the representative's name."""
    microsoft = record(run, "relation/19787207")
    assert len(microsoft.external_ids["osm"]) == 3
    assert microsoft.canonical_name.startswith("Microsoft Data Center 1 to 3 (")
    assert sorted(aliases(microsoft)) == [
        "Microsoft Data Center 1",
        "Microsoft Data Center 2",
        "Microsoft Data Center 3",
    ]


@pytest.mark.parametrize(
    ("name", "numbered"),
    [
        ("Microsoft Data Center 1", True),
        ("Microsoft Data Center 12", True),
        ("Microsoft Data Center", False),  # no number
        ("Data Center 2", False),  # no word of the operator
        ("Building 2", False),
        ("Microsoft Ashburn 1", False),  # a specific name: campus_name reads it as it stands
    ],
)
def test_a_numbered_name(name: str, numbered: bool) -> None:
    words = operator_words([member("way/1", "x", "Microsoft")])
    assert _numbered_name(name, words) is numbered


def test_numbered_names_name_a_campus_only_as_a_series() -> None:
    """One numbered building keeps its name, and numbered names of no one series name no
    campus."""
    one = [member("way/1", "Microsoft Data Center 1", "Microsoft")]
    assert name_of(one, "Microsoft") == "Microsoft Data Center 1 (Place, VA)"
    mixed = [
        member("way/1", "Microsoft Data Center 1", "Microsoft"),
        member("way/2", "Microsoft Building 2", "Microsoft"),
    ]
    assert name_of(mixed, "Microsoft") == "Microsoft Data Center 1 (Place, VA)"


# ---------------------------------------------------------------------------- named points


def test_named_points_name_their_campus_like_buildings(run: ImportResult) -> None:
    """DartPoints' two nodes BTR01 and BTR02 at 7127 Florida Blvd were one record called after
    BTR02 (round 6 sample, gwa-01m4tabfskb8kbnw7n709gmg6q); the same rule names the seed's other
    campuses of named points: two nodes, or a building and a node."""
    dartpoints = record(run, "node/14124760832")
    assert dartpoints.canonical_name.startswith("DartPoints Baton Rouge (")
    assert sorted(aliases(dartpoints)) == ["DartPoints Baton Rouge 1", "DartPoints Baton Rouge 2"]
    expected = {
        "node/13746843367": "Digital Fortress - Portland (",
        "way/239147373": "Lumen McLean (",
        "way/1561771299": "NTT VA10 and VA11 (",
        "way/918943458": "EdgeConneX DEN01 and DEN02 (",
    }
    for ref, start in expected.items():
        assert record(run, ref).canonical_name.startswith(start), ref


def test_a_point_does_not_break_the_buildings_series() -> None:
    """The node 'Equinix Ashburn DC18' among Equinix DC1 to DC22: with the buildings and the
    point together nothing would name the campus, and the record would take its first
    building's name. The buildings name it first."""
    campus = [
        member("way/1", "Equinix DC1", "Equinix"),
        member("way/2", "Equinix DC2", "Equinix"),
        member("way/3", "Equinix DC4", "Equinix"),
        member("node/4", "Equinix Ashburn DC18", "Equinix", "point"),
    ]
    assert name_of(campus, "Equinix") == "Equinix DC1, DC2 and DC4 (Place, VA)"


# ---------------------------------------------------------------------------- operator prefix


def test_a_name_that_starts_with_the_operator_misspelt_gets_no_prefix(run: ImportResult) -> None:
    """'Date Center West - Eugene' under operator 'Data Center West, Inc.' was published as
    'Data Center West, Inc. Date Center West - Eugene' (round 6 sample,
    gwa-01m4tabfrdnhxv0sw1h8vgpk4n), and so was the Medford way that carries the same name."""
    for ref in ("node/13748859030", "way/967131562"):
        assert record(run, ref).canonical_name.startswith("Date Center West - Eugene ("), ref


@pytest.mark.parametrize(
    ("base", "operator", "starts"),
    [
        ("Date Center West - Eugene", "Data Center West, Inc.", True),
        ("Data Center West", "Data Center West, Inc.", True),
        ("Date Centre West", "Data Center West, Inc.", False),  # three characters apart
        ("Date Center East", "Data Center West, Inc.", False),
        ("Arc Data Center", "Ark", False),  # one word: a typo counts from 6 letters
        ("CyrusOne Data Center Campus", "CyrysOne", True),
    ],
)
def test_the_operator_as_a_phrase_one_character_apart(
    base: str, operator: str, starts: bool
) -> None:
    assert _starts_with_operator(base, operator, None) is starts
