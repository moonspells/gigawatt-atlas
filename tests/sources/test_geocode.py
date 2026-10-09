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
from atlas.geo.places import PlaceIndex
from atlas.geocode import (
    CENSUS_LONG_RANGE,
    CENSUS_MAX_ADDRESS,
    COUNTY_CHECKED_PRECISIONS,
    PLACES_ZIP,
    CensusGeocoder,
    Gazetteer,
    GeocodeRequest,
    GeocoderError,
    GeocodeResult,
    clean_address,
    cousub_base_name,
    geocode,
    house_number,
    normalize_place,
    parse_address,
    parse_census_response,
    place_base_name,
    street_name_tokens,
    street_title,
    street_tokens,
)
from atlas.net import FetchError, make_client
from atlas.schema.record import FacilityRecord
from atlas.validate import validate_record

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "census"
# More recorded responses, from the seed import of 2026-10-08 (tests/fixtures/geocode/README.md).
GEOCODE_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "geocode"
SEED_FIXTURES = GEOCODE_FIXTURES / "census"
PLACES_SAMPLE = GEOCODE_FIXTURES / "places" / "places_sample.zip"
COUSUBS_SAMPLE = GEOCODE_FIXTURES / "gazetteer" / "2025_Gaz_cousubs_sample.zip"
TULANE = "5420 Tulane Rd, Memphis, TN 38109"
HOLLY_RIDGE = "Holly Ridge, LA 71269"
ROSEMOUNT = "1772-2396 145th St, Rosemount, MN 55068"
CENSUS_HOST = "geocoding.geo.census.gov"


def recorded(address: str) -> bytes:
    digest = hashlib.sha256(address.encode("utf-8")).hexdigest()
    path = FIXTURES / f"{digest}.json"
    return (path if path.exists() else SEED_FIXTURES / f"{digest}.json").read_bytes()


class CensusServer:
    """A MockTransport handler that answers onelineaddress from the recorded responses."""

    def __init__(self, status: int = 200) -> None:
        self.calls: list[httpx.Request] = []
        self.status = status
        self.answer: bytes | None = None  # one body for every address, instead of the recordings

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.host != CENSUS_HOST:
            return httpx.Response(404, request=request)
        if self.status != 200:
            return httpx.Response(self.status, json={"errors": ["x"]}, request=request)
        address = request.url.params["address"]
        try:
            body = self.answer or recorded(address)
        except FileNotFoundError:
            body = json.dumps({"result": {"input": {}, "addressMatches": []}}).encode()
        return httpx.Response(
            200, content=body, headers={"content-type": "application/json"}, request=request
        )


@pytest.fixture(scope="module")
def gazetteer() -> Gazetteer:
    return Gazetteer.load()


@pytest.fixture(scope="module")
def places() -> Iterator[PlaceIndex]:
    index = PlaceIndex.load(PLACES_SAMPLE, verify_sha256=False)
    yield index
    index.close()


@pytest.fixture(scope="module")
def gazetteer_with_cousubs() -> Gazetteer:
    return Gazetteer.load(cousub_zip=COUSUBS_SAMPLE, verify_sha256=False)


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
    assert clean_address("1 Main\x1b St\x7f, Kuna, ID") == "1 Main St, Kuna, ID"
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
    # The county subdivision file is checked as well: the sample is not the Census file.
    with pytest.raises(ValueError, match="sha256"):
        Gazetteer.load(cousub_zip=COUSUBS_SAMPLE)


def test_gazetteer_county_subdivisions(gazetteer_with_cousubs: Gazetteer) -> None:
    g = gazetteer_with_cousubs
    assert g.cousub_count == 15 and g.place_count == 32_350
    bloomfield = g.cousub_entry("CT", "Bloomfield")
    assert bloomfield is not None and (bloomfield.name, bloomfield.county_fips) == (
        "Bloomfield town",
        "09110",  # Capitol Planning Region
    )
    assert g.cousub_entry("ct", "Bloomfield Town") == bloomfield
    assert g.cousub_entry("CT", "Hartford") is not None  # a town consolidated with its city (C)
    # Five Salem townships in Pennsylvania: ambiguous, unless the county says which.
    assert g.cousub_entry("PA", "Salem Township") is None
    salem = g.cousub_entry("PA", "Salem Township", "42079")
    assert salem is not None and salem.base_name == "Salem"
    # Statistical (CCD), nonfunctioning (precinct) and fictitious (village) entries are not used.
    assert g.cousub_entry("KY", "Bloomfield") is None
    assert g.cousub_entry("IL", "Bloomfield") is None
    assert g.cousub_entry("WI", "Bloomfield") is None  # two Bloomfield towns
    assert (cousub_base_name("Bloomfield charter township"), cousub_base_name("Gore")) == (
        "Bloomfield",
        "Gore",
    )
    assert Gazetteer.load().cousub_count == 0  # not loaded unless asked for
    # has_locality: what an importer may send as GeocodeRequest.locality.
    assert g.has_locality("CT", "Bloomfield") and not Gazetteer.load().has_locality(
        "CT", "Bloomfield"
    )
    assert g.has_locality("WY", "Cheyenne")  # a place
    assert not g.has_locality("PA", "Salem Township")  # five of them
    assert g.has_locality("PA", "Salem Township", "42079")


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


@pytest.mark.parametrize("status", [429, 408, 404, 403])
def test_census_rate_limits_and_other_4xx_are_errors_not_misses(
    census_factory: Callable[..., Any], gazetteer: Gazetteer, counties: CountyIndex, status: int
) -> None:
    # Only 400 means "no such address". A 429 after the retries must fail the run: as a miss it
    # would put the record at the Gazetteer place for one run and back at the address on the next.
    census, server = census_factory(status=status)
    with pytest.raises(FetchError):
        geocode(parsed_request(TULANE), census=census, gazetteer=gazetteer, counties=counties)
    assert not census.cache_path(TULANE).exists()
    assert len(server.calls) == (4 if status == 429 else 1)  # atlas.net retries only 429 and 5xx


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
    # The match's MEMPHIS is the USPS city; without the place polygons no city is named.
    assert (result.street, result.city, result.postcode) == ("5420 Tulane Rd", None, "38109")
    assert result.matched_address == "5420 TULANE RD, MEMPHIS, TN, 38109"
    assert result.confidence == 0.90
    assert_rule5(result, counties)


@pytest.mark.parametrize(
    ("address", "city", "precision"),
    [
        ("5420 Tulane Rd, Memphis, TN 38109", "Memphis", "address"),
        # QTS Cedar Rapids: USPS city FAIRFAX; the point is in Cedar Rapids city, but 5 m from its
        # generalized line, too close to name it: no city (the record names Linn County).
        ("6200 76th Ave SW, Fairfax, IA 52228", None, "address"),
        # OpenAI Stargate Lordstown: USPS city WARREN, the point in Lordstown village.
        ("2300 Hallock Young Rd, Warren, OH 44481", "Lordstown", "address"),
        # STACK NVA02: USPS city MANASSAS, the point in Innovation CDP, Prince William County.
        ("9590 Hornbaker Rd, Manassas, VA 20109", "Innovation", "street"),
        # Google Omaha: USPS city OMAHA, the point in no place (Union precinct, Douglas County).
        ("11110 State St, Omaha, NE 68142", None, "address"),
    ],
)
def test_step2_the_city_is_the_census_place_that_contains_the_point(
    census_factory: Callable[..., Any],
    gazetteer: Gazetteer,
    counties: CountyIndex,
    places: PlaceIndex,
    address: str,
    city: str | None,
    precision: str,
) -> None:
    census, _ = census_factory()
    req = parsed_request(address)
    match = census.onelineaddress(req.oneline or "")
    assert match is not None and match.city == (req.city or "").upper()  # the postal city
    result = geocode(req, census=census, gazetteer=gazetteer, counties=counties, places=places)
    assert result is not None
    assert (result.method, result.precision, result.city) == ("census_geocoder", precision, city)
    assert result.municipality is None
    assert_rule5(result, counties)
    bare = geocode(req, census=census, gazetteer=gazetteer, counties=counties)
    assert (
        bare is not None
        and bare.city is None
        and (bare.lat, bare.lon)
        == (
            result.lat,
            result.lon,
        )
    )


def parsed_request(text: str, *, county_name: str | None = None) -> GeocodeRequest:
    """The request the Epoch importer builds for an address."""
    parsed = parse_address(text)
    return GeocodeRequest(
        parsed.state_abbr,
        oneline=parsed.text,
        street=parsed.street,
        city=parsed.city,
        postcode=parsed.postcode,
        locality=parsed.city,
        county_name=county_name or parsed.county_name,
    )


def test_step2_census_street_precision_when_the_house_number_differs(
    census_factory: Callable[..., Any], gazetteer: Gazetteer, counties: CountyIndex
) -> None:
    census, server = census_factory()
    server.answer = recorded(TULANE)  # 5420 TULANE RD, for an input of 5400 Tulane Rd
    result = geocode(
        parsed_request("5400 Tulane Rd, Memphis, TN 38109"),
        census=census,
        gazetteer=gazetteer,
        counties=counties,
    )
    assert result is not None
    assert (result.precision, result.county_fips, result.street) == (
        "street",
        "47157",
        "5400 Tulane Rd",
    )
    assert result.confidence == 0.60  # 07 §3.4: derived from less than an address
    assert_rule5(result, counties)


@pytest.mark.parametrize(
    ("address", "city"),
    [
        # "42 COUNTY CT": the house number equals the road number, the street does not.
        ("Co Rd 42, Montgomery, AL 36105, USA", "Montgomery"),
        # "LARRISON DR" for Larrison Blvd: a street type the input states must be the match's.
        ("55001 Larrison Blvd, New Carlisle, IN 46552", "New Carlisle"),
        # "145TH ST E" and "145TH ST W", 4.6 km apart, both agree: ambiguous.
        (ROSEMOUNT, "Rosemount"),
    ],
)
def test_step2_a_match_on_another_street_falls_back_to_the_place(
    census_factory: Callable[..., Any],
    gazetteer: Gazetteer,
    counties: CountyIndex,
    address: str,
    city: str,
) -> None:
    census, server = census_factory()
    result = geocode(parsed_request(address), census=census, gazetteer=gazetteer, counties=counties)
    assert len(server.calls) == 1  # the Census answered with a match
    assert result is not None
    assert (result.precision, result.method, result.city) == ("locality", "gazetteer", city)
    assert_rule5(result, counties)


def test_step2_a_stated_directional_or_qualifier_must_match(
    census_factory: Callable[..., Any], gazetteer: Gazetteer, counties: CountyIndex
) -> None:
    # The Census matched "2950 S. Litchfield Road" to 2950 LITCHFIELD RD BYP, 6.6 km from
    # 2950 S Litchfield Rd. Even with the state and the city given, that match is refused.
    census, server = census_factory()
    req = GeocodeRequest(
        "AZ",
        oneline="2950 S. Litchfield Road",
        street="2950 S. Litchfield Road",
        city="Goodyear",
        locality="Goodyear",
    )
    result = geocode(req, census=census, gazetteer=gazetteer, counties=counties)
    assert len(server.calls) == 1
    assert result is not None and (result.precision, result.method) == ("locality", "gazetteer")


@pytest.mark.parametrize(
    ("address", "street", "postcode"),
    [
        ("984 County Road 112, Afton, TX 79220", "984 Co Rd 112", "79220"),  # CO RD 112
        ("1435 Hwy 54 W, Fayetteville, GA 30214", "1435 State Rte 54", "30214"),  # STATE RTE 54
    ],
)
def test_step2_street_synonyms_and_parts_the_input_leaves_out(
    census_factory: Callable[..., Any],
    gazetteer: Gazetteer,
    counties: CountyIndex,
    address: str,
    street: str,
    postcode: str,
) -> None:
    census, _ = census_factory()
    result = geocode(parsed_request(address), census=census, gazetteer=gazetteer, counties=counties)
    assert result is not None
    assert (result.precision, result.method, result.street, result.postcode) == (
        "address",
        "census_geocoder",
        street,
        postcode,
    )
    assert_rule5(result, counties)


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        # Meta Jeffersonville: 500 E 8TH ST is downtown, 13 km from the campus (IDEM permit:
        # 300 International Drive). The Gazetteer place of the request's city is used instead.
        ("500 8th St, Jeffersonville, IN 47130", ("locality", "gazetteer", "Jeffersonville")),
        # Amazon Ridgeland: 1626 E COUNTY LINE RD is 6.7 km from the campus on W County Line Rd.
        # The parser leaves the city in the street part, so no later step applies.
        ("1626 County Line Road Ridgeland, Mississippi", None),
    ],
)
def test_step2_a_directional_the_input_does_not_state_is_another_street(
    census_factory: Callable[..., Any],
    gazetteer: Gazetteer,
    counties: CountyIndex,
    address: str,
    expected: tuple[str, str, str] | None,
) -> None:
    census, server = census_factory()
    req = parsed_request(address)
    match = census.onelineaddress(req.oneline or "")
    assert match is not None and match.street_parts[1][0] == "preDirection"
    assert match.street_parts[1][1] == ("E",)  # the only match adds an E the input lacks
    result = geocode(req, census=census, gazetteer=gazetteer, counties=counties)
    assert len(server.calls) == 1
    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert (result.precision, result.method, result.city) == expected
        assert result.street is None
        assert_rule5(result, counties)


def test_step2_a_long_address_range_gives_street_precision(
    census_factory: Callable[..., Any], gazetteer: Gazetteer, counties: CountyIndex
) -> None:
    # Meta Kuna: 601 KUNA MORA RD is interpolated on the edge numbered 1229-1 beside Kuna's town
    # centre; the campus is 12.2 km east on the same road. The house number agrees, but such a
    # long edge is not address precision.
    census, _ = census_factory()
    req = parsed_request("601 Kuna Mora Rd, Kuna ID 83634")
    match = census.onelineaddress(req.oneline or "")
    assert match is not None and (match.range_from, match.range_to, match.range_span) == (
        1229,
        1,
        1228,
    )
    assert match.house_number == "601"
    result = geocode(req, census=census, gazetteer=gazetteer, counties=counties)
    assert result is not None
    assert (result.precision, result.method, result.street, result.confidence) == (
        "street",
        "census_geocoder",
        "601 Kuna Mora Rd",
        0.60,
    )
    assert result.county_fips == "16001"  # Ada County, from the point
    assert_rule5(result, counties)
    # A range of 1,000 numbers or more is long: 9590 Hornbaker Rd is on 9512-10542.
    hornbaker = geocode(
        parsed_request("9590 Hornbaker Rd, Manassas, VA 20109"),
        census=census,
        gazetteer=gazetteer,
        counties=counties,
    )
    assert hornbaker is not None and hornbaker.precision == "street"
    assert CENSUS_LONG_RANGE == 1000


def test_street_tokens() -> None:
    assert street_tokens("Hwy 54 W") == ("HWY", "54", "W")
    assert street_tokens("STATE RTE 54") == street_tokens("State Route 54") == ("HWY", "54")
    assert street_tokens("US Hwy 54") == ("US", "HWY", "54")
    assert street_tokens("County Road 112") == street_tokens("CR 112") == ("CO", "RD", "112")
    assert street_tokens("S. Litchfield Road") == ("S", "LITCHFIELD", "RD")
    assert street_name_tokens("1772-2396 145th St") == ("145TH", "ST")
    assert street_name_tokens("Co Rd 42") == ("CO", "RD", "42")


def test_step2_rejects_a_match_in_another_state(
    census_factory: Callable[..., Any], gazetteer: Gazetteer, counties: CountyIndex
) -> None:
    census, _ = census_factory()
    req = GeocodeRequest("AR", oneline=TULANE)
    assert geocode(req, census=census, gazetteer=gazetteer, counties=counties) is None


def test_step2_needs_a_state(
    census_factory: Callable[..., Any], gazetteer: Gazetteer, counties: CountyIndex
) -> None:
    # "2950 S. Litchfield Road" names no state; any Census match could come from any state, so the
    # address is not sent at all.
    census, server = census_factory()
    req = parsed_request("2950 S. Litchfield Road")
    assert req.state_abbr is None
    assert geocode(req, census=census, gazetteer=gazetteer, counties=counties) is None
    assert server.calls == []


def postal_request(text: str, *, county_name: str | None = None) -> GeocodeRequest:
    """An address whose city is only its postal city (no source states the place)."""
    parsed = parse_address(text)
    return GeocodeRequest(
        parsed.state_abbr,
        oneline=parsed.text,
        street=parsed.street,
        city=parsed.city,
        postcode=parsed.postcode,
        county_name=county_name or parsed.county_name,
    )


@pytest.mark.parametrize(
    "address",
    [
        "1125 Electron Ave, Berwick, PA 18603",  # AWS Berwick: Berwick is in Columbia County
        "7601 State Hwy 105, Trenton, SC 29847",  # Meta Aiken: Trenton is in Edgefield County
        "15000 Lambda Drive, San Antonio, TX 78245",  # SAT40: 27 km from San Antonio's point
    ],
)
def test_step3_a_postal_city_alone_gives_no_point(
    census_factory: Callable[..., Any], gazetteer: Gazetteer, counties: CountyIndex, address: str
) -> None:
    census, server = census_factory()
    req = postal_request(address)
    assert req.city and req.locality is None and req.county_name is None
    assert gazetteer.place(req.state_abbr or "", req.city) is not None  # the city is a place
    assert geocode(req, census=census, gazetteer=gazetteer, counties=counties) is None
    assert len(server.calls) == 1  # the Census had no match


@pytest.mark.parametrize(
    ("address", "county_name", "expected"),
    [
        # The site is in Luzerne County; Berwick's point is in Columbia: the county's point.
        ("1125 Electron Ave, Berwick, PA 18603", "Luzerne County", ("county", "42079")),
        # Meta Aiken is in Aiken County; Trenton's point is in Edgefield: the county's point.
        ("7601 State Hwy 105, Trenton, SC 29847", "Aiken County", ("county", "45003")),
        # A postal city inside the stated county gives its point, but never the city.
        ("15000 Lambda Drive, San Antonio, TX 78245", "Bexar County", ("locality", "48029")),
    ],
)
def test_step3_a_postal_city_gives_a_point_only_inside_a_stated_county(
    census_factory: Callable[..., Any],
    gazetteer: Gazetteer,
    counties: CountyIndex,
    address: str,
    county_name: str,
    expected: tuple[str, str],
) -> None:
    census, _ = census_factory()
    req = postal_request(address, county_name=county_name)
    result = geocode(req, census=census, gazetteer=gazetteer, counties=counties)
    assert result is not None
    assert (result.precision, result.county_fips) == expected
    assert result.city is None and result.municipality is None
    assert_rule5(result, counties)


def test_step3_a_stated_place_still_names_the_city(
    gazetteer: Gazetteer, counties: CountyIndex
) -> None:
    # AI GridWatch's "Trenton (Aiken County)": a place a source states. Trenton's point is in
    # Edgefield County, so the county's point; a stated place inside the county names the city.
    req = GeocodeRequest("SC", locality="Trenton", county_name="Aiken County")
    result = geocode(req, census=None, gazetteer=gazetteer, counties=counties)
    assert result is not None and (result.precision, result.county_fips) == ("county", "45003")
    req = GeocodeRequest("SC", locality="Trenton", county_name="Edgefield County")
    result = geocode(req, census=None, gazetteer=gazetteer, counties=counties)
    assert result is not None and (result.precision, result.city) == ("locality", "Trenton")


@pytest.mark.parametrize(
    ("req", "expected"),
    [
        # Bloomfield, CT is a town: the Gazetteer's only place there is Blue Hills CDP, and the
        # 2025 county file has planning regions, not Hartford County.
        (
            GeocodeRequest("CT", locality="Bloomfield"),
            ("Bloomfield", "09110", 41.843772, -72.741002),
        ),
        (
            GeocodeRequest("CT", locality="Bloomfield", county_name="Hartford County"),
            ("Bloomfield", "09110", 41.843772, -72.741002),
        ),
        (
            GeocodeRequest("PA", locality="Salem Township", county_name="Luzerne County"),
            ("Salem", "42079", 41.105979, -76.183579),
        ),
    ],
)
def test_step3_a_county_subdivision_the_source_names(
    gazetteer_with_cousubs: Gazetteer,
    gazetteer: Gazetteer,
    counties: CountyIndex,
    req: GeocodeRequest,
    expected: tuple[str, str, float, float],
) -> None:
    without = geocode(req, census=None, gazetteer=gazetteer, counties=counties)
    if req.county_name == "Luzerne County":
        assert without is not None and without.precision == "county"  # the county's point
    else:
        assert without is None  # the seed's geocode_failed for atlas-bloomfield-ct
    result = geocode(req, census=None, gazetteer=gazetteer_with_cousubs, counties=counties)
    assert result is not None
    assert (result.precision, result.method, result.city) == ("locality", "gazetteer", None)
    assert (result.municipality, result.county_fips, result.lat, result.lon) == expected
    assert_rule5(result, counties)


def test_step3_an_ambiguous_or_misplaced_county_subdivision_is_not_used(
    gazetteer_with_cousubs: Gazetteer, counties: CountyIndex
) -> None:
    g = gazetteer_with_cousubs
    # Five Salem townships and no county: nothing to place.
    assert (
        geocode(
            GeocodeRequest("PA", locality="Salem Township"),
            census=None,
            gazetteer=g,
            counties=counties,
        )
        is None
    )
    # Salem Township in a county that has none: the county's point.
    result = geocode(
        GeocodeRequest("PA", locality="Salem Township", county_name="Columbia County"),
        census=None,
        gazetteer=g,
        counties=counties,
    )
    assert result is not None and (result.precision, result.municipality) == ("county", None)


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


@pytest.mark.parametrize(
    ("state", "locality", "county_name"),
    [
        ("TX", "Abilene", "Shackelford County"),  # Abilene's point is in Taylor County
        ("NV", "Reno", "Storey County"),  # Reno's point is in Washoe County
    ],
)
def test_step3_a_place_outside_the_stated_county_gives_the_county(
    gazetteer: Gazetteer, counties: CountyIndex, state: str, locality: str, county_name: str
) -> None:
    named = counties.by_name(state, county_name)
    assert named is not None
    req = GeocodeRequest(state, locality=locality, county_name=county_name)
    result = geocode(req, census=None, gazetteer=gazetteer, counties=counties)
    assert result is not None
    assert (result.precision, result.method, result.county_fips, result.city) == (
        "county",
        "county_centroid",
        named.fips,
        None,
    )
    assert counties.contains(named.fips, result.lat or 0.0, result.lon or 0.0)
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
