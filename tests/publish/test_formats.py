"""The release file renderers and small helpers of atlas.publish (07 §11.1)."""

from __future__ import annotations

import csv
import gzip
import io
import json
import shlex
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from atlas import publish
from atlas.jsonio import dumps_compact
from atlas.publish import (
    CSV_COLUMNS,
    MAP_KEYS,
    Facility,
    build_summary,
    flat_row,
    geo_metadata,
    gzip_bytes,
    iso_z,
    make_facility,
    map_properties,
    pmtiles_summary,
    public_record,
    rec_object,
    release_now,
    release_time,
    render_csv,
    render_geojson,
    render_map,
    render_records,
)
from atlas.schema.record import FacilityRecord
from atlas.store import RecordStore, load_orgs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RECORDS = FIXTURES / "records"
ASHBURN = "gwa-01m47854008j5vt37xkj5ag72d"
VANTAGE = "gwa-01m4785401mdczvghe70mfz68q"
GOODYEAR = "gwa-01m4785403r3skww5dz30ye0t4"
STATE_ONLY = "gwa-01m4785405ksmnvwymzaxynj4x"
OUT_OF_SCOPE = "gwa-01m4785408jrs4nmnq9bthqa2h"
COMMITTED = publish.REPO_ROOT / "fixtures" / "release" / "v" / "20000101-0000"


@pytest.fixture(scope="module")
def records() -> dict[str, FacilityRecord]:
    return RecordStore(RECORDS).load()


@pytest.fixture(scope="module")
def facilities(records: dict[str, FacilityRecord]) -> list[Facility]:
    return [
        make_facility(public_record(r))
        for _, r in sorted(records.items())
        if r.scope == "in_scope" and r.merged_into is None
    ]


@pytest.fixture(scope="module")
def org_kinds() -> dict[str, str]:
    return {o.id: o.kind for o in load_orgs(FIXTURES / "orgs.json")}


def by_id(facilities: list[Facility], rid: str) -> Facility:
    return next(f for f in facilities if f.record.id == rid)


def test_release_ids() -> None:
    assert release_now(datetime(2026, 10, 12, 7, 5, 59, tzinfo=UTC)) == "20261012-0705"
    est = timezone(timedelta(hours=-4))
    assert release_now(datetime(2026, 10, 12, 22, 30, tzinfo=est)) == "20261013-0230"
    assert release_time("20000101-0000") == datetime(2000, 1, 1, tzinfo=UTC)
    for bad in ("2026-10-12", "20261012-12", "20261301-0000", "20261012-2460"):
        with pytest.raises(
            ValueError, match=r"YYYYMMDD-HHMM|does not match|out of range|unconverted"
        ):
            release_time(bad)
    assert iso_z(datetime(2026, 10, 12, 12, 0, 0, 123456, tzinfo=UTC)) == "2026-10-12T12:00:00Z"


def test_gzip_is_reproducible() -> None:
    a = gzip_bytes(b"x\n" * 100)
    assert a == gzip_bytes(b"x\n" * 100)
    assert a[4:8] == b"\0\0\0\0"
    assert gzip.decompress(a) == b"x\n" * 100


def test_public_record_refuses_out_of_scope(records: dict[str, FacilityRecord]) -> None:
    with pytest.raises(ValueError, match="out of scope"):
        public_record(records[OUT_OF_SCOPE])


def test_public_record_strips_keys_and_values(records: dict[str, FacilityRecord]) -> None:
    record = records[ASHBURN]
    planted = record.model_copy(
        update={
            "canonical_name": "​Example⁦ Campus﻿",
            "external_ids": {"o‍sm": ["way/990000001"]},
        }
    )
    public = public_record(planted)
    assert public.canonical_name == "Example Campus"
    assert public.external_ids == {"osm": ["way/990000001"]}
    data, h = rec_object(public)
    assert len(h) == 12
    assert "​".encode() not in data


def test_flat_row_columns(facilities: list[Facility]) -> None:
    row = by_id(facilities, VANTAGE).row
    assert tuple(row) == CSV_COLUMNS
    assert row["mw_display"] == 96.0 and row["mw_basis"] == "it"
    assert row["application_filed"] == "2026"
    assert row["expected_in_service"] == "2028-03-30"
    assert row["construction_start"] is None  # a planned event does not count
    assert row["latest_event"] == "application_filed"
    assert row["latest_event_date"] == "2026"
    assert row["operator"] == "Vantage Data Centers"
    assert row["state"] == "TX"
    assert row["url"] == f"https://moonspells.dev/atlas/facility/{VANTAGE}/"
    state_only = by_id(facilities, STATE_ONLY).row
    assert state_only["lat"] is None and state_only["lon"] is None
    assert state_only["mw_display"] == 500.0 and state_only["mw_basis"] == "facility"


def test_flat_row_uses_the_status_confidence(records: dict[str, FacilityRecord]) -> None:
    record = records[ASHBURN]
    data = record.model_dump(mode="json")
    data["field_meta"]["/status"] = {"confidence": 0.6, "method": "derived", "source_ids": []}
    with_meta = FacilityRecord.model_validate(data)
    row = flat_row(with_meta, json.loads(rec_object(with_meta)[0]))
    assert row["confidence_status"] == 0.6
    assert flat_row(record, json.loads(rec_object(record)[0]))["confidence_status"] is None


def test_map_properties(facilities: list[Facility], org_kinds: dict[str, str]) -> None:
    props = map_properties(by_id(facilities, ASHBURN), org_kinds)
    assert tuple(props) == MAP_KEYS
    assert props["ot"] == "colocation"
    assert map_properties(by_id(facilities, ASHBURN), {})["ot"] is None
    goodyear = map_properties(by_id(facilities, GOODYEAR), org_kinds)
    assert goodyear["s"] == "operating"
    assert goodyear["x"] is True  # an operating campus with a phase still in the pipeline
    nameless = map_properties(by_id(facilities, STATE_ONLY), org_kinds)
    assert nameless["o"] is None and nameless["ot"] is None and nameless["a"] is None


def test_render_map_is_compact_and_within_budget(
    facilities: list[Facility], org_kinds: dict[str, str]
) -> None:
    data = render_map("20261012-1200", facilities, org_kinds)
    assert b"\n" not in data and b": " not in data
    doc = json.loads(data)
    assert list(doc) == ["features", "release", "type"]
    assert len(gzip_bytes(data)) <= publish.MAP_BUDGET_GZIP_BYTES


def test_render_csv_cells(facilities: list[Facility]) -> None:
    text = render_csv(facilities).decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    first = rows[0]
    assert first["expanding"] in ("true", "false")
    assert first["owner"] == ""  # None is an empty cell
    assert first["name"] == "Example Colocation Ashburn Campus (Ashburn, VA)"  # quoted, comma kept
    assert len(rows) == len(facilities)


@pytest.mark.parametrize(
    "name",
    [
        '=HYPERLINK("https://evil.example/x","Ashburn")',
        "+1+cmd|' /C calc'!A0",
        "-2+3",
        "@SUM(1+1)",
        "\t=1+1",
        "\r=1+1",
        "\uff1d1+1",  # full-width =
        "\uff0b1",
        "\uff0d1",
        "\uff20SUM(1)",
    ],
)
def test_render_csv_neutralizes_formula_cells(
    facilities: list[Facility], records: dict[str, FacilityRecord], name: str
) -> None:
    planted = make_facility(
        public_record(records[ASHBURN].model_copy(update={"canonical_name": name}))
    )
    others = [f for f in facilities if f.record.id != ASHBURN]
    rows = list(csv.DictReader(io.StringIO(render_csv([planted, *others]).decode("utf-8"))))
    row = next(r for r in rows if r["id"] == ASHBURN)
    assert row["name"] == "'" + name  # shown as text, never run as a formula
    assert row["lon"] == "-77.4874"  # numbers keep their sign
    # Only the CSV is escaped; the GeoJSON and the map keep the name as stored.
    feature = json.loads(render_geojson([planted]))["features"][0]
    assert feature["properties"]["name"] == name


def test_csv_text_cells_that_stay_as_they_are() -> None:
    for value in ("Example Campus", "2026-07", "", "a=b", " =1", "Vantage | -"):
        assert publish.csv_text_cell(value) == value


def test_render_geojson(facilities: list[Facility]) -> None:
    doc = json.loads(render_geojson(facilities))
    assert doc["type"] == "FeatureCollection"
    features = doc["features"]
    assert [f["properties"]["id"] for f in features] == [f.record.id for f in facilities]
    assert set(features[0]["properties"]) == set(CSV_COLUMNS)
    nulls = [f for f in features if f["geometry"] is None]
    assert [f["properties"]["id"] for f in nulls] == [STATE_ONLY]
    for f in features:
        if f["geometry"] is not None:
            lon, lat = f["geometry"]["coordinates"]
            assert (lon, lat) == (f["properties"]["lon"], f["properties"]["lat"])


def test_render_records_lines(records: dict[str, FacilityRecord]) -> None:
    public = [public_record(r) for r in records.values() if r.scope == "in_scope"]
    raw = gzip.decompress(render_records(list(reversed(public)))).decode("utf-8")
    assert raw.endswith("\n")
    lines = raw.splitlines()
    assert [json.loads(line)["id"] for line in lines] == sorted(r.id for r in public)
    assert all(line.encode() == dumps_compact(json.loads(line)) for line in lines)


def test_summary_math(facilities: list[Facility]) -> None:
    summary: dict[str, Any] = build_summary("20261012-1200", "2026-10-12T12:00:00Z", facilities, 1)
    assert summary["facilities"] == 7
    assert summary["gw_total"] == 2.052  # 36 + 96 + 1000 + 120 + 300 + 500 MW
    assert summary["gw_utility_request_basis"] == 1.3
    assert summary["by_status"]["operating"] == {"count": 2, "gw": 0.156}
    assert sorted(summary["by_status"]) == sorted(publish.STATUSES)
    assert summary["by_group"]["pl"] == {"count": 2, "gw": 1.096}
    assert summary["by_state"]["TX"]["count"] == 2
    assert summary["by_state"]["TX"]["by_status"]["paused"] == 1
    assert summary["by_iso"]["ERCOT"] == {"count": 2, "gw": 0.596}
    assert summary["by_evidence"] == {"confirmed": 5, "reported": 1, "rumor": 1}
    assert summary["last_updated"] == "2026-10-06T00:00:00Z"
    empty = build_summary("20261012-1200", "2026-10-12T12:00:00Z", [], 0)
    assert empty["gw_total"] == 0.0 and empty["last_updated"] is None


def test_summary_last_updated_compares_instants(records: dict[str, FacilityRecord]) -> None:
    # 09:00-05:00 is 14:00Z, later than 10:00Z, although the string sorts first.
    stamps = {ASHBURN: "2026-10-06T10:00:00Z", VANTAGE: "2026-10-06T09:00:00-05:00"}
    planted = [
        make_facility(
            public_record(
                FacilityRecord.model_validate(
                    dict(records[rid].model_dump(mode="json"), updated_at=stamp)
                )
            )
        )
        for rid, stamp in stamps.items()
    ]
    summary = build_summary("20261012-1200", "2026-10-12T12:00:00Z", planted, 0)
    assert summary["last_updated"] == "2026-10-06T14:00:00Z"


def test_geo_metadata_bbox(facilities: list[Facility]) -> None:
    geo = geo_metadata(facilities)
    points = [f.lonlat for f in facilities if f.lonlat]
    assert geo["columns"]["geometry"]["bbox"] == [
        min(p[0] for p in points),
        min(p[1] for p in points),
        max(p[0] for p in points),
        max(p[1] for p in points),
    ]
    assert "bbox" not in geo_metadata([])["columns"]["geometry"]


def test_pmtiles_summary_reads_the_committed_fixture() -> None:
    summary = pmtiles_summary(COMMITTED / "facilities.pmtiles")
    assert summary["max_zoom"] == 12
    assert summary["addressed_tiles"] > 0
    assert "generator_options" not in summary["metadata"]
    assert summary["metadata"]["vector_layers"][0]["id"] == "facilities"


def test_pmtiles_attribution_credits_every_source() -> None:
    # facilities.pmtiles carries names, MW and dates from all four sources (07 §5.1, §11.1).
    for credit in (
        "Gigawatt Atlas (ODbL)",
        "© OpenStreetMap contributors",
        "PNNL IM3",
        "Epoch AI (CC BY 4.0)",
        "AI GridWatch (CC BY 4.0)",
    ):
        assert credit in publish.TILES_ATTRIBUTION
    args = publish.TIPPECANOE_ARGS
    assert args[args.index("-A") + 1] == publish.TILES_ATTRIBUTION
    meta = pmtiles_summary(COMMITTED / "facilities.pmtiles")["metadata"]
    assert meta["attribution"] == publish.TILES_ATTRIBUTION


def test_committed_archive_was_built_with_these_tippecanoe_args() -> None:
    # CI has no tippecanoe, so a change to TIPPECANOE_ARGS must come with a rebuilt fixture
    # archive; tippecanoe records its command line in the metadata.
    meta = publish.pmtiles_metadata(COMMITTED / "facilities.pmtiles")
    assert meta["generator"] == "tippecanoe v2.79.0"
    assert meta["generator_options"] == shlex.join(publish.tippecanoe_argv())


def test_pmtiles_summary_refuses_other_files(tmp_path: Path) -> None:
    path = tmp_path / "x.pmtiles"
    path.write_bytes(b"not a pmtiles archive" * 10)
    with pytest.raises(ValueError, match="not a PMTiles v3 archive"):
        pmtiles_summary(path)


def test_tippecanoe_missing_is_a_clear_error(
    tmp_path: Path, facilities: list[Facility], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(publish, "tippecanoe_path", lambda: None)
    with pytest.raises(publish.ReleaseError, match="--skip-pmtiles"):
        publish.write_pmtiles(tmp_path / "facilities.pmtiles", facilities)


def test_layers_shape() -> None:
    layers = json.loads((publish.REPO_ROOT / "overlays" / "out" / "layers.json").read_text("utf-8"))
    assert publish.check_layers(layers) == []
    assert layers["basemap"]["url"] == (
        "https://tiles.moonspells.dev/basemap/conus-z10-20261006.pmtiles"
    )
    assert publish.check_layers({}) == ["must be a non-empty JSON object of layers"]
    bad = {"basemap": dict(layers["basemap"], url="http://x", frozen="no", asOf="2026")}
    assert len(publish.check_layers(bad)) == 3
    assert publish.check_layers({"basemap": {"url": "https://x"}})


def test_layers_allow_additive_keys_and_site_paths() -> None:
    # Contract 1 grows by additions (07 §11.3); metric JSON lives on the site (07 §10.3).
    layers = json.loads((publish.REPO_ROOT / "overlays" / "out" / "layers.json").read_text("utf-8"))
    metric = {
        "url": "/atlas/data/electricity-price.json",
        "asOf": "2025-12-31",
        "label": "Industrial electricity price",
        "attribution": "EIA",
        "frozen": False,
        "kind": "metric",
    }
    extra = dict(layers["basemap"], minzoom=0)
    assert publish.check_layers({"basemap": extra, "electricity-price": metric}) == []
    for url in ("/atlas/data/../x.json", "/etc/passwd", "atlas/data/x.json", "//evil.example/x"):
        assert publish.check_layers({"m": dict(metric, url=url)}) == [
            "layer 'm': url must be https or /atlas/data/{layer}.json"
        ]
    missing = {k: v for k, v in metric.items() if k != "frozen"}
    assert publish.check_layers({"m": missing}) == [
        "layer 'm' must have asOf, attribution, frozen, label, url"
    ]


def test_renderers_sort_by_id(facilities: list[Facility], org_kinds: dict[str, str]) -> None:
    shuffled = list(reversed(facilities))
    assert render_csv(shuffled) == render_csv(facilities)
    assert render_geojson(shuffled) == render_geojson(facilities)
    assert render_map("r", shuffled, org_kinds) == render_map("r", facilities, org_kinds)
