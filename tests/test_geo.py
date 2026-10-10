from __future__ import annotations

from pathlib import Path

import pytest

from atlas.geo.counties import COUNTIES_SHA256, CountyIndex
from atlas.geo.fips import IN_SCOPE, STATES, state_by_abbr, state_by_fips


def test_states_and_scope() -> None:
    assert len(STATES) == 56
    assert len(IN_SCOPE) == 51
    assert "DC" in IN_SCOPE
    assert not {"PR", "GU", "VI", "AS", "MP"} & IN_SCOPE
    va = state_by_abbr["VA"]
    assert (va.fips, va.name, va.in_scope) == ("51", "Virginia", True)
    assert state_by_abbr("XX") is None
    assert state_by_fips("72") == state_by_abbr["PR"]
    with pytest.raises(KeyError):
        state_by_fips["99"]


def test_county_file_matches_the_fips_table(counties: CountyIndex) -> None:
    assert len(counties) == 3235
    for county in counties.all():
        state = state_by_abbr(county.state_abbr)
        assert state is not None, county
        assert state.fips == county.state_fips == county.fips[:2]
    assert {c.state_abbr for c in counties.all()} == {s.abbr for s in STATES}


@pytest.mark.parametrize(
    ("lat", "lon", "fips", "abbr"),
    [
        (39.0438, -77.4874, "51107", "VA"),
        (34.99949, -90.04298, "47157", "TN"),
        (29.420749, -98.792034, "48029", "TX"),
    ],
)
def test_lookup(counties: CountyIndex, lat: float, lon: float, fips: str, abbr: str) -> None:
    county = counties.lookup(lat, lon)
    assert county is not None
    assert (county.fips, county.state_abbr) == (fips, abbr)


def test_puerto_rico_is_found_but_out_of_scope(counties: CountyIndex) -> None:
    county = counties.lookup(18.4, -66.1)
    assert county is not None
    assert county.state_abbr == "PR"
    assert county.state_abbr not in IN_SCOPE


def test_lookup_outside_every_county(counties: CountyIndex) -> None:
    assert counties.lookup(40.0, -30.0) is None


def test_lookup_many_is_one_join(counties: CountyIndex) -> None:
    points = [(39.0438, -77.4874), (40.0, -30.0), (34.99949, -90.04298)]
    got = counties.lookup_many(points)
    assert [c.fips if c else None for c in got] == ["51107", None, "47157"]
    assert counties.lookup_many([]) == []


def test_contains_and_tolerance(counties: CountyIndex) -> None:
    assert counties.contains("51107", 39.0438, -77.4874)
    assert not counties.contains("51059", 39.0438, -77.4874)  # Fairfax
    assert not counties.contains("99999", 39.0438, -77.4874)
    # Walk west from the centroid to Loudoun's boundary; 0.001 degrees past it is outside the
    # polygon but inside the default 0.003-degree tolerance.
    lat, lon = counties.centroid("51107")
    inside, outside = lon, lon - 2.0
    assert not counties.contains("51107", lat, outside, tolerance_deg=0)
    for _ in range(40):
        mid = (inside + outside) / 2
        if counties.contains("51107", lat, mid, tolerance_deg=0):
            inside = mid
        else:
            outside = mid
    just_out = outside - 0.001
    assert not counties.contains("51107", lat, just_out, tolerance_deg=0)
    assert counties.contains("51107", lat, just_out)


def test_in_state(counties: CountyIndex) -> None:
    assert counties.in_state("VA", 39.0438, -77.4874)
    assert not counties.in_state("MD", 39.0438, -77.4874)
    assert counties.in_state("WY", 41.128275, -104.799155)


@pytest.mark.parametrize(
    ("abbr", "name", "fips"),
    [
        ("VA", "Loudoun", "51107"),
        ("VA", "loudoun county", "51107"),
        ("LA", "Richland Parish", "22083"),
        ("AK", "Juneau City and Borough", "02110"),
        ("AK", "Juneau", "02110"),
        ("MD", "Baltimore city", "24510"),
        ("MD", "Baltimore County", "24005"),
        ("MD", "Baltimore", "24005"),
        ("VA", "City of Richmond", "51760"),
        ("VA", "Richmond", "51159"),
        ("MO", "St. Louis County", "29189"),
        ("NV", "Carson City", "32510"),
    ],
)
def test_by_name(counties: CountyIndex, abbr: str, name: str, fips: str) -> None:
    county = counties.by_name(abbr, name)
    assert county is not None
    assert county.fips == fips


def test_by_name_unknown(counties: CountyIndex) -> None:
    assert counties.by_name("VA", "Atlantis") is None
    assert counties.by_name("ZZ", "Loudoun") is None


@pytest.mark.parametrize("fips", ["51107", "48029", "02110", "13077", "72127"])
def test_centroid_is_inside_its_county(counties: CountyIndex, fips: str) -> None:
    lat, lon = counties.centroid(fips)
    assert (lat, lon) == (round(lat, 6), round(lon, 6))
    found = counties.lookup(lat, lon)
    assert found is not None and found.fips == fips
    with pytest.raises(KeyError):
        counties.centroid("99999")


def test_load_checks_the_sha256(tmp_path: Path) -> None:
    bad = tmp_path / "cb_2025_us_county_5m.zip"
    bad.write_bytes(b"not the county file")
    with pytest.raises(ValueError, match=COUNTIES_SHA256):
        CountyIndex.load(bad)
    with pytest.raises(FileNotFoundError):
        CountyIndex.load(tmp_path / "missing.zip")
