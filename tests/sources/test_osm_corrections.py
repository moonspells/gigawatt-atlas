"""The element decisions and record corrections of config/overrides/osm.json (fix round 6;
docs/sources/osm-pnnl.md section 9, docs/corrections.md): join, alone, status with status_as_of,
expect, the redundancy guards, and the corrections applied to the record that holds an element.
The elements are in the fix4, fix5 and fix6 fixtures, copied from the seed's Overpass response;
the decisions are the reviewers' kind (NTT's Sacramento campus, Equinix DC18), cited to an example
page. tests/sources/test_corrections.py tests the shared ops themselves, and
tests/sources/test_committed_corrections.py the citation of every committed Fix."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import HttpUrl

from atlas.dissolve import Bounds, Cluster, DissolveNote, DissolveResult, OsmObject, dissolve
from atlas.geo.counties import CountyIndex
from atlas.net import FetchError
from atlas.schema.record import FacilityRecord
from atlas.schema.rollup import is_expanding
from atlas.sources.base import (
    Candidate,
    ImportContext,
    ImportResult,
    InputSnapshot,
    ReviewItem,
    merge_import,
)
from atlas.sources.corrections import human_confidence
from atlas.sources.osm import (
    ElementOverride,
    OsmImporter,
    _keep_first_reported,
    _status_groups,
    build_candidates,
    classify,
    join_clusters,
    load_overrides,
)
from atlas.store import RecordStore
from atlas.validate import validate_record

MakeContext = Callable[..., ImportContext]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIX4 = FIXTURES / "osm" / "overpass-fix4.json"
FIX5 = FIXTURES / "osm" / "overpass-fix5.json"
FIX6 = FIXTURES / "osm" / "overpass-fix6.json"
OSM = "https://www.openstreetmap.org/"
PAGE = "https://example.com/campus"
# The top-level citation of an element decision.
CITED: dict[str, Any] = {
    "reason": "The operator's page says so",
    "source_url": PAGE,
    "publisher": "Example",
    "source_type": "company_release",
    "quote": "Our three buildings on one campus.",
    "retrieved_at": "2026-10-10T12:00:00Z",
    "reviewed_at": "2026-10-10",
}


def fix(*ops: dict[str, Any], url: str = "https://example.com/fix", **extra: Any) -> dict[str, Any]:
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


@pytest.fixture
def run(make_test_context: MakeContext, tmp_path: Path) -> Callable[..., ImportResult]:
    """The importer on a fixture, without PNNL, with the entries given as its overrides file."""

    def go(fixture: Path, entries: dict[str, Any], **ctx: Any) -> ImportResult:
        path = tmp_path / "osm.json"
        path.write_text(json.dumps(entries), encoding="utf-8")
        parser = argparse.ArgumentParser()
        OsmImporter().add_arguments(parser)
        args = parser.parse_args(["--no-pnnl", "--overrides", str(path)])
        return OsmImporter().run(make_test_context(input_path=fixture, **ctx), args)

    return go


Run = Callable[..., ImportResult]


def candidate(result: ImportResult, ref: str) -> Candidate:
    (found,) = [c for c in result.candidates if ref in c.match_values]
    return found


def record(result: ImportResult, ref: str) -> FacilityRecord:
    return candidate(result, ref).record


def items(result: ImportResult, kind: str, external_id: str) -> list[ReviewItem]:
    return [i for i in result.review if i.kind == kind and i.external_id == external_id]


def valid(result: ImportResult, counties: CountyIndex, today: date) -> None:
    for c in result.candidates:
        assert validate_record(c.record, counties=counties, today=today) == [], c.match_values


# ---------------------------------------------------------------------------- the entry model


def test_an_entry_with_only_corrections_has_no_top_level_citation(tmp_path: Path) -> None:
    path = tmp_path / "osm.json"
    op = {"op": "set", "path": "/location/postcode", "from": "1", "value": "2"}
    path.write_text(json.dumps({"way/1": {"corrections": [fix(op)]}}), encoding="utf-8")
    (entry,) = load_overrides(path).values()
    assert not entry.decides and len(entry.corrections) == 1
    with pytest.raises(ValueError, match="no top-level quote"):
        _ = entry.quote
    path.write_text(json.dumps({"way/1": {**CITED, "corrections": [fix(op)]}}), encoding="utf-8")
    with pytest.raises(FetchError, match="has no top-level citation"):
        load_overrides(path)


def test_an_element_decision_reads_its_citation() -> None:
    entry = ElementOverride.model_validate({"join": "way/2", **CITED})
    assert entry.decides and entry.quote == CITED["quote"] and entry.kind == "company_release"
    assert entry.reviewed_at == date(2026, 10, 10) and str(entry.source_url) == PAGE


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({**CITED, "join": "way/2", "alone": True}, "join and alone exclude each other"),
        ({**CITED, "name": "X", "status_as_of": "2026-09"}, "status_as_of dates a status"),
        ({"status_as_of": "2026-09", "corrections": [fix({"op": "hold"})]}, "status_as_of"),
        ({**CITED, "drop": True, "corrections": [fix({"op": "hold"})]}, "replaces nothing"),
        ({**CITED, "drop": True, "join": "way/2"}, "replaces nothing"),
        ({"join": "way/2", "reason": "r"}, r"needs its citation: \['source_url'"),
        ({**CITED, "join": "Way 2"}, "join"),
        ({**CITED, "status": "planned"}, "status"),
        ({**CITED, "status": "cancelled", "source_type": "commercial_aggregator"}, "gate 6"),
        ({**CITED, "expect": {"": "x"}, "name": "X"}, "expect"),
        ({"expect": {"name": "X"}}, "must drop the element or replace"),
        ({"corrections": []}, "must drop the element or replace"),
        ({**CITED, "alone": True, "colour": "red"}, "Extra inputs"),
    ],
)
def test_a_malformed_entry_is_an_error(tmp_path: Path, entry: dict[str, Any], message: str) -> None:
    path = tmp_path / "osm.json"
    path.write_text(json.dumps({"way/1": entry}), encoding="utf-8")
    with pytest.raises(FetchError, match=message):
        load_overrides(path)


def test_an_entry_may_not_join_its_own_element(tmp_path: Path) -> None:
    path = tmp_path / "osm.json"
    path.write_text(json.dumps({"way/1": {**CITED, "join": "way/1"}}), encoding="utf-8")
    with pytest.raises(FetchError, match="joins its own element: way/1"):
        load_overrides(path)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({**CITED, "status": "announced", "status_as_of": "2026-11"}, "status_as_of 2026-11"),
        ({"corrections": [fix({"op": "hold"}, reviewed_at="2026-10-20")]}, "reviewed_at"),
    ],
)
def test_an_entry_dated_after_the_run_stops_it(
    run: Run, entry: dict[str, Any], message: str
) -> None:
    with pytest.raises(FetchError, match=message):
        run(FIX4, {"node/14156477686": entry})


# ---------------------------------------------------------------------------- input checks


def test_an_expect_guard_checks_the_tags_as_read(run: Run) -> None:
    status = {**CITED, "status": "announced"}
    good = {"name": "Equinix Ashburn DC18", "proposed:telecom": None}
    result = run(FIX4, {"node/14156477686": {**status, "expect": good}})
    assert record(result, "node/14156477686").status == "operating"
    for wrong in ({"name": "Equinix DC18"}, {"telecom": None}):
        with pytest.raises(FetchError, match=r"expect .* but the element has .*retagged upstream"):
            run(FIX4, {"node/14156477686": {**status, "expect": wrong}})


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"name": "RagingWire CA3"}, "has the name 'RagingWire CA3' already"),
        ({"operator": "NTT", "name": "X"}, None),  # it also removes operator:wikidata
        ({"join": "way/32560290"}, None),
        ({"join": "way/1"}, "join names way/1, which this input does not have"),
    ],
)
def test_a_decision_must_change_something(
    run: Run, entry: dict[str, Any], message: str | None
) -> None:
    entries = {"way/190868312": {**CITED, **entry}}
    if message is None:
        run(FIX5, entries)
    else:
        with pytest.raises(FetchError, match=message):
            run(FIX5, entries)


def test_an_operator_the_element_has_already_is_an_error(run: Run) -> None:
    """way/597876719 has operator Rackspace and no operator:wikidata; way/597876723 has no name
    and no operator."""
    with pytest.raises(FetchError, match="has the operator 'Rackspace' already"):
        run(FIX5, {"way/597876719": {**CITED, "operator": "Rackspace"}})
    for key in ("name", "operator"):
        with pytest.raises(FetchError, match=f"has no {key} tag already"):
            run(FIX5, {"way/597876723": {**CITED, key: None}})


def test_a_join_target_must_be_in_a_campus_of_its_own(run: Run) -> None:
    join = {**CITED, "join": "way/32560290"}
    for target, message in (
        ({"drop": True}, "which an entry drops"),
        ({"alone": True}, "sets alone"),
    ):
        with pytest.raises(FetchError, match=message):
            run(FIX5, {"way/190868312": join, "way/32560290": {**CITED, **target}})
    # RagingWire CA1 and CA2 are one campus already.
    with pytest.raises(FetchError, match="one campus already, so the join changes nothing"):
        run(FIX5, {"way/32560292": {**CITED, "join": "way/32560290"}})
    # A second join into the same campus changes nothing either (joins apply in ref order).
    second = {"way/190868312": join, "way/32560292": {**CITED, "join": "way/190868312"}}
    with pytest.raises(FetchError, match="way/190868312 and way/32560290 are one campus already"):
        run(FIX5, second)


# ---------------------------------------------------------------------------- join


def test_a_join_makes_one_record_of_two_clusters(
    run: Run, counties: CountyIndex, today: date
) -> None:
    """NTT's Sacramento campus: RagingWire CA3 is 720 m from CA2, beyond every rule
    (test_osm_fix5); a reviewer's join and a cited rename make one record."""
    rename = {"op": "rename", "from": "NTT RagingWire", "base": "NTT Sacramento CA1 to CA3"}
    entries = {"way/190868312": {**CITED, "join": "way/32560290", "corrections": [fix(rename)]}}
    result = run(FIX5, entries)
    found = candidate(result, "way/190868312")
    refs = ["way/32560290", "way/32560292", "way/190868312"]
    assert list(found.match_values) == refs and list(found.joined) == refs
    assert found.split_from is None
    ntt = found.record
    assert ntt.canonical_name == "NTT Sacramento CA1 to CA3 (Sacramento County, CA)"
    assert ntt.external_ids["osm"] == refs
    s3 = next(s for s in ntt.sources if s.id == "s3")
    assert (str(s3.url), s3.quote_match, s3.quote) == (PAGE, "human", CITED["quote"])
    assert s3.supports == ["/buildings/2"]  # CA3, a building of this campus
    assert [b.ref for b in ntt.buildings] == [f"osm:{r}" for r in refs]
    assert [c.pointer for c in ntt.corrections] == ["/canonical_name"]
    # The note that CA3 continues CA2's series, 720 m apart, is gone with the two records.
    assert items(result, "possible_duplicate", "way/190868312") == []
    assert not any("way/190868312" in json.dumps(i.data) for i in result.review)
    valid(result, counties, today)


def test_a_join_across_a_county_line(run: Run, counties: CountyIndex, today: date) -> None:
    """Digital Realty IAD53 (Manassas city) is 155 m from IAD51 (Prince William County): the
    county rule held it (test_osm_fix4). A join makes one record across the line, with the
    representative's county and a county_mismatch item."""
    result = run(FIX4, {"way/1269259655": {**CITED, "join": "way/1194169753"}})
    iad = record(result, "way/1269259655")
    assert "way/1194169753" in iad.external_ids["osm"]
    assert iad.location.county_fips == "51153"
    assert items(result, "possible_duplicate", "way/1269259655") == []
    (line,) = items(result, "county_mismatch", "way/1194169753")
    assert line.data == {"osm": ["way/1269259655"], "county_fips": "51683"}
    assert "Manassas city" in line.reason and "a reviewed join" in line.reason
    valid(result, counties, today)


def test_a_joined_tenant_is_named_by_a_correction(
    run: Run, counties: CountyIndex, today: date
) -> None:
    """Rackspace's DFW3, 42 m from Digital Realty's row, is a building of that campus (a
    tenant_building item before). The join makes it a member; an add_party cites the way."""
    tenant = fix(
        {"op": "add_party", "role": "tenant", "name": "Rackspace"},
        url=f"{OSM}way/597876719",
        publisher="OpenStreetMap contributors",
        source_type="open_dataset",
        quote="operator=Rackspace",
    )
    entries = {"way/597876719": {**CITED, "join": "way/597876730", "corrections": [tenant]}}
    result = run(FIX5, entries)
    dallas = record(result, "way/597876730")
    assert "way/597876719" in dallas.external_ids["osm"]
    assert [o.name for o in dallas.parties.operator] == ["Digital Realty"]
    ((name, sids),) = [(o.name, o.source_ids) for o in dallas.parties.tenant]
    way = next(s for s in dallas.sources if str(s.url) == f"{OSM}way/597876719")
    assert name == "Rackspace" and sids == [way.id]  # the element's own source, reused
    assert (way.quote, way.quote_match) == ("operator=Rackspace", "human")
    assert items(result, "possible_duplicate", "way/597876719") == []
    valid(result, counties, today)


def test_join_clusters_merges_and_drops_the_notes_between(counties: CountyIndex) -> None:
    def obj(ref: str, lat: float) -> OsmObject:
        bounds = Bounds(lat, -77.47, lat + 0.0003, -77.4697)
        tags = {"building": "data_center", "telecom": "data_center"}
        return OsmObject(ref, *bounds.center, bounds, tags, "building")

    a, b, c = obj("way/1", 39.01), obj("way/2", 39.02), obj("way/3", 39.03)
    clusters = [Cluster((m,), m) for m in (a, b, c)]
    notes = [
        DissolveNote("same_operator_nearby", ("way/1", "way/2"), "8", "a series"),
        DissolveNote("county_blocked", ("way/1", "way/2"), "2"),
        DissolveNote("same_operator_nearby", ("way/2", "way/3"), "8", "a street"),
    ]
    entry = ElementOverride.model_validate({"join": "way/1", **CITED})
    regions = {"way/1": "51107", "way/2": "51059", "way/3": "51059"}
    joined = join_clusters(DissolveResult(clusters, notes), {"way/2": entry}, regions=regions)
    assert [m.refs for m in joined.clusters] == [("way/1", "way/2"), ("way/3",)]
    assert joined.notes == [
        DissolveNote("county_line", ("way/2", "way/1"), "join"),
        DissolveNote("same_operator_nearby", ("way/2", "way/3"), "8", "a street"),
    ]
    same = join_clusters(DissolveResult(clusters, notes), {}, regions=regions)
    assert same.clusters == clusters and same.notes == notes


# ---------------------------------------------------------------------------- alone


def test_an_alone_element_is_a_campus_of_its_own(
    run: Run, counties: CountyIndex, today: date
) -> None:
    """way/45583050 leaves Equinix DC14's cluster and is a record of its own, in scope."""
    result = run(FIX6, {"way/45583050": {**CITED, "alone": True}})
    found = candidate(result, "way/45583050")
    assert found.match_values == ("way/45583050",) and found.split_from == "way/45583050"
    assert found.record.scope == "in_scope"
    assert any(str(s.url) == PAGE for s in found.record.sources)
    assert record(result, "way/1227539675").external_ids["osm"] == ["way/1227539675"]
    valid(result, counties, today)


def test_an_element_alone_anyway_is_an_error(run: Run) -> None:
    """way/45583037 (7777 Infantry Ridge Road) is a record of its own already (fix round 6)."""
    with pytest.raises(FetchError, match="no dissolve rule joins it"):
        run(FIX6, {"way/45583037": {**CITED, "alone": True}})


# ---------------------------------------------------------------------------- status


def test_an_element_status_makes_a_phase(run: Run, counties: CountyIndex, today: date) -> None:
    """Equinix DC18 (node/14156477686) is planned, though telecom=data_center reads operating:
    its status entry makes it a phase of its own, and the campus stays operating, expanding."""
    result = run(FIX4, {"node/14156477686": {**CITED, "status": "announced"}})
    equinix = record(result, "node/14156477686")
    assert equinix.status == "operating" and is_expanding(equinix)
    assert [p.phase_id for p in equinix.phases] == ["osm-operating", "review-announced"]
    assert equinix.phases[1].name == "Announced per Example: Equinix Ashburn DC18"
    assert equinix.phases[1].source_ids == ["s3"]
    dc18 = next(b for b in equinix.buildings if b.ref == "osm:node/14156477686")
    assert dc18.phase_id == "review-announced"
    operating, announced = equinix.status_history
    assert (announced.status, announced.event, announced.as_of.value) == (
        "announced",
        "other",
        "2026-10-09",  # the snapshot date: it dates nothing
    )
    assert announced.source_ids == ["s3"] and announced.note is not None
    assert "Status per Example" in announced.note
    own = next(s.id for s in equinix.sources if str(s.url) == f"{OSM}node/14156477686")
    assert own not in operating.source_ids  # the node's tags no longer give a status
    assert equinix.field_meta["/status"].method == "imported"  # the operating phase leads
    assert "first_reported" not in equinix.dates
    valid(result, counties, today)


def test_status_as_of_dates_the_observation(run: Run, counties: CountyIndex, today: date) -> None:
    entry = {**CITED, "status": "announced", "status_as_of": "2026-09"}
    result = run(FIX4, {"node/14156477686": entry})
    history = record(result, "node/14156477686").status_history
    (announced,) = [e for e in history if e.phase_id == "review-announced"]
    assert announced.event == "other" and announced.as_of.value == "2026-09"
    assert announced.as_of.precision == "month"
    valid(result, counties, today)


@pytest.mark.parametrize(
    ("source_type", "confidence"),
    [("company_release", 0.99), ("commercial_aggregator", 0.65)],
)
def test_a_site_status_decides_its_members(
    run: Run, counties: CountyIndex, today: date, source_type: str, confidence: float
) -> None:
    """PowerHouse CTP-02's site polygon (way 1493522242) gives no status of its own; an entry on
    it decides its hall too, in place of telecom=data_center's operating, and the site's
    opening_date still adds the planned energized event. The status is the reviewer's (07 §3.4:
    a commercial aggregator over OpenStreetMap's tag is 0.95 - 0.10 - 0.20)."""
    entry = {**CITED, "status": "under_construction", "source_type": source_type}
    result = run(FIX6, {"way/1493522242": entry})
    power = record(result, "way/1493522242")
    assert power.status == "under_construction" and power.phases == []
    observed, planned = power.status_history
    assert (observed.status, observed.source_ids) == ("under_construction", ["s3"])
    assert (planned.event, planned.as_of.value, planned.planned) == ("energized", "2027", True)
    assert planned.source_ids == ["s1"]  # opening_date=2027 is the polygon's own tag
    meta = power.field_meta["/status"]
    assert (meta.method, meta.source_ids, meta.confidence) == ("human_entered", ["s3"], confidence)
    assert confidence == human_confidence(source_type, ["open_dataset"])
    # The construction-site rule's item is not filed: the reviewer decided.
    assert items(result, "conflict", "way/1493522242") == []
    valid(result, counties, today)


def test_a_reviewed_status_on_a_construction_site_is_not_regrouped(counties: CountyIndex) -> None:
    """A site not open yet regroups the members OSM reads as operating with it (fix round 6), but
    not a member an entry says operates: that is the reviewer's, and stays a phase of its own."""

    def area(ref: str, lat: float, lon: float, d: float, **tags: str) -> OsmObject:
        bounds = Bounds(lat - d, lon - d, lat + d, lon + d)
        return OsmObject(ref, lat, lon, bounds, tags, classify("way", tags))

    site = area(
        "way/10",
        39.01,
        -77.47,
        0.002,
        landuse="construction",
        industrial="data_centre",
        name="Example Campus",
        opening_date="2030",
    )
    hall = area("way/11", 39.0105, -77.4705, 0.0002, building="yes", telecom="data_center")
    built = area(
        "way/12", 39.0095, -77.4695, 0.0002, building="construction", telecom="data_center"
    )
    snapshot = InputSnapshot(
        name="overpass",
        url=HttpUrl("https://overpass-api.de/api/interpreter"),
        retrieved_at=datetime(2026, 10, 9, 10, 0, tzinfo=UTC),
        sha256="0" * 64,
        bytes=1,
        license="ODbL-1.0",
        upstream_version="2026-10-09T09:51:31Z",
    )
    entry = ElementOverride.model_validate({**CITED, "status": "operating"})
    result = build_candidates(
        dissolve([site, hall, built]),
        counties=counties,
        now=datetime(2026, 10, 12, 12, 0, tzinfo=UTC),
        snapshot=snapshot,
        overrides={"way/12": entry},
    )
    (campus,) = [c.record for c in result.candidates]
    assert [p.phase_id for p in campus.phases] == ["osm-under_construction", "review-operating"]
    phase_of = {b.ref: b.phase_id for b in campus.buildings}
    assert phase_of == {"osm:way/11": "osm-under_construction", "osm:way/12": "review-operating"}
    assert campus.status == "operating"
    (item,) = [i for i in result.review if i.kind == "conflict"]
    assert item.data["refs"] == ["way/11"]


def test_a_reviewed_announcement_is_not_a_lead(run: Run) -> None:
    """An unnamed building without an operator that OSM tags only as planned is held as a lead;
    one an entry says is announced has a citable source, so it is a record."""
    result = run(FIX6, {"way/45583037": {**CITED, "status": "announced"}})  # 7777 Infantry Ridge
    assert record(result, "way/45583037").status == "announced"
    assert items(result, "unverified_upstream", "way/45583037") == []


def test_the_members_a_status_entry_decides_are_cited_for_nothing_else(run: Run) -> None:
    """Digital Loudoun Plaza's two site outlines: an entry on one decides every member. The
    entry's source cites their status, so an element that gives no other value is no source."""
    result = run(FIX4, {"way/1020953461": {**CITED, "status": "under_construction"}})
    plaza = record(result, "way/1020953461")
    ((status, sids),) = {(e.status, tuple(e.source_ids)) for e in plaza.status_history}
    entry = next(s.id for s in plaza.sources if str(s.url) == PAGE)
    assert (status, sids) == ("under_construction", (entry,))
    osm = [s for s in plaza.sources if s.url.host == "www.openstreetmap.org"]
    assert [s.id for s in osm if not s.supports] == []


def test_a_status_the_tags_give_already_is_an_error(run: Run) -> None:
    with pytest.raises(FetchError, match="tags give operating already"):
        run(FIX4, {"way/460050155": {**CITED, "status": "operating"}})


def test_two_sites_giving_a_status_is_an_error(run: Run) -> None:
    """Digital Loudoun Plaza is one cluster of two site outlines (ways 1020953454, 1020953461)."""
    entries = {
        "way/1020953454": {**CITED, "status": "under_construction"},
        "way/1020953461": {**CITED, "status": "announced"},
    }
    with pytest.raises(FetchError, match="each give a status to the campus"):
        run(FIX4, entries)


def test_one_status_needs_one_status_as_of(run: Run) -> None:
    entries = {
        "way/596690175": {**CITED, "status": "announced", "status_as_of": "2026-08"},
        "way/793888408": {**CITED, "status": "announced", "status_as_of": "2026-09"},
    }
    with pytest.raises(FetchError, match="different status_as_of"):
        run(FIX4, entries)


def test_a_stopped_status_ranks_after_the_active_ones(
    run: Run, counties: CountyIndex, today: date
) -> None:
    entry = {**CITED, "status": "paused", "source_type": "news"}
    result = run(FIX4, {"node/14156477686": entry})
    equinix = record(result, "node/14156477686")
    assert [p.phase_id for p in equinix.phases] == ["osm-operating", "review-paused"]
    assert equinix.status == "operating"
    valid(result, counties, today)
    node = OsmObject("node/1", 39.0, -77.4, None, {"telecom": "data_center"}, "point")
    other = OsmObject("node/2", 39.0, -77.4, None, {"telecom": "data_center"}, "point")
    groups = _status_groups([node, other], {"node/1": ("node/1", "cancelled")})
    assert [(g.status.status, g.entries) for g in groups] == [
        ("operating", ()),
        ("cancelled", ("node/1",)),
    ]


def test_an_entry_status_keeps_its_first_seen_date(
    run: Run, ids: Callable[[], str], today: date, counties: CountyIndex
) -> None:
    """A later run keeps the earliest stored date of the entry's observation (one without a
    status_as_of), so weekly runs do not move it; one the entry dates keeps the entry's date."""
    entry = {**CITED, "status": "announced"}
    first = record(run(FIX4, {"node/14156477686": entry}), "node/14156477686")
    events = []
    for e in first.status_history:
        if e.phase_id == "review-announced":
            e = e.model_copy(update={"as_of": e.as_of.model_copy(update={"value": "2026-09-01"})})
        events.append(e)
    stored = first.model_copy(update={"id": ids(), "status_history": events})
    again = run(FIX4, {"node/14156477686": entry}, records={stored.id: stored})
    (kept,) = [
        e for e in record(again, "node/14156477686").status_history if e.status != "operating"
    ]
    assert kept.as_of.value == "2026-09-01"
    dated = run(
        FIX4,
        {"node/14156477686": {**entry, "status_as_of": "2026-10-01"}},
        records={stored.id: stored},
    )
    (given,) = [
        e for e in record(dated, "node/14156477686").status_history if e.status != "operating"
    ]
    assert given.as_of.value == "2026-10-01"
    # The OSM observation keeps its own stored date, untouched by the entry's; s3 is the
    # entry's source, its URL for an undated entry, None for a dated one.
    for urls, day in (({"s3": PAGE}, "2026-09-01"), ({"s3": None}, "2026-10-09")):
        kept_osm, kept_entry = _keep_first_reported(list(first.status_history), stored, urls)
        assert (kept_osm.as_of.value, kept_entry.as_of.value) == ("2026-10-09", day)


# ---------------------------------------------------------------------------- corrections


def test_corrections_apply_to_the_record_that_holds_the_element(
    run: Run, counties: CountyIndex, today: date
) -> None:
    """DFW17 (way/597876721) is a member of Digital Realty Dallas, led by way/597876730: its
    entry's corrections apply to that record."""
    op = {"op": "set", "path": "/location/postcode", "from": "75081", "value": "75080"}
    result = run(FIX5, {"way/597876721": {"corrections": [fix(op)]}})
    dallas = record(result, "way/597876721")
    assert dallas.location.postcode == "75080"
    assert dallas.field_meta["/location/postcode"].method == "human_entered"
    assert [c.pointer for c in dallas.corrections] == ["/location/postcode"]
    assert result.metrics["corrections_entries"] == 1 and result.metrics["records_corrected"] == 1
    assert result.metrics["corrections_ops"] == 1
    valid(result, counties, today)


def test_the_items_about_a_corrected_record_say_so(run: Run) -> None:
    """RagingWire CA3's street, '1625 National Driveq' in OpenStreetMap: its possible_duplicate
    item names the correction."""
    street = {
        "op": "set",
        "path": "/location/street",
        "from": "1625 National Driveq",
        "value": "1625 National Drive",
    }
    result = run(FIX5, {"way/190868312": {"corrections": [fix(street)]}})
    assert record(result, "way/190868312").location.street == "1625 National Drive"
    (item,) = items(result, "possible_duplicate", "way/190868312")
    assert str(item.data["corrected_by"]).endswith(
        "osm.json (reviewed 2026-10-10): The reviewer read the cited page"
    )
    assert [i for i in result.review if "corrected_by" in i.data] == [item]


def test_a_stale_correction_stops_the_run(run: Run) -> None:
    op = {"op": "set", "path": "/location/postcode", "from": "75080", "value": "75082"}
    with pytest.raises(FetchError, match=r"corrections\[0\]\.ops\[0\] \(set\).*stale"):
        run(FIX5, {"way/597876721": {"corrections": [fix(op)]}})


def test_corrections_on_a_held_cluster_stop_the_run(run: Run) -> None:
    """The Lakeside Technology Center's building is held as a carrier hotel (fix round 6): a
    correction of its record cannot apply, but an entry that only holds it is fine."""
    op = {"op": "set", "path": "/location/postcode", "from": "60616", "value": "60615"}
    with pytest.raises(
        FetchError, match=r"no record was built for way/156520409 \(held: possible_duplicate"
    ):
        run(FIX6, {"way/156520409": {"corrections": [fix(op)]}})
    result = run(FIX6, {"way/156520409": {"corrections": [fix({"op": "hold"})]}})
    assert not any("way/156520409" in c.match_values for c in result.candidates)


def test_a_join_lets_corrections_reach_a_held_element(run: Run) -> None:
    """With Digital Realty ORD10's node joined to it, the Lakeside building is a record."""
    op = {"op": "set", "path": "/location/postcode", "from": "60616", "value": "60615"}
    entries = {
        "node/10910879064": {**CITED, "join": "way/156520409"},
        "way/156520409": {"corrections": [fix(op)]},
    }
    lakeside = record(run(FIX6, entries), "way/156520409")
    assert lakeside.location.postcode == "60615" and lakeside.parties.operator[0].name == (
        "Digital Realty"
    )


def test_a_whole_site_out_of_scope(run: Run, counties: CountyIndex, today: date) -> None:
    result = run(FIX5, {"way/597876721": {"corrections": [fix({"op": "out_of_scope"})]}})
    dallas = record(result, "way/597876721")
    assert dallas.scope == "out_of_scope" and len(dallas.external_ids["osm"]) == 7
    (item,) = items(result, "out_of_scope", "way/597876730")  # the representative
    assert "osm.json entry 'way/597876721'" in item.reason
    assert item.data["source_url"] == "https://example.com/fix"
    valid(result, counties, today)


# ---------------------------------------------------------------------------- a populated store


def merge(
    result: ImportResult, ctx: ImportContext, root: Path
) -> tuple[dict[str, int], RecordStore]:
    store = RecordStore(root / "data" / "records")
    receipt, _ = merge_import(
        OsmImporter(),
        result,
        ctx,
        store=store,
        review_dir=root / "review" / "queue",
        receipts_dir=root / "data" / "imports",
    )
    return receipt.counts, store


def test_a_join_merges_stored_records(
    make_test_context: MakeContext, run: Run, tmp_repo: Path
) -> None:
    """On a populated store a join makes the older record take the campus and the other one
    merged into it (07 §6.6); the next run changes nothing."""
    ctx = make_test_context()
    counts, store = merge(run(FIX5, {}), ctx, tmp_repo)
    assert counts["new"] == 15
    before = store.load()
    (ca3,) = [r for r in before.values() if r.external_ids["osm"] == ["way/190868312"]]
    (ca12,) = [r for r in before.values() if "way/32560290" in r.external_ids["osm"]]
    older, younger = sorted((ca3, ca12), key=lambda r: (r.created_at, r.id))
    entries = {"way/190868312": {**CITED, "join": "way/32560290"}}
    for expected in ({"updated": 1, "merged": 1}, {"unchanged": 14, "merged": 0}):
        records = store.load()
        counts, store = merge(
            run(FIX5, entries, records=records), make_test_context(records=records), tmp_repo
        )
        assert {k: counts[k] for k in expected} == expected and counts["conflicts"] == 0
    after = store.load()
    survivor = after[older.id]
    assert survivor.external_ids["osm"] == ["way/32560290", "way/32560292", "way/190868312"]
    assert survivor.merged_into is None and after[younger.id].merged_into == older.id
    assert counts["removed_upstream"] == 0


def test_an_alone_element_splits_a_stored_record(
    make_test_context: MakeContext, run: Run, tmp_repo: Path
) -> None:
    ctx = make_test_context()
    _, store = merge(run(FIX5, {}), ctx, tmp_repo)
    records = store.load()
    entries = {"way/32560292": {**CITED, "alone": True}}
    counts, store = merge(
        run(FIX5, entries, records=records), make_test_context(records=records), tmp_repo
    )
    assert (counts["new"], counts["updated"], counts["conflicts"]) == (1, 1, 0)
    ntt = [r for r in store.load().values() if r.external_ids["osm"][0].startswith("way/3256")]
    assert sorted(r.external_ids["osm"] for r in ntt) == [["way/32560290"], ["way/32560292"]]
