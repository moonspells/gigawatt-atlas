"""atlas.geocode: the 07 §6.5 chain, the Census Geocoder client and the Gazetteer."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from atlas.geo.counties import CountyIndex
from atlas.geocode import (
    CENSUS_MAX_ADDRESS,
    COUNTY_CHECKED_PRECISIONS,
    PLACES_ZIP,
    CensusGeocoder,
    Gazetteer,
    GeocodeRequest,
    GeocoderError,
    GeocodeResult,
    clean_address,
    geocode,
    house_number,
    normalize_place,
    parse_address,
    parse_census_response,
    place_base_name,
    street_title,
)
from atlas.net import FetchError, make_client
from atlas.schema.record import FacilityRecord
from atlas.validate import validate_record

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "census"
TULANE = "5420 Tulane Rd, Memphis, TN 38109"
HOLLY_RIDGE = "Holly Ridge, LA 71269"
ROSEMOUNT = "1772-2396 145th St, Rosemount, MN 55068"
CENSUS_HOST = "geocoding.geo.census.gov"


def recorded(address: str) -> bytes:
    digest = hashlib.sha256(address.encode("utf-8")).hexdigest()
    return (FIXTURES / f"{digest}.json").read_bytes()


class CensusServer:
    """A MockTransport handler that answers onelineaddress from the recorded responses."""

    def __init__(self, status: int = 200) -> None:
        self.calls: list[httpx.Request] = []
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.host != CENSUS_HOST:
            return httpx.Response(404, request=request)
        if self.status != 200:
            return httpx.Response(self.status, json={"errors": ["x"]}, request=request)
        address = request.url.params["address"]
        try:
            body = recorded(address)
        except FileNotFoundError:
            body = json.dumps({"result": {"input": {}, "addressMatches": []}}).encode()
        return httpx.Response(
            200, content=body, headers={"content-type": "application/json"}, request=request
        )


@pytest.fixture(scope="module")
def gazetteer() -> Gazetteer:
    return Gazetteer.load()


@pytest.fixture
def census_factory(tmp_path: Path) -> Iterator[Callable[..., tuple[CensusGeocoder, CensusServer]]]:
    clients: list[httpx.Client] = []

    def factory(status: int = 200, **kwargs: Any) -> tuple[CensusGeocoder, CensusServer]:
        server = CensusServer(status)
        client = make_client(
            transport=httpx.MockTransport(server), resolver=lambda host: ["93.184.216.34"]
        )
        clients.append(client)
        kwargs.setdefault("sleep", lambda s: None)
        return CensusGeocoder(client, tmp_path / "cache", **kwargs), server

    yield factory
    for c in clients:
        c.close()


# ---------------------------------------------------------------------------- address parsing


@pytest.mark.parametrize(
    ("text", "street", "city", "state", "postcode", "county"),
    [
        (TULANE, "5420 Tulane Rd", "Memphis", "TN", "38109", None),
        (HOLLY_RIDGE, None, "Holly Ridge", "LA", "71269", None),
        ("Cheyenne, WY 82007, USA", None, "Cheyenne", "WY", "82007", None),
        ("601 Kuna Mora Rd,  Kuna ID 83634", "601 Kuna Mora Rd", "Kuna", "ID", "83634", None),
        (
            "14651-15249 Gold Coast Rd 68138 Papillion Nebraska, USA",
            "14651-15249 Gold Coast Rd",
            "Papillion",
            "NE",
            "68138",
            None,
        ),
        (
            "175 Private Road 1604, Abilene, TX 79601, Shackelford County",
            "175 Private Road 1604",
            "Abilene",
            "TX",
            "79601",
            "Shackelford County",
        ),
        (
            "2205 Industrial South Rd, Dalton, GA, 30721",
            "2205 Industrial South Rd",
            "Dalton",
            "GA",
            "30721",
            None,
        ),
        (
            "3963 S Lincoln Ave, Vineland, New Jersey",
            "3963 S Lincoln Ave",
            "Vineland",
            "NJ",
            None,
            None,
        ),
        ("13360 Miller Rd NW", "13360 Miller Rd NW", None, None, None, None),
        ("Co Rd 42, Montgomery, AL 36105, USA", "Co Rd 42", "Montgomery", "AL", "36105", None),
    ],
)
def test_parse_address(
    text: str,
    street: str | None,
    city: str | None,
    state: str | None,
    postcode: str | None,
    county: str | None,
) -> None:
    got = parse_address(text)
    assert (got.street, got.city, got.state_abbr, got.postcode, got.county_name) == (
        street,
        city,
        state,
        postcode,
        county,
    )


def test_parse_address_unparseable_text_keeps_state_and_county() -> None:
    got = parse_address(
        "Amazon Data Services Inc, Madison Mega Site Nissan Parkway and Highway 22 Canton, "
        "Mississippi Madison County"
    )
    assert (got.state_abbr, got.county_name, got.street) == ("MS", "Madison County", None)


def test_small_helpers() -> None:
    assert (
        clean_address(" 216 Greenfield Rd, Lancaster, PA \n") == "216 Greenfield Rd, Lancaster, PA"
    )
    assert clean_address("1 Main St, Kuna, ID, United States") == "1 Main St, Kuna, ID"
    assert house_number("1772-2396 145th St") == "1772"
    assert house_number("0042 Main St") == "42"
    assert house_number("Co Rd 42") is None
    assert street_title("9685 87TH AVE SE") == "9685 87th Ave SE"
    assert street_title(None) is None
    assert place_base_name("Indianapolis city (balance)") == "Indianapolis"
    assert place_base_name("Nashville-Davidson metropolitan government (balance)") == (
        "Nashville-Davidson"
    )
    assert place_base_name("Carson City") == "Carson City"
    assert normalize_place("St. Louis") == normalize_place("Saint Louis") == "saint louis"
    assert normalize_place("Doña Ana") == "dona ana"


# ---------------------------------------------------------------------------- Gazetteer


def test_gazetteer_places(gazetteer: Gazetteer) -> None:
    assert gazetteer.place_count == 32_350
    assert gazetteer.county_count == 3_222
    cheyenne = gazetteer.place("WY", "Cheyenne")
    assert cheyenne is not None and cheyenne[2] == "Cheyenne"
    assert gazetteer.place("wy", "CHEYENNE CITY") == cheyenne
    assert gazetteer.place("IN", "Indianapolis") is not None  # "Indianapolis city (balance)"
    assert gazetteer.place("MO", "Saint Louis") == gazetteer.place("MO", "St. Louis")
    assert gazetteer.place("LA", "Holly Ridge") is None  # only North Carolina has one


def test_gazetteer_prefers_incorporated_places_and_refuses_ambiguous_names(
    gazetteer: Gazetteer,
) -> None:
    salem = gazetteer.place_entry("AR", "Salem")  # Salem city and Salem CDP
    assert salem is not None and salem.name == "Salem city"
    assert gazetteer.place("AL", "Mount Olive") is None  # two CDPs of that name


def test_gazetteer_counties(gazetteer: Gazetteer) -> None:
    madison = gazetteer.county("MS", "Madison County")
    assert madison is not None and madison[2] == "28089"
    assert gazetteer.county_full_name("22083") == "Richland Parish"
    assert gazetteer.county_full_name("99999") is None


def test_gazetteer_checks_the_sha256(tmp_path: Path, repo_root: Path) -> None:
    bad = tmp_path / "places.zip"
    shutil.copyfile(repo_root / PLACES_ZIP, bad)
    with bad.open("ab") as f:
        f.write(b"\0")
    with pytest.raises(ValueError, match="sha256"):
        Gazetteer.load(bad)
    with pytest.raises(FileNotFoundError):
        Gazetteer.load(tmp_path / "missing.zip")


# ---------------------------------------------------------------------------- Census Geocoder


def test_census_match_and_request(census_factory: Callable[..., Any]) -> None:
    census, server = census_factory()
    match = census.onelineaddress(TULANE)
    assert match is not None
    assert (match.lon, match.lat) == (-90.042976440198, 34.9994880087)
    assert match.county_fips == "47157" and match.county_name == "Shelby County"
    assert match.house_number == "5420" and match.state_abbr == "TN"
    (request,) = server.calls  # robots=False: no robots.txt request
    assert request.url.path == "/geocoder/geographies/onelineaddress"
    params = request.url.params
    assert params["address"] == TULANE
    assert (params["benchmark"], params["vintage"], params["format"], params["layers"]) == (
        "Public_AR_Current",
        "Current_Current",
        "json",
        "Counties",
    )


def test_census_no_match_and_long_addresses(census_factory: Callable[..., Any]) -> None:
    census, server = census_factory()
    assert census.onelineaddress(HOLLY_RIDGE) is None
    assert census.onelineaddress("x" * (CENSUS_MAX_ADDRESS + 1)) is None
    assert census.onelineaddress("   ") is None
    assert len(server.calls) == 1  # only Holly Ridge was sent


def test_census_cache(census_factory: Callable[..., Any], tmp_path: Path) -> None:
    census, server = census_factory()
    first = census.onelineaddress(TULANE)
    path = census.cache_path(TULANE)
    assert (
        path
        == tmp_path / "cache" / "census" / f"{hashlib.sha256(TULANE.encode()).hexdigest()}.json"
    )
    assert path.read_bytes() == recorded(TULANE)
    again, server2 = census_factory()
    assert again.onelineaddress(f"  {TULANE} ") == first  # whitespace-normalized key
    assert again.cache_hits == 1 and again.requests == 0
    assert not server2.calls and len(server.calls) == 1


def test_census_rate_limit_uses_the_injected_sleep(census_factory: Callable[..., Any]) -> None:
    sleeps: list[float] = []
    clock = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    census, server = census_factory(sleep=sleeps.append, now=lambda: clock)
    census.onelineaddress(TULANE)
    census.onelineaddress(HOLLY_RIDGE)
    census.onelineaddress(ROSEMOUNT)
    assert sleeps == [1.0, 1.0]
    assert len(server.calls) == 3


def test_census_errors(census_factory: Callable[..., Any]) -> None:
    census, _ = census_factory(status=400)
    assert census.onelineaddress(TULANE) is None
    assert not census.cache_path(TULANE).exists()  # a refusal is not cached
    failing, server = census_factory(status=503)
    with pytest.raises(FetchError):
        failing.onelineaddress(TULANE)
    assert len(server.calls) == 4  # three retries
    with pytest.raises(GeocoderError):
        parse_census_response({"errors": ["no"]})


# ---------------------------------------------------------------------------- the chain


def assert_rule5(result: GeocodeResult, counties: CountyIndex) -> None:
    """The result passes `atlas validate` rule 5 (and the other geo rules)."""
    assert result.lat is not None and result.lon is not None
    if result.precision in COUNTY_CHECKED_PRECISIONS:
        assert result.county_fips is not None
        assert counties.contains(result.county_fips, result.lat, result.lon)
    else:
        assert counties.in_state(result.state_abbr, result.lat, result.lon)
    record = FacilityRecord.model_validate(
        {
            "id": "gwa-" + "0" * 26,
            "record_type": "project",
            "canonical_name": "Geocode test",
            "status": "announced",
            "evidence_level": "reported",
            "status_history": [
                {
                    "seq": 1,
                    "status": "announced",
                    "event": "announced",
                    "as_of": {"value": "2026-01", "precision": "month"},
                    "source_ids": ["s1"],
                }
            ],
            "location": {
                "lat": result.lat,
                "lon": result.lon,
                "precision": result.precision,
                "county_fips": result.county_fips,
                "county_name": result.county_name,
                "state_abbr": result.state_abbr,
                "geocode_method": result.method,
            },
            "dates": {
                "first_reported": {"value": "2026-01", "precision": "month"},
                "announced": {"value": "2026-01", "precision": "month"},
            },
            "sources": [
                {
                    "id": "s1",
                    "url": "https://example.org/",
                    "publisher": "Example",
                    "source_type": "news",
                    "retrieved_at": "2026-10-12T12:00:00Z",
                    "supports": ["/canonical_name", "/location"],
                }
            ],
            "review": {"state": "machine"},
            "created_at": "2026-10-12T12:00:00Z",
            "updated_at": "2026-10-12T12:00:00Z",
            "last_verified_at": "2026-10-12T12:00:00Z",
        }
    )
    issues = validate_record(record, counties=counties, today=datetime(2026, 10, 12).date())
    assert [i for i in issues if i.rule == "geo"] == []


def test_step1_source_coordinates(gazetteer: Gazetteer, counties: CountyIndex) -> None:
    locality = geocode(
        GeocodeRequest(
            "TN", lat=35.07, lon=-90.05, source_precision="locality", county_name="Shelby"
        ),
        census=None,
        gazetteer=gazetteer,
        counties=counties,
    )
    assert locality is not None
    assert (locality.precision, locality.method, locality.county_fips) == (
        "locality",
        "source_coords",
        "47157",
    )
    assert_rule5(locality, counties)
    site = geocode(
        GeocodeRequest("TN", lat=34.999488, lon=-90.042976, source_precision="site"),
        census=None,
        gazetteer=gazetteer,
        counties=counties,
    )
    assert site is not None and site.county_fips == "47157"  # point-in-polygon
    assert_rule5(site, counties)


def test_step1_coordinates_outside_the_state_fall_through(
    gazetteer: Gazetteer, counties: CountyIndex
) -> None:
    req = GeocodeRequest("WY", lat=35.07, lon=-90.05, source_precision="locality")
    assert geocode(req, census=None, gazetteer=gazetteer, counties=counties) is None
    req = GeocodeRequest(
        "WY", lat=35.07, lon=-90.05, source_precision="locality", locality="Cheyenne"
    )
    result = geocode(req, census=None, gazetteer=gazetteer, counties=counties)
    assert result is not None and result.method == "gazetteer"


def test_step2_census_address_precision(
    census_factory: Callable[..., Any], gazetteer: Gazetteer, counties: CountyIndex
) -> None:
    census, _ = census_factory()
    parsed = parse_address(TULANE)
    result = geocode(
        GeocodeRequest(
            parsed.state_abbr,
            oneline=parsed.text,
            street=parsed.street,
            city=parsed.city,
            postcode=parsed.postcode,
            locality=parsed.city,
        ),
        census=census,
        gazetteer=gazetteer,
        counties=counties,
    )
    assert result is not None
    assert (result.precision, result.method, result.county_fips, result.county_name) == (
        "address",
        "census_geocoder",
        "47157",
        "Shelby",
    )
    assert (result.lat, result.lon) == (34.999488, -90.042976)
    assert (result.street, result.city, result.postcode) == ("5420 Tulane Rd", "Memphis", "38109")
    assert result.matched_address == "5420 TULANE RD, MEMPHIS, TN, 38109"
    assert result.confidence == 0.90
    assert_rule5(result, counties)


def test_step2_census_street_precision_when_the_house_number_differs(
    census_factory: Callable[..., Any], gazetteer: Gazetteer, counties: CountyIndex
) -> None:
    census, _ = census_factory()
    parsed = parse_address(ROSEMOUNT)
    result = geocode(
        GeocodeRequest(parsed.state_abbr, oneline=parsed.text, street=parsed.street),
        census=census,
        gazetteer=gazetteer,
        counties=counties,
    )
    assert result is not None
    assert (result.precision, result.county_fips, result.street) == (
        "street",
        "27037",
        "1772-2396 145th St",
    )
    assert result.matched_address == "2396 145TH ST E, ROSEMOUNT, MN, 55068"
    assert_rule5(result, counties)


def test_step2_rejects_a_match_in_another_state(
    census_factory: Callable[..., Any], gazetteer: Gazetteer, counties: CountyIndex
) -> None:
    census, _ = census_factory()
    req = GeocodeRequest("AR", oneline=TULANE)
    assert geocode(req, census=census, gazetteer=gazetteer, counties=counties) is None


def test_step3_gazetteer_after_a_census_miss(
    census_factory: Callable[..., Any], gazetteer: Gazetteer, counties: CountyIndex
) -> None:
    census, server = census_factory()
    req = GeocodeRequest("WY", oneline="Cheyenne, WY 82007", locality="Cheyenne", postcode="82007")
    result = geocode(req, census=census, gazetteer=gazetteer, counties=counties)
    assert result is not None
    assert (result.precision, result.method, result.city, result.county_fips) == (
        "locality",
        "gazetteer",
        "Cheyenne",
        None,
    )
    assert result.postcode == "82007" and result.matched_address == "Cheyenne city, WY"
    assert len(server.calls) == 1
    assert_rule5(result, counties)


def test_step3_takes_the_county_from_the_name(gazetteer: Gazetteer, counties: CountyIndex) -> None:
    req = GeocodeRequest("WY", locality="Cheyenne", county_name="Laramie County")
    result = geocode(req, census=None, gazetteer=gazetteer, counties=counties)
    laramie = counties.by_name("WY", "Laramie")
    assert laramie is not None
    assert result is not None and result.county_fips == laramie.fips
    assert_rule5(result, counties)


def test_step4_county_by_name(gazetteer: Gazetteer, counties: CountyIndex) -> None:
    parsed = parse_address(
        "Amazon Data Services Inc, Madison Mega Site Nissan Parkway and Highway 22 Canton, "
        "Mississippi Madison County"
    )
    result = geocode(
        GeocodeRequest(
            parsed.state_abbr,
            oneline=parsed.text,
            locality=parsed.city,
            county_name=parsed.county_name,
        ),
        census=None,
        gazetteer=gazetteer,
        counties=counties,
    )
    assert result is not None
    assert (result.precision, result.method, result.county_fips, result.city) == (
        "county",
        "county_centroid",
        "28089",
        None,
    )
    assert (result.lat, result.lon) == counties.centroid("28089")
    assert_rule5(result, counties)


def test_step5_none(
    census_factory: Callable[..., Any], gazetteer: Gazetteer, counties: CountyIndex
) -> None:
    census, _ = census_factory()
    parsed = parse_address(HOLLY_RIDGE)
    req = GeocodeRequest(parsed.state_abbr, oneline=parsed.text, locality=parsed.city)
    assert geocode(req, census=census, gazetteer=gazetteer, counties=counties) is None
    assert (
        geocode(GeocodeRequest(None), census=None, gazetteer=gazetteer, counties=counties) is None
    )


def test_census_damaged_cache_entry_is_fetched_again(census_factory: Callable[..., Any]) -> None:
    census, server = census_factory()
    path = census.cache_path(TULANE)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    match = census.onelineaddress(TULANE)
    assert match is not None and match.county_fips == "47157"
    assert len(server.calls) == 1 and census.cache_hits == 0
    assert path.read_bytes() == recorded(TULANE)


def test_census_unexpected_match_shape() -> None:
    doc = {"result": {"addressMatches": [{"coordinates": {"x": "west"}}]}}
    with pytest.raises(GeocoderError, match="unexpected address match"):
        parse_census_response(doc)
    with pytest.raises(GeocoderError, match="no addressMatches"):
        parse_census_response({"result": {}})
