"""The geocoding chain (07 §6.5): source coordinates, Census Geocoder, Gazetteer place, county.

geocode() stops at the first step that succeeds:

1. Source coordinates, at the precision the source states.
2. Census Geocoder (onelineaddress), only for a request that names its state (a match could
   otherwise come from any state). A match counts only when its state, its street (name, type,
   directionals and qualifiers; the input may leave out the street type, never a directional) and
   its city or ZIP agree with the request: "address" when the matched house number also equals
   the input's and the matched address range is short, otherwise "street". Agreeing matches more
   than 200 m apart are ambiguous and count as none. County from the response GEOID when the point
   lies in it.
3. Census Gazetteer internal point of the place a source states (`locality`): "locality", unless
   a stated county does not contain it. A name that is no Census place may be a county
   subdivision (a New England town, a township), when the Gazetteer was loaded with them: its
   point, its county and `municipality`. Without a stated place, a postal city (`city`) gives its
   point only inside a stated county, and never the city: it names the post office, not the place
   the site is in, and its point can lie in another county (AWS Berwick's postal city is in
   Columbia County, the campus in Luzerne).
4. County by name (CountyIndex.by_name) at the polygon's point on surface: "county".
5. None: the caller files a review item.

County FIPS comes from point-in-polygon when the precision is address or better, otherwise from the
stated county name (or the county subdivision's county). Every result is checked against the county
polygons with the tolerance that `atlas validate` rule 5 uses, so a result from geocode() passes
that rule, and a locality point lies in the county the result names.

`city` is a place a source states, or the Census place that contains a Census match's point
(`places`, atlas.geo.places), never the postal city of an address: QTS Cedar Rapids is mailed to
Fairfax, IA, but its point is in Cedar Rapids city. Without `places`, or when no place contains
the point, a Census result names no city, and the caller names the county.

Importing this module does no I/O and loads neither DuckDB nor httpx.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode

from atlas.geo import reference
from atlas.geo.counties import resolve_reference, sha256_file
from atlas.geo.fips import STATES, state_by_abbr
from atlas.safezip import open_zip
from atlas.schema.record import GeocodeMethod, Precision
from atlas.text import clean_text, strip_invisible

if TYPE_CHECKING:
    import httpx

    from atlas.geo.counties import County, CountyIndex
    from atlas.geo.places import PlaceIndex


PLACES_ZIP = Path("reference/census/2025_Gaz_place_national.zip")
PLACES_SHA256 = "49644173a453469d9bd77fb7a493b027f87567e209edaf2078aac7543ac2ee29"
GAZ_COUNTIES_ZIP = Path("reference/census/2025_Gaz_counties_national.zip")
GAZ_COUNTIES_SHA256 = "4c90d0f805779923b5958ab13d0c1e9b99fe4932b786bfcf75dd739bb2dcb4ea"
# County subdivisions (36,427: New England towns, townships, CCDs), for Gazetteer.load(cousub_zip=).
# Not committed: an import downloads the file on first use and checks it against the pin
# (atlas.geo.reference.COUNTY_SUBDIVISIONS; ImportContext.gazetteer() loads it for every import
# that geocodes).
COUSUBS_SHA256 = reference.COUNTY_SUBDIVISIONS.sha256
COUSUBS_BYTES = reference.COUNTY_SUBDIVISIONS.size

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
CENSUS_PARAMS = {
    "benchmark": "Public_AR_Current",
    "vintage": "Current_Current",
    "format": "json",
    "layers": "Counties",
}
# The Census Geocoder answers HTTP 400 for an address longer than this.
CENSUS_MAX_ADDRESS = 100
# The answer that means "this address cannot be geocoded". Any other error (429, 408, 5xx, another
# 4xx) after the retries in atlas.net.fetch is raised: a rate-limited request must not be mistaken
# for a miss, or the record would drop to the Gazetteer for one run and come back on the next.
CENSUS_NO_MATCH_STATUSES = frozenset({400})
# Agreeing matches farther apart than this are ambiguous ("145th St E" and "145th St W").
CENSUS_AMBIGUOUS_M = 200.0
# A match interpolated on a TIGER edge whose address range spans this many house numbers or more
# gives "street", not "address": such an edge is a long rural stretch, where the interpolated point
# can lie far from the building and the same number may belong to another stretch of the road
# under another numbering. "601 Kuna Mora Rd, Kuna ID" matched 601 on the edge numbered 1-1229
# beside the town centre; Meta's campus is 12 km east on the same road. In the seed's 31 matches
# the only other such edge is 9512-10542 Hornbaker Rd (0.45 km from the building); every other
# range spans under 900 numbers, most of them 100 to 200.
CENSUS_LONG_RANGE = 1000

# Confidence of the location per step (07 §3.4: derived in code, 0.90 when the input is address
# precision or better, otherwise 0.60; imported from an open dataset, 0.70).
CONFIDENCE: dict[str, float] = {
    "source_coords": 0.70,
    "address": 0.90,
    "street": 0.60,
    "locality": 0.60,
    "county": 0.60,
}

# Precisions whose county comes from point-in-polygon (07 §6.5).
POINT_IN_POLYGON_PRECISIONS = frozenset({"footprint", "parcel", "site", "address"})
# Precisions whose point must lie inside the stated county (validate rule 5).
COUNTY_CHECKED_PRECISIONS = frozenset(
    {"footprint", "parcel", "site", "address", "street", "county"}
)

_SPACE_RE = re.compile(r"\s+")
_HOUSE_RE = re.compile(r"^\s*(\d+)")
_ZIP_RE = re.compile(r"^(\d{5})(?:-\d{4})?$")
_COUNTRY_RE = re.compile(
    r"(?:,\s*|\s+)(?:USA|U\.S\.A\.?|U\.S\.|United States(?: of America)?)\.?$", re.I
)
_COUNTY_PART_RE = re.compile(r"^(.+?\s(?:County|Parish|Borough|Census Area))$", re.I)
_STREET_ZIP_CITY_RE = re.compile(r"^(?P<street>\d.*?)\s+(?P<zip>\d{5})\s+(?P<city>\D.*)$")
_CITY_ST_RE = re.compile(r"^(?P<city>.*?)\s*\b(?P<st>[A-Z]{2})(?:\s+(?P<zip>\d{5})(?:-\d{4})?)?$")
_LSAD_SUFFIX_RE = re.compile(
    r"\s+(?:city and borough|city|town|village|CDP|borough|municipality|comunidad|zona urbana|"
    r"corporation|urban county|(?:unified|consolidated|metropolitan|metro) government)"
    r"(?:\s*\(balance\))?$"
)
_BALANCE_RE = re.compile(r"\s*\(balance\)$")
_DIRECTIONALS = frozenset({"N", "S", "E", "W", "NE", "NW", "SE", "SW"})
# Census preType values (as street_tokens) of numbered routes: "STATE RTE 54", "US HWY 54", "CO RD 112".
_NUMBERED_ROUTE_TYPES = frozenset({("HWY",), ("US", "HWY"), ("CO", "RD")})
_HOUSE_PART_RE = re.compile(r"^\s*\d+(?:\s*[-\u2013]\s*\d+)?\s+(?=\S)")
_STREET_PUNCT_RE = re.compile(r"[.,#]")
# Street words spelled out and abbreviated, mapped to one form on both sides of a comparison (the
# Census writes USPS abbreviations: RD, AVE, BLVD, PKWY, CO RD).
_STREET_WORDS = {
    "BYPASS": "BYP",
    "CR": "CO RD",
    "SR": "HWY",
    "RTE": "HWY",
    "NORTH": "N",
    "SOUTH": "S",
    "EAST": "E",
    "WEST": "W",
    "NORTHEAST": "NE",
    "NORTHWEST": "NW",
    "SOUTHEAST": "SE",
    "SOUTHWEST": "SW",
    "AVENUE": "AVE",
    "AV": "AVE",
    "BOULEVARD": "BLVD",
    "CENTER": "CTR",
    "CENTRE": "CTR",
    "CIRCLE": "CIR",
    "COUNTY": "CO",
    "COURT": "CT",
    "DRIVE": "DR",
    "EXPRESSWAY": "EXPY",
    "FREEWAY": "FWY",
    "HIGHWAY": "HWY",
    "LANE": "LN",
    "LP": "LOOP",
    "PARKWAY": "PKWY",
    "PKY": "PKWY",
    "PLACE": "PL",
    "PRIVATE": "PVT",
    "ROAD": "RD",
    "ROUTE": "HWY",
    "SQUARE": "SQ",
    "STREET": "ST",
    "TERRACE": "TER",
    "TRAIL": "TRL",
}
_ABBREVIATIONS = {"st": "saint", "ste": "sainte", "mt": "mount", "ft": "fort"}
_INCORPORATED_LSAD_EXCLUDED = frozenset({"57", "55", "62"})  # CDP, comunidad, zona urbana
# County subdivisions that are governments (FUNCSTAT A, B, C, G): towns, townships, plantations.
# Not F (coextensive with an incorporated place, which the place step finds), I (inactive),
# N (nonfunctioning: precincts, barrios, NH grants) or S (statistical: CCDs).
_ACTIVE_COUSUB_FUNCSTAT = frozenset({"A", "B", "C", "G"})
_COUSUB_SUFFIX_RE = re.compile(r"\s+(?:charter township|township|town|plantation|borough)$")

_STATE_NAMES = {s.name.casefold(): s.abbr for s in STATES}
_STATE_NAMES["washington dc"] = "DC"
_STATE_NAME_RE = re.compile(
    r"^(?P<prefix>.*?)\s*\b(?P<name>"
    + "|".join(re.escape(n) for n in sorted(_STATE_NAMES, key=len, reverse=True))
    + r")\b\s*(?P<rest>.*)$",
    re.I,
)


# ---------------------------------------------------------------------------- requests and results


@dataclass(frozen=True)
class GeocodeRequest:
    """What is known about a place. Set only the fields the source gives.

    city is the city of a postal address (its USPS city): it is checked against a Census match
    and may give a point inside a stated county, but never names the place. locality is a place a
    source says the site is in; it names the city (07 §6.5 step 3).
    """

    state_abbr: str | None
    oneline: str | None = None
    street: str | None = None
    city: str | None = None
    postcode: str | None = None
    lat: float | None = None
    lon: float | None = None
    source_precision: Precision | None = None
    locality: str | None = None
    county_name: str | None = None


@dataclass(frozen=True)
class GeocodeResult:
    lat: float | None
    lon: float | None
    precision: Precision
    method: GeocodeMethod | None
    state_abbr: str
    county_fips: str | None
    county_name: str | None
    city: str | None
    postcode: str | None
    street: str | None
    matched_address: str | None
    confidence: float
    municipality: str | None = None  # a county subdivision the request named ("Bloomfield")


@dataclass(frozen=True)
class CensusMatch:
    """One address match of a Census Geocoder response.

    The street parts are the response's addressComponents in order (preQualifier, preDirection,
    preType, streetName, suffixType, suffixDirection, suffixQualifier), each a normalized token
    tuple; street_parts is empty when the response has no components.
    """

    lat: float
    lon: float
    matched_address: str
    house_number: str | None
    street: str | None
    city: str | None
    state_abbr: str | None
    postcode: str | None
    county_fips: str | None
    county_name: str | None
    street_parts: tuple[tuple[str, tuple[str, ...]], ...] = ()
    range_from: int | None = None  # the matched edge's address range (fromAddress, toAddress)
    range_to: int | None = None

    @property
    def range_span(self) -> int | None:
        """How many house numbers the matched edge's range spans, when the response gives it."""
        if self.range_from is None or self.range_to is None:
            return None
        return abs(self.range_to - self.range_from)


@dataclass(frozen=True)
class ParsedAddress:
    """The parts of a free-text US address that parse_address could find."""

    text: str
    street: str | None
    city: str | None
    state_abbr: str | None
    postcode: str | None
    county_name: str | None


# ---------------------------------------------------------------------------- address parsing


def clean_address(text: str) -> str:
    """Invisible characters removed, whitespace collapsed, a trailing country name dropped."""
    s = clean_text(text).strip(" ,")
    return _COUNTRY_RE.sub("", s).strip(" ,")


def street_title(street: str | None) -> str | None:
    """A Census street ("1435 HWY 54 W", "9685 87TH AVE SE") in title case, directionals kept."""
    if not street:
        return None
    return " ".join(
        w if w in _DIRECTIONALS else w.capitalize() for w in _SPACE_RE.split(street.strip())
    )


def house_number(text: str | None) -> str | None:
    """The leading house number ("1772" in "1772-2396 145th St"), without leading zeros."""
    if not text:
        return None
    m = _HOUSE_RE.match(text)
    return str(int(m.group(1))) if m else None


def street_tokens(text: str | None) -> tuple[str, ...]:
    """Upper-case words with punctuation dropped and street words in one form (_STREET_WORDS).

    Route words become HWY and "State Hwy" becomes HWY, so "Hwy 54", "State Route 54" and the
    Census "STATE RTE 54" compare equal; "US Hwy 54" stays different.
    """
    words = _STREET_PUNCT_RE.sub(" ", strip_invisible(text or "")).upper().split()
    tokens = [t for w in words for t in _STREET_WORDS.get(w, w).split()]
    out: list[str] = []
    for t in tokens:
        if t == "HWY" and out and out[-1] == "STATE":
            out[-1] = "HWY"
        else:
            out.append(t)
    return tuple(out)


def street_name_tokens(street: str | None) -> tuple[str, ...]:
    """The street without its house number or range, as street_tokens."""
    m = _HOUSE_PART_RE.match(street or "")
    return street_tokens((street or "")[m.end() :] if m else street)


def _state_part(part: str) -> tuple[str | None, str, str | None, str | None] | None:
    """(text before the state, state abbr, zip, county) when part names a state, else None."""
    m = _CITY_ST_RE.match(part)
    if m and state_by_abbr(m.group("st")) is not None:
        return (m.group("city").strip() or None, m.group("st"), m.group("zip"), None)
    m = _STATE_NAME_RE.match(part)
    if m is None:
        return None
    abbr = _STATE_NAMES[m.group("name").casefold()]
    rest = m.group("rest").strip()
    zip_code: str | None = None
    county: str | None = None
    if rest:
        zm = _ZIP_RE.match(rest)
        if zm:
            zip_code = zm.group(1)
        elif _COUNTY_PART_RE.match(rest):
            county = rest
        else:
            return None
    return (m.group("prefix").strip() or None, abbr, zip_code, county)


def parse_address(text: str) -> ParsedAddress:
    """Split a free-text US address into street, city, state, ZIP and county where possible.

    Handles "5420 Tulane Rd, Memphis, TN 38109", "Holly Ridge, LA 71269", "Cheyenne, WY 82007,
    USA", "601 Kuna Mora Rd, Kuna ID 83634", "14651 Gold Coast Rd 68138 Papillion Nebraska" and
    "..., Canton, Mississippi Madison County". A part it cannot read is left out, never guessed.
    """
    cleaned = clean_address(text)
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    street = city = state = postcode = county = None
    state_index: int | None = None
    for i in range(len(parts) - 1, -1, -1):
        part = parts[i]
        zm = _ZIP_RE.match(part)
        if zm:
            postcode = postcode or zm.group(1)
            continue
        if _COUNTY_PART_RE.match(part) and _state_part(part) is None:
            county = county or part
            continue
        found = _state_part(part)
        if found is None:
            break
        prefix, state, zip_code, county_in_part = found
        postcode = postcode or zip_code
        county = county or county_in_part
        state_index = i
        if prefix:
            sm = _STREET_ZIP_CITY_RE.match(prefix)
            if sm:
                street, city = sm.group("street"), sm.group("city")
                postcode = postcode or sm.group("zip")
            elif prefix[0].isdigit():
                street = prefix
            else:
                city = prefix
        break
    if state_index is None:
        if parts and parts[0][0].isdigit():
            street = parts[0]
    else:
        before = parts[:state_index]
        if city is None and street is None and before:
            last = before.pop()
            if last[0].isdigit():
                street = last
            else:
                city = last
        if street is None and before and any(c.isdigit() for c in before[-1]):
            street = before[-1]
    return ParsedAddress(cleaned, street, city, state, postcode, county)


# ---------------------------------------------------------------------------- Census Geocoder


class GeocoderError(Exception):
    """The Census Geocoder answered with something that is not a geocoder response."""


_COMPONENT_KEYS = (
    "preQualifier",
    "preDirection",
    "preType",
    "streetName",
    "suffixType",
    "suffixDirection",
    "suffixQualifier",
)


def parse_census_response(doc: object) -> CensusMatch | None:
    """The first address match of an onelineaddress response, or None when there is none."""
    matches = parse_census_matches(doc)
    return matches[0] if matches else None


def parse_census_matches(doc: object) -> list[CensusMatch]:
    """Every address match of an onelineaddress response, in the order given."""
    if not isinstance(doc, dict) or not isinstance(doc.get("result"), dict):
        raise GeocoderError("not a Census Geocoder response (no result object)")
    matches = doc["result"].get("addressMatches")
    if not isinstance(matches, list):
        raise GeocoderError("not a Census Geocoder response (no addressMatches list)")
    return [_census_match(m) for m in matches]


def _census_match(m: object) -> CensusMatch:
    if not isinstance(m, dict):
        raise GeocoderError(f"Census Geocoder: unexpected address match: {m!r}")
    try:
        coords = m.get("coordinates") or {}
        lat, lon = float(coords["y"]), float(coords["x"])
        comps = m.get("addressComponents") or {}
        counties = (m.get("geographies") or {}).get("Counties") or []
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        raise GeocoderError(f"Census Geocoder: unexpected address match: {e!r}") from e
    county = counties[0] if counties and isinstance(counties[0], dict) else {}
    matched = str(m.get("matchedAddress") or "")
    geoid = county.get("GEOID")
    parts = (
        tuple((key, street_tokens(str(comps.get(key) or ""))) for key in _COMPONENT_KEYS)
        if isinstance(comps, dict) and comps.get("streetName")
        else ()
    )

    def range_end(key: str) -> int | None:
        value = str(comps.get(key) or "").strip() if isinstance(comps, dict) else ""
        return int(value) if value.isdigit() else None

    return CensusMatch(
        lat=lat,
        lon=lon,
        matched_address=matched,
        house_number=house_number(matched),
        street=matched.split(",", 1)[0].strip() or None,
        city=str(comps["city"]) if comps.get("city") else None,
        state_abbr=str(comps["state"]) if comps.get("state") else None,
        postcode=str(comps["zip"]) if comps.get("zip") else None,
        county_fips=str(geoid) if geoid and re.fullmatch(r"\d{5}", str(geoid)) else None,
        county_name=str(county["NAME"]) if county.get("NAME") else None,
        street_parts=parts,
        range_from=range_end("fromAddress"),
        range_to=range_end("toAddress"),
    )


def census_url(address: str) -> str:
    return f"{CENSUS_URL}?{urlencode({'address': address, **CENSUS_PARAMS}, quote_via=quote)}"


class CensusGeocoder:
    """The Census Geocoder's onelineaddress endpoint (public domain), one request per second.

    Responses are cached by sha256(address) in cache_dir/census/, so a re-run sends no request.
    This is an API, not a crawl, so robots.txt is not consulted (robots=False).
    """

    def __init__(
        self,
        client: httpx.Client,
        cache_dir: Path,
        *,
        min_interval: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client
        self.cache_dir = Path(cache_dir) / "census"
        self.min_interval = min_interval
        self._sleep = sleep
        self._now = now
        self.requests = 0
        self.cache_hits = 0
        self._seen: dict[str, list[CensusMatch]] = {}

    @staticmethod
    def normalize(address: str) -> str:
        return _SPACE_RE.sub(" ", strip_invisible(address)).strip()

    def cache_path(self, address: str) -> Path:
        digest = hashlib.sha256(self.normalize(address).encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def onelineaddress(self, address: str) -> CensusMatch | None:
        """The first match for address, or None (see matches)."""
        found = self.matches(address)
        return found[0] if found else None

    def matches(self, address: str) -> list[CensusMatch]:
        """Every match for address; none for no match, an empty address or one over 100 characters.

        Raises atlas.net.FetchError when the service fails after retries (429, 408, 5xx and any
        4xx but 400 included), and GeocoderError when it answers with something else. A 400 answer
        counts as no match and is not cached.
        """
        from atlas.net import FetchError, fetch  # httpx on use

        text = self.normalize(address)
        if not text or len(text) > CENSUS_MAX_ADDRESS:
            return []
        path = self.cache_path(text)
        if path.exists():
            try:
                cached = parse_census_matches(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, GeocoderError):
                path.unlink()  # a damaged cache entry is fetched again
            else:
                self.cache_hits += 1
                self._seen[text] = cached
                return cached
        self.requests += 1
        try:
            result = fetch(
                self.client,
                census_url(text),
                robots=False,
                min_interval=self.min_interval,
                allowed_types=("application/json",),
                max_bytes=2_000_000,
                sleep=self._sleep,
                now=self._now,
            )
        except FetchError as e:
            if e.status in CENSUS_NO_MATCH_STATUSES:
                return []
            raise
        try:
            doc = json.loads(result.content)
        except ValueError as e:
            raise GeocoderError(f"Census Geocoder: response is not JSON: {e}") from e
        found = parse_census_matches(doc)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(result.content)
        self._seen[text] = found
        return found

    def seen(self, address: str) -> list[CensusMatch]:
        """The matches an earlier matches() call returned for address in this run, without a
        request or a cache read (none when it was not asked): for a review item that shows a
        reviewer the matches the chain refused."""
        return list(self._seen.get(self.normalize(address), []))


# ---------------------------------------------------------------------------- Gazetteer


def normalize_place(name: str) -> str:
    """Casefolded, accents and punctuation removed, St/Mt/Ft spelled out."""
    s = unicodedata.normalize("NFKD", strip_invisible(name))
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = re.sub("[.'\u2019]", "", s)
    s = re.sub(r"[^\w]+", " ", s)
    return " ".join(_ABBREVIATIONS.get(t, t) for t in s.split())


def place_base_name(name: str) -> str:
    """A Gazetteer NAME without its LSAD descriptor: "Abbeville city" -> "Abbeville"."""
    stripped = _LSAD_SUFFIX_RE.sub("", name)
    return _BALANCE_RE.sub("", stripped).strip()


@dataclass(frozen=True)
class Place:
    state_abbr: str
    geoid: str
    name: str  # NAME as published, with its LSAD descriptor
    base_name: str
    lsad: str
    funcstat: str
    lat: float
    lon: float

    @property
    def incorporated(self) -> bool:
        return self.funcstat == "A" and self.lsad not in _INCORPORATED_LSAD_EXCLUDED


@dataclass(frozen=True)
class CountySubdivision:
    state_abbr: str
    geoid: str  # state, county and subdivision code: "0911005910" (Bloomfield town, CT)
    name: str  # NAME as published ("Bloomfield town")
    base_name: str  # "Bloomfield"
    funcstat: str
    lat: float
    lon: float
    area_m2: float = 0.0  # ALAND + AWATER

    @property
    def county_fips(self) -> str:
        """The county (in Connecticut, the planning region) the subdivision lies in."""
        return self.geoid[:5]

    @property
    def active(self) -> bool:
        return self.funcstat in _ACTIVE_COUSUB_FUNCSTAT


def cousub_base_name(name: str) -> str:
    """A county subdivision NAME without its descriptor: "Bloomfield town" -> "Bloomfield"."""
    return _COUSUB_SUFFIX_RE.sub("", name).strip()


@dataclass(frozen=True)
class GazCounty:
    state_abbr: str
    geoid: str
    name: str  # NAME as published ("Madison County")
    lat: float
    lon: float


def _read_gazetteer(path: Path, sha256: str | None) -> list[dict[str, str]]:
    path = resolve_reference(path)
    if not path.exists():
        raise FileNotFoundError(f"Gazetteer file not found: {path}")
    if sha256 is not None:
        actual = sha256_file(path)
        if actual != sha256:
            raise ValueError(f"{path}: sha256 {actual} != expected {sha256}")
    with open_zip(path, max_members=5, max_member_bytes=20_000_000) as zf:
        members = [n for n in zf.names() if n.endswith(".txt")]
        if len(members) != 1:
            raise ValueError(f"{path}: expected one .txt member, found {members}")
        text = zf.read(members[0]).decode("utf-8")
    lines = text.splitlines()
    header = [h.strip() for h in lines[0].split("|")]
    return [
        dict(zip(header, (c.strip() for c in line.split("|")), strict=False))
        for line in lines[1:]
        if line.strip()
    ]


def _file_key(path: Path) -> tuple[str, int, int]:
    resolved = resolve_reference(path)
    try:
        st = resolved.stat()
    except OSError:
        return (str(resolved), -1, -1)
    return (str(resolved.resolve()), st.st_size, st.st_mtime_ns)


class Gazetteer:
    """Census 2025 Gazetteer places and counties (and, when loaded, county subdivisions):
    internal points by state and name.

    Read-only after load, so one instance can be shared.
    """

    def __init__(
        self,
        places: list[Place],
        counties: list[GazCounty],
        cousubs: list[CountySubdivision] | None = None,
    ) -> None:
        self._places: dict[tuple[str, str], list[Place]] = {}
        for p in places:
            for key in {normalize_place(p.base_name), normalize_place(p.name)}:
                self._places.setdefault((p.state_abbr, key), []).append(p)
        self._counties = {c.geoid: c for c in counties}
        self._county_names = {(c.state_abbr, normalize_place(c.name)): c for c in counties}
        self._cousubs: dict[tuple[str, str], list[CountySubdivision]] = {}
        self._cousub_areas = {c.geoid: c.area_m2 for c in cousubs or []}
        for c in cousubs or []:
            if c.active:
                for key in {normalize_place(c.base_name), normalize_place(c.name)}:
                    self._cousubs.setdefault((c.state_abbr, key), []).append(c)
        self.place_count = len(places)
        self.county_count = len(self._counties)
        self.cousub_count = len(cousubs or [])

    @classmethod
    def load(
        cls,
        place_zip: Path = PLACES_ZIP,
        county_zip: Path = GAZ_COUNTIES_ZIP,
        *,
        cousub_zip: Path | None = None,
        verify_sha256: bool = True,
    ) -> Gazetteer:
        """Load the files; verify_sha256 checks them against the pinned checksums first.
        cousub_zip (the county-subdivision file, which ImportContext.gazetteer() downloads on
        first use) adds the county subdivisions, so that a New England town or a township a source
        names can be placed.

        The parsed files are kept for the life of the process (keyed by path, size and mtime), so
        repeated imports and tests parse them once.
        """
        key = (
            _file_key(place_zip),
            _file_key(county_zip),
            _file_key(cousub_zip) if cousub_zip is not None else None,
            verify_sha256,
        )
        cached = _LOADED.get(key)
        if cached is None:
            cached = cls._read(place_zip, county_zip, cousub_zip, verify_sha256=verify_sha256)
            _LOADED[key] = cached
        return cached

    @classmethod
    def _read(
        cls, place_zip: Path, county_zip: Path, cousub_zip: Path | None, *, verify_sha256: bool
    ) -> Gazetteer:
        places = [
            Place(
                state_abbr=r["USPS"],
                geoid=r["GEOID"],
                name=r["NAME"],
                base_name=place_base_name(r["NAME"]),
                lsad=r["LSAD"],
                funcstat=r["FUNCSTAT"],
                lat=round(float(r["INTPTLAT"]), 6),
                lon=round(float(r["INTPTLONG"]), 6),
            )
            for r in _read_gazetteer(place_zip, PLACES_SHA256 if verify_sha256 else None)
        ]
        counties = [
            GazCounty(
                state_abbr=r["USPS"],
                geoid=r["GEOID"],
                name=r["NAME"],
                lat=round(float(r["INTPTLAT"]), 6),
                lon=round(float(r["INTPTLONG"]), 6),
            )
            for r in _read_gazetteer(county_zip, GAZ_COUNTIES_SHA256 if verify_sha256 else None)
        ]
        cousubs = (
            [
                CountySubdivision(
                    state_abbr=r["USPS"],
                    geoid=r["GEOID"],
                    name=r["NAME"],
                    base_name=cousub_base_name(r["NAME"]),
                    funcstat=r["FUNCSTAT"],
                    lat=round(float(r["INTPTLAT"]), 6),
                    lon=round(float(r["INTPTLONG"]), 6),
                    area_m2=float(r.get("ALAND") or 0) + float(r.get("AWATER") or 0),
                )
                for r in _read_gazetteer(
                    cousub_zip, reference.COUNTY_SUBDIVISIONS.sha256 if verify_sha256 else None
                )
            ]
            if cousub_zip is not None
            else None
        )
        return cls(places, counties, cousubs)

    def place_entry(self, abbr: str, name: str) -> Place | None:
        """The place called name in state abbr, or None when there is none or the name is ambiguous.

        A name shared by an incorporated place and a CDP means the incorporated place.
        """
        found = self._places.get((abbr.upper(), normalize_place(name)), [])
        unique = list({p.geoid: p for p in found}.values())
        if len(unique) > 1:
            unique = [p for p in unique if p.incorporated]
        return unique[0] if len(unique) == 1 else None

    def place(self, abbr: str, name: str) -> tuple[float, float, str] | None:
        """(lat, lon, base name) of the place's internal point."""
        p = self.place_entry(abbr, name)
        return (p.lat, p.lon, p.base_name) if p else None

    def cousub_entry(
        self, abbr: str, name: str, county_fips: str | None = None
    ) -> CountySubdivision | None:
        """The county subdivision that is a government called name in state abbr (in county_fips,
        when given), or None when there is none or the name is ambiguous ("Salem Township" in
        Pennsylvania is five townships, one of them in Luzerne County)."""
        found = self._cousubs.get((abbr.upper(), normalize_place(name)), [])
        unique = list({c.geoid: c for c in found}.values())
        if county_fips is not None:
            unique = [c for c in unique if c.county_fips == county_fips]
        return unique[0] if len(unique) == 1 else None

    def cousub_area(self, geoid: str) -> float | None:
        """Land plus water area (m2) of the county subdivision geoid, active or not, or None when
        it is not in the loaded file."""
        return self._cousub_areas.get(geoid)

    def has_locality(self, abbr: str, name: str, county_fips: str | None = None) -> bool:
        """True when geocode()'s step 3 can place name as a locality: a Census place, or (when
        loaded) a county subdivision (in county_fips, when given). For an importer choosing which
        of a source's place names to send as GeocodeRequest.locality."""
        return (
            self.place_entry(abbr, name) is not None
            or self.cousub_entry(abbr, name, county_fips) is not None
        )

    def county(self, abbr: str, name: str) -> tuple[float, float, str] | None:
        """(lat, lon, GEOID) of a county's internal point, by its full name ("Madison County")."""
        c = self._county_names.get((abbr.upper(), normalize_place(name)))
        return (c.lat, c.lon, c.geoid) if c else None

    def county_full_name(self, fips: str) -> str | None:
        """The county's full name ("Madison County", "Richland Parish"), for display."""
        c = self._counties.get(fips)
        return c.name if c else None


_FileKey = tuple[str, int, int]
_LOADED: dict[tuple[_FileKey, _FileKey, _FileKey | None, bool], Gazetteer] = {}


# ---------------------------------------------------------------------------- the chain


def _round(v: float) -> float:
    return round(float(v), 6)


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (haversine, mean Earth radius)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_008.8 * math.asin(min(1.0, math.sqrt(h)))


def _county_for_point(
    counties: CountyIndex, lat: float, lon: float, preferred: str | None
) -> County | None:
    """preferred if the point lies in it (with the validate tolerance), else point-in-polygon."""
    if preferred is not None and counties.get(preferred) and counties.contains(preferred, lat, lon):
        return counties.get(preferred)
    return counties.lookup(lat, lon)


def _state_ok(req: GeocodeRequest, abbr: str) -> bool:
    return req.state_abbr is None or req.state_abbr.upper() == abbr


def _from_source(
    req: GeocodeRequest, src_lat: float, src_lon: float, counties: CountyIndex
) -> GeocodeResult | None:
    lat, lon = _round(src_lat), _round(src_lon)
    precision: Precision = req.source_precision or "site"
    named = (
        counties.by_name(req.state_abbr, req.county_name)
        if req.state_abbr and req.county_name
        else None
    )
    county: County | None
    if precision in POINT_IN_POLYGON_PRECISIONS or (precision == "street" and named is None):
        county = counties.lookup(lat, lon)
        if county is None:
            return None
    else:
        county = named
    state = county.state_abbr if county else (req.state_abbr or "").upper()
    if not state or not _state_ok(req, state):
        return None
    if precision in COUNTY_CHECKED_PRECISIONS:
        if county is None or not counties.contains(county.fips, lat, lon):
            return None
    elif precision in ("state", "unknown") or not counties.in_state(state, lat, lon):
        return None
    return GeocodeResult(
        lat=lat,
        lon=lon,
        precision=precision,
        method="source_coords",
        state_abbr=state,
        county_fips=county.fips if county else None,
        county_name=county.name if county else None,
        city=req.city or req.locality,
        postcode=req.postcode,
        street=req.street,
        matched_address=None,
        confidence=CONFIDENCE["source_coords"],
    )


def _street_forms(m: CensusMatch) -> set[tuple[str, ...]]:
    """The ways the match's street may be written: its components in order, with the street type
    optional (the input may leave it out). A directional is never optional: for "500 8th St" the
    Census offered 500 E 8TH ST, 13 km from Meta Jeffersonville, and for "1626 County Line Road"
    1626 E COUNTY LINE RD, 6.7 km from the campus on W County Line Rd. A directional the input does
    not state makes another street, and the chain falls through to the Gazetteer."""
    optional = {"suffixType"}
    forms: set[tuple[str, ...]] = {()}
    for key, tokens in m.street_parts:
        forms = {f + tokens for f in forms} | (forms if key in optional else set())
    forms.discard(())
    return forms


def _street_agrees(m: CensusMatch, wanted: tuple[str, ...]) -> bool:
    """The input street (no house number) is one of the match's street forms. A directional, type
    or qualifier the input states must be the match's own: "S Litchfield Rd" is not "LITCHFIELD
    RD BYP", and "Co Rd 42" is not "COUNTY CT". The one exception is a numbered route, where the
    input may add a trailing directional the Census leaves out ("Hwy 54 W", "STATE RTE 54")."""
    if not m.street_parts or not wanted:
        return False
    forms = _street_forms(m)
    if wanted in forms:
        return True
    parts = dict(m.street_parts)
    name = parts.get("streetName", ())
    numbered_route = (
        parts.get("preType") in _NUMBERED_ROUTE_TYPES
        and bool(name)
        and all(t.isdigit() for t in name)
        and not parts.get("suffixDirection")
    )
    return (
        numbered_route and len(wanted) > 1 and wanted[-1] in _DIRECTIONALS and wanted[:-1] in forms
    )


def _place_agrees(req: GeocodeRequest, m: CensusMatch, oneline: str) -> bool:
    """The match's ZIP is the input's, or its city is the input's city (or, when no city could be
    parsed, a run of words of the input text)."""
    if req.postcode and m.postcode and req.postcode == m.postcode:
        return True
    if not m.city:
        return False
    city = normalize_place(m.city)
    if req.city:
        return normalize_place(req.city) == city
    return f" {city} " in f" {normalize_place(oneline)} "


def _census_precision(req: GeocodeRequest, m: CensusMatch, oneline: str) -> Precision | None:
    """The precision of a match that agrees with the request (state, city or ZIP, street): "address"
    when the house number is also the input's and the matched range spans fewer than
    CENSUS_LONG_RANGE numbers, else "street". None when it does not agree."""
    if not m.state_abbr or m.state_abbr.upper() != (req.state_abbr or "").upper():
        return None
    if not _place_agrees(req, m, oneline):
        return None
    street = req.street or oneline.split(",", 1)[0]
    wanted = street_name_tokens(street)
    candidates = [wanted]
    city = street_tokens(m.city)
    if city and len(wanted) > len(city) and wanted[-len(city) :] == city:
        candidates.append(wanted[: -len(city)])  # "5800 EDGEWOOD RD SW CEDAR RAPIDS, IA 52404"
    if not any(_street_agrees(m, w) for w in candidates):
        return None
    number = house_number(street)
    if number is None or number != m.house_number:
        return "street"
    span = m.range_span
    return "street" if span is not None and span >= CENSUS_LONG_RANGE else "address"


def _from_census(
    req: GeocodeRequest,
    oneline: str,
    census: CensusGeocoder,
    counties: CountyIndex,
    places: PlaceIndex | None,
) -> GeocodeResult | None:
    """The Census match that agrees with the request, or None (no agreeing match, or agreeing
    matches more than CENSUS_AMBIGUOUS_M apart), so the chain falls back to the Gazetteer. Its
    city is the Census place that contains the point, if any; the match's own city is the USPS
    city ("FAIRFAX" for QTS Cedar Rapids, whose point is in Cedar Rapids city)."""
    agreeing = [
        (m, precision)
        for m in census.matches(oneline)
        if (precision := _census_precision(req, m, oneline)) is not None
    ]
    if not agreeing:
        return None
    first = agreeing[0][0]
    if any(
        distance_m(first.lat, first.lon, m.lat, m.lon) > CENSUS_AMBIGUOUS_M for m, _ in agreeing
    ):
        return None
    m, precision = next(((m, p) for m, p in agreeing if p == "address"), agreeing[0])
    lat, lon = _round(m.lat), _round(m.lon)
    county = _county_for_point(counties, lat, lon, m.county_fips)
    if county is None or not _state_ok(req, county.state_abbr):
        return None
    if not counties.contains(county.fips, lat, lon):
        return None
    return GeocodeResult(
        lat=lat,
        lon=lon,
        precision=precision,
        method="census_geocoder",
        state_abbr=county.state_abbr,
        county_fips=county.fips,
        county_name=county.name,
        city=places.city_at(lat, lon, county.state_abbr) if places is not None else None,
        postcode=m.postcode or req.postcode,
        street=(street_title(m.street) if precision == "address" else None)
        or req.street
        or street_title(m.street),
        matched_address=m.matched_address or None,
        confidence=CONFIDENCE[precision],
    )


def _from_gazetteer(
    req: GeocodeRequest, state: str, locality: str, gazetteer: Gazetteer, counties: CountyIndex
) -> GeocodeResult | None:
    named = counties.by_name(state, req.county_name) if req.county_name else None
    place = gazetteer.place_entry(state, locality)
    if place is None:
        return _from_cousub(req, state, locality, named, gazetteer, counties)
    if not counties.in_state(state, place.lat, place.lon):
        return None
    if named is not None and not counties.contains(named.fips, place.lat, place.lon):
        return None  # Abilene's point is in Taylor County, not the stated Shackelford County
    return GeocodeResult(
        lat=place.lat,
        lon=place.lon,
        precision="locality",
        method="gazetteer",
        state_abbr=state,
        county_fips=named.fips if named else None,
        county_name=named.name if named else None,
        city=place.base_name,
        postcode=req.postcode,
        street=None,
        matched_address=f"{place.name}, {state}",
        confidence=CONFIDENCE["locality"],
    )


def _from_cousub(
    req: GeocodeRequest,
    state: str,
    locality: str,
    named: County | None,
    gazetteer: Gazetteer,
    counties: CountyIndex,
) -> GeocodeResult | None:
    """A locality that is no Census place may be a county subdivision: Bloomfield, CT is a town,
    and the Gazetteer's only place there is Blue Hills CDP. Its internal point, at "locality",
    with municipality set and the county it lies in (a planning region in Connecticut)."""
    sub = gazetteer.cousub_entry(state, locality, named.fips if named else None)
    if sub is None:
        return None
    county = counties.get(sub.county_fips)
    if county is None or not counties.contains(county.fips, sub.lat, sub.lon):
        return None
    return GeocodeResult(
        lat=sub.lat,
        lon=sub.lon,
        precision="locality",
        method="gazetteer",
        state_abbr=state,
        county_fips=county.fips,
        county_name=county.name,
        city=None,
        postcode=req.postcode,
        street=None,
        matched_address=f"{sub.name}, {state}",
        confidence=CONFIDENCE["locality"],
        municipality=sub.base_name,
    )


def _from_postal_city(
    req: GeocodeRequest, state: str, city: str, gazetteer: Gazetteer, counties: CountyIndex
) -> GeocodeResult | None:
    """A postal city's internal point, at "locality", only inside a stated county, and with no
    city: the post office's town is not where the site is. AWS Berwick (mailed to Berwick, PA,
    Columbia County) is in Salem Township, Luzerne County; Meta Aiken (Trenton, SC, Edgefield
    County) is in Aiken County; Microsoft SAT40 (San Antonio) is 27 km from San Antonio's point.
    Without a stated county nothing tells which county the site is in, so None."""
    named = counties.by_name(state, req.county_name) if req.county_name else None
    place = gazetteer.place_entry(state, city)
    if named is None or place is None or not counties.contains(named.fips, place.lat, place.lon):
        return None
    return GeocodeResult(
        lat=place.lat,
        lon=place.lon,
        precision="locality",
        method="gazetteer",
        state_abbr=state,
        county_fips=named.fips,
        county_name=named.name,
        city=None,
        postcode=req.postcode,
        street=None,
        matched_address=f"{place.name}, {state}",
        confidence=CONFIDENCE["locality"],
    )


def _from_county(
    req: GeocodeRequest, state: str, county_name: str, counties: CountyIndex
) -> GeocodeResult | None:
    county = counties.by_name(state, county_name)
    if county is None:
        return None
    lat, lon = counties.centroid(county.fips)
    if not counties.contains(county.fips, lat, lon):
        return None
    return GeocodeResult(
        lat=lat,
        lon=lon,
        precision="county",
        method="county_centroid",
        state_abbr=county.state_abbr,
        county_fips=county.fips,
        county_name=county.name,
        city=None,
        postcode=req.postcode,
        street=None,
        matched_address=None,
        confidence=CONFIDENCE["county"],
    )


def geocode(
    req: GeocodeRequest,
    *,
    census: CensusGeocoder | None,
    gazetteer: Gazetteer,
    counties: CountyIndex,
    places: PlaceIndex | None = None,
) -> GeocodeResult | None:
    """Run the chain; the first step that succeeds wins (07 §6.5). census=None skips step 2;
    places (atlas.geo.places.PlaceIndex) names the city of a Census match, which otherwise has
    none.

    Source coordinates that fail the county or state check fall through to the later steps. A
    request without a state skips step 2: a Census match could then come from any state.
    """
    state = req.state_abbr.upper() if req.state_abbr else None
    if req.lat is not None and req.lon is not None:
        result = _from_source(req, req.lat, req.lon, counties)
        if result is not None:
            return result
    if census is not None and req.oneline and state:
        result = _from_census(req, req.oneline, census, counties, places)
        if result is not None:
            return result
    if state and req.locality:
        result = _from_gazetteer(req, state, req.locality, gazetteer, counties)
        if result is not None:
            return result
    elif state and req.city:
        result = _from_postal_city(req, state, req.city, gazetteer, counties)
        if result is not None:
            return result
    if state and req.county_name:
        return _from_county(req, state, req.county_name, counties)
    return None
