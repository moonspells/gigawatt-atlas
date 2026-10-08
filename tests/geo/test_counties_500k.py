"""County point-in-polygon uses the Census 1:500,000 file (SV-2, A11).

The four points are the building centroids of Amazon IAD100-IAD103 in the PNNL IM3 Open Source
Data Center Atlas v2026.02.09 (DOI 10.57931/3017294, ODbL 1.0), which gives Prince William
County for all four; the Census Geocoder (geographies/coordinates, Public_AR_Current, checked
2026-10-08) returns 51153 Prince William County for each. The 1:5,000,000 file puts them in
Manassas city (51683).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from atlas.geo.counties import (
    COUNTIES_5M_SHA256,
    COUNTIES_5M_ZIP,
    COUNTIES_SHA256,
    COUNTIES_ZIP,
    KNOWN_COUNTY_FILES,
    CountyIndex,
)
from atlas.jsonio import record_json
from atlas.schema.record import FacilityRecord
from atlas.validate import validate_record

MakeRecord = Callable[..., FacilityRecord]
PRINCE_WILLIAM = "51153"
MANASSAS_CITY = "51683"
IAD = {
    "IAD100": (38.734598, -77.506642),
    "IAD101": (38.735440, -77.505695),
    "IAD102": (38.735970, -77.504901),
    "IAD103": (38.736984, -77.504075),
}


@pytest.fixture(scope="module")
def counties_5m(repo_root: Path) -> Iterator[CountyIndex]:
    index = CountyIndex.load(repo_root / COUNTIES_5M_ZIP)
    yield index
    index.close()


def test_the_committed_file_is_the_census_release(repo_root: Path) -> None:
    path = repo_root / COUNTIES_ZIP
    assert path.name == "cb_2025_us_county_500k.zip"
    assert path.stat().st_size == 11_758_981
    assert hashlib.sha256(path.read_bytes()).hexdigest() == COUNTIES_SHA256
    readme = (repo_root / "reference" / "README.md").read_text(encoding="utf-8")
    assert COUNTIES_SHA256 in readme and "11,758,981" in readme


def test_the_default_index_is_the_500k_file(counties: CountyIndex) -> None:
    assert len(counties) == 3235
    assert set(KNOWN_COUNTY_FILES) == {COUNTIES_SHA256, COUNTIES_5M_SHA256}


@pytest.mark.parametrize("name", sorted(IAD))
def test_iad100_to_iad103_are_in_prince_william(counties: CountyIndex, name: str) -> None:
    lat, lon = IAD[name]
    county = counties.lookup(lat, lon)
    assert county is not None and county.fips == PRINCE_WILLIAM
    assert counties.contains(PRINCE_WILLIAM, lat, lon)
    assert counties.contains(PRINCE_WILLIAM, lat, lon, tolerance_deg=0)


def test_the_5m_file_misplaces_them(counties_5m: CountyIndex) -> None:
    """Why the 5m file is not used for rule 5: two of the four fail even with the tolerance."""
    got = counties_5m.lookup_many(list(IAD.values()))
    assert [c.fips if c else None for c in got] == [MANASSAS_CITY] * 4
    inside = {name: counties_5m.contains(PRINCE_WILLIAM, *IAD[name]) for name in IAD}
    assert not inside["IAD102"] and not inside["IAD103"]


def iad_record(make_record: MakeRecord, name: str, fips: str) -> FacilityRecord:
    lat, lon = IAD[name]
    loc = record_json(make_record())["location"]
    assert isinstance(loc, dict)
    loc.update(lat=lat, lon=lon, county_fips=fips, county_name=None, city="Manassas")
    return make_record(location=loc)


@pytest.mark.parametrize("name", sorted(IAD))
def test_rule_5_accepts_the_right_county(
    make_record: MakeRecord, counties: CountyIndex, counties_5m: CountyIndex, name: str
) -> None:
    """A reviewer who sets county_fips 51153 is no longer failed by atlas validate."""
    today = date(2026, 10, 12)
    r = iad_record(make_record, name, PRINCE_WILLIAM)
    assert validate_record(r, counties=counties, today=today) == []
    if name in ("IAD102", "IAD103"):
        issues = validate_record(r, counties=counties_5m, today=today)
        assert [i.rule for i in issues] == ["geo"]


def test_the_5m_file_still_loads_for_display(counties_5m: CountyIndex) -> None:
    assert len(counties_5m) == 3235
    county = counties_5m.get(PRINCE_WILLIAM)
    assert county is not None and county.name == "Prince William"


def test_load_refuses_an_unknown_file(tmp_path: Path) -> None:
    bad = tmp_path / "cb_2025_us_county_500k.zip"
    bad.write_bytes(b"not the county file")
    with pytest.raises(ValueError, match=f"not a known Census county file.*{COUNTIES_SHA256}"):
        CountyIndex.load(bad)


def test_load_without_the_check_names_the_shapefile_after_the_zip(repo_root: Path) -> None:
    index = CountyIndex.load(repo_root / COUNTIES_ZIP, verify_sha256=False)
    try:
        county = index.lookup(*IAD["IAD100"])
        assert county is not None and county.fips == PRINCE_WILLIAM
    finally:
        index.close()


def test_a_shared_vertex_goes_to_the_lower_fips(counties: CountyIndex) -> None:
    """(39.051695, -77.334249) is a vertex of both Fairfax (51059) and Loudoun (51107)."""
    point: Any = (39.051695, -77.334249)
    assert counties.contains("51059", *point, tolerance_deg=0)
    assert counties.contains("51107", *point, tolerance_deg=0)
    county = counties.lookup(*point)
    assert county is not None and county.fips == "51059"


def test_atlas_validate_uses_the_500k_file_by_default(
    tmp_path: Path,
    make_record: MakeRecord,
    fixture_orgs_path: Path,
    repo_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from atlas.cli import main
    from atlas.store import RecordStore

    records = tmp_path / "records"
    RecordStore(records).write(iad_record(make_record, "IAD103", PRINCE_WILLIAM))
    schema = str(repo_root / "schema" / "facility.v1.json")
    args = ["validate", "--records", str(records), "--orgs", str(fixture_orgs_path)]
    assert main([*args, "--schema", schema, "--today", "2026-10-12"]) == 0
    assert capsys.readouterr().out.endswith(", 0 issues\n")
    five_m = str(repo_root / COUNTIES_5M_ZIP)
    assert main([*args, "--schema", schema, "--today", "2026-10-12", "--counties", five_m]) == 1
    assert "[geo]" in capsys.readouterr().out
