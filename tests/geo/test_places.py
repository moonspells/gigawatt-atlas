"""atlas.geo.places: the Census place that contains a point (07 §6.5).

The sample (tests/fixtures/geocode/places/places_sample.zip) holds eight polygons of the Census
2025 cartographic boundary file cb_2025_us_place_500k (public domain). The points are Census
Geocoder matches of the seed import of 2026-10-08, whose USPS city is not the place they lie in.
"""

from __future__ import annotations

import hashlib
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from atlas.geo.duck import connect
from atlas.geo.places import (
    PLACE_MARGIN_DEG,
    PLACE_POLYGONS_BYTES,
    PLACE_POLYGONS_SHA256,
    PLACE_POLYGONS_ZIP,
    PlaceIndex,
)

SAMPLE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "geocode" / "places" / "places_sample.zip"
)
QTS_CEDAR_RAPIDS = (41.905594, -91.751082)  # USPS city Fairfax; 5 m inside Cedar Rapids city
GOOGLE_CEDAR_RAPIDS = (41.921946, -91.71563)  # 621 m inside Cedar Rapids city
STARGATE_LORDSTOWN = (41.143721, -80.883293)  # USPS city Warren
STACK_NVA02 = (38.749819, -77.535848)  # USPS city Manassas
GOOGLE_OMAHA = (41.335728, -96.088471)  # USPS city Omaha, in no place
VANTAGE_TX1 = (29.416684, -98.788564)  # USPS city San Antonio, in no place
LAKE_MARINER = (43.34894, -78.603357)  # USPS city Barker; Somerset town, in no place


@pytest.fixture(scope="module")
def sample() -> Iterator[PlaceIndex]:
    index = PlaceIndex.load(SAMPLE, verify_sha256=False)
    yield index
    index.close()


def test_the_place_that_contains_a_point(sample: PlaceIndex) -> None:
    assert len(sample) == 8
    cedar_rapids = sample.containing(*GOOGLE_CEDAR_RAPIDS)
    assert cedar_rapids is not None
    assert (cedar_rapids.geoid, cedar_rapids.name, cedar_rapids.lsad_name) == (
        "1912000",
        "Cedar Rapids",
        "Cedar Rapids city",
    )
    assert cedar_rapids.incorporated and cedar_rapids.state_abbr == "IA"
    innovation = sample.containing(*STACK_NVA02)
    assert innovation is not None and innovation.lsad_name == "Innovation CDP"
    assert not innovation.incorporated
    assert sample.containing(*GOOGLE_OMAHA) is None  # Omaha city is 1.6 km away
    assert sample.get("3944912") == sample.containing(*STARGATE_LORDSTOWN)


def test_a_point_near_a_generalized_boundary_names_no_place(sample: PlaceIndex) -> None:
    # QTS Cedar Rapids is 5 m inside Cedar Rapids city on the 1:500,000 lines: too close to tell.
    # (Microsoft Fairwater Atlanta is 5 m inside Fayetteville city on them, but outside it on the
    # Census Geocoder's lines.)
    assert sample.containing(*QTS_CEDAR_RAPIDS) is None
    assert sample.containing(*QTS_CEDAR_RAPIDS, margin_deg=0.0) == sample.get("1912000")
    assert PLACE_MARGIN_DEG == 0.001


def test_city_at_names_the_place_in_the_state(sample: PlaceIndex) -> None:
    assert sample.city_at(*GOOGLE_CEDAR_RAPIDS) == "Cedar Rapids"
    assert sample.city_at(*GOOGLE_CEDAR_RAPIDS, "ia") == "Cedar Rapids"
    assert sample.city_at(*GOOGLE_CEDAR_RAPIDS, "NE") is None
    assert sample.city_at(*STARGATE_LORDSTOWN, "OH") == "Lordstown"
    assert sample.city_at(*STACK_NVA02, "VA") == "Innovation"
    assert sample.city_at(*GOOGLE_OMAHA, "NE") is None
    assert sample.containing_many([GOOGLE_CEDAR_RAPIDS, GOOGLE_OMAHA, STACK_NVA02]) == [
        sample.get("1912000"),
        None,
        sample.get("5139916"),
    ]
    assert sample.containing_many([]) == []


def write_places(path: Path, rows: list[tuple[str, str, str, str, str]]) -> Path:
    """A place zip with the Census columns: (GEOID, NAME, NAMELSAD, LSAD, polygon WKT)."""
    con = connect()
    con.execute(
        "CREATE TABLE t (GEOID VARCHAR, NAME VARCHAR, NAMELSAD VARCHAR, LSAD VARCHAR,"
        " STUSPS VARCHAR, geom GEOMETRY)"
    )
    for geoid, name, lsad_name, lsad, wkt in rows:
        con.execute(
            "INSERT INTO t VALUES (?, ?, ?, ?, 'XX', ST_GeomFromText(?))",
            [geoid, name, lsad_name, lsad, wkt],
        )
    shp = path / "overlap.shp"
    con.execute(f"COPY t TO '{shp}' WITH (FORMAT GDAL, DRIVER 'ESRI Shapefile')")
    con.close()
    out = path / "overlap.zip"
    with zipfile.ZipFile(out, "w") as z:
        for ext in ("shp", "shx", "dbf"):
            z.write(path / f"overlap.{ext}", f"overlap.{ext}")
    return out


def test_a_point_two_places_contain_is_ambiguous(tmp_path: Path) -> None:
    path = write_places(
        tmp_path,
        [
            ("9900001", "West", "West city", "25", "POLYGON((0 30, 2 30, 2 32, 0 32, 0 30))"),
            ("9900002", "East", "East city", "25", "POLYGON((2 30, 4 30, 4 32, 2 32, 2 30))"),
        ],
    )
    index = PlaceIndex.load(path, verify_sha256=False)
    assert index.city_at(31.0, 1.0) == "West"
    assert index.containing(31.0, 2.0) is None  # on the shared boundary
    assert index.containing(31.0, 5.0) is None
    index.close()


def test_load_checks_the_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sha256"):
        PlaceIndex.load(SAMPLE)
    with pytest.raises(FileNotFoundError):
        PlaceIndex.load(tmp_path / "missing.zip")


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[2] / PLACE_POLYGONS_ZIP).exists(),
    reason="reference/census/cb_2025_us_place_500k.zip is not committed yet",
)
def test_the_reference_file_is_the_census_release(repo_root: Path) -> None:
    path = repo_root / PLACE_POLYGONS_ZIP
    assert path.stat().st_size == PLACE_POLYGONS_BYTES
    assert hashlib.sha256(path.read_bytes()).hexdigest() == PLACE_POLYGONS_SHA256
    readme = (repo_root / "reference" / "README.md").read_text(encoding="utf-8")
    assert PLACE_POLYGONS_SHA256 in readme
    index = PlaceIndex.load()
    assert len(index) == 32_629
    assert index.city_at(*GOOGLE_CEDAR_RAPIDS, "IA") == "Cedar Rapids"
    assert index.city_at(*STARGATE_LORDSTOWN, "OH") == "Lordstown"
    assert index.containing(33.445341, -84.525447) is None  # Fairwater Atlanta, see above
    assert index.city_at(*STACK_NVA02, "VA") == "Innovation"
    for point in (GOOGLE_OMAHA, VANTAGE_TX1, LAKE_MARINER):
        assert index.containing(*point) is None
    index.close()
