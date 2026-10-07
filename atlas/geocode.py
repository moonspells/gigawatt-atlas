"""The geocoding chain (07 §6.5): source coordinates, Census Geocoder, Gazetteer place, county.

geocode() stops at the first step that succeeds:

1. Source coordinates, at the precision the source states.
2. Census Geocoder (onelineaddress): "address" when the matched house number equals the input's,
   otherwise "street". County from the response GEOID when the point lies in it.
3. Census Gazetteer place internal point: "locality".
4. County by name (CountyIndex.by_name) at the polygon's point on surface: "county".
5. None: the caller files a review item.

County FIPS comes from point-in-polygon when the precision is address or better, otherwise from the
stated county name. Every result is checked against the county polygons with the tolerance that
`atlas validate` rule 5 uses, so a result from geocode() always passes that rule.

Importing this module does no I/O and loads neither DuckDB nor httpx.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode

from atlas.geo.counties import resolve_reference, sha256_file
from atlas.geo.fips import STATES, state_by_abbr
from atlas.safezip import open_zip
from atlas.schema.record import GeocodeMethod, Precision
from atlas.text import strip_invisible

if TYPE_CHECKING:
    import httpx

    from atlas.geo.counties import County, CountyIndex


PLACES_ZIP = Path("reference/census/2025_Gaz_place_national.zip")
PLACES_SHA256 = "49644173a453469d9bd77fb7a493b027f87567e209edaf2078aac7543ac2ee29"
GAZ_COUNTIES_ZIP = Path("reference/census/2025_Gaz_counties_national.zip")
GAZ_COUNTIES_SHA256 = "4c90d0f805779923b5958ab13d0c1e9b99fe4932b786bfcf75dd739bb2dcb4ea"

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
CENSUS_PARAMS = {
    "benchmark": "Public_AR_Current",
    "vintage": "Current_Current",
    "format": "json",
    "layers": "Counties",
}
# The Census Geocoder answers HTTP 400 for an address longer than this.
CENSUS_MAX_ADDRESS = 100

# Confidence of the location per step (07 §3.4: derived in code, 0.90 from an address, else 0.60;
# imported from an open dataset, 0.70).
CONFIDENCE: dict[str, float] = {
    "source_coords": 0.70,
    "address": 0.90,
    "street": 0.80,
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
_ABBREVIATIONS = {"st": "saint", "ste": "sainte", "mt": "mount", "ft": "fort"}
_INCORPORATED_LSAD_EXCLUDED = frozenset({"57", "55", "62"})  # CDP, comunidad, zona urbana

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
    """What is known about a place. Set only the fields the source gives."""

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


@dataclass(frozen=True)
class CensusMatch:
    """The first address match of a Census Geocoder response."""

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
    s = _SPACE_RE.sub(" ", strip_invisible(text)).strip(" ,")
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


def parse_census_response(doc: object) -> CensusMatch | None:
    """The first address match of an onelineaddress response, or None when there is none."""
    if not isinstance(doc, dict) or not isinstance(doc.get("result"), dict):
        raise GeocoderError("not a Census Geocoder response (no result object)")
    matches = doc["result"].get("addressMatches")
    if not isinstance(matches, list):
        raise GeocoderError("not a Census Geocoder response (no addressMatches list)")
    if not matches:
        return None
    m = matches[0]
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

    @staticmethod
    def normalize(address: str) -> str:
        return _SPACE_RE.sub(" ", strip_invisible(address)).strip()

    def cache_path(self, address: str) -> Path:
        digest = hashlib.sha256(self.normalize(address).encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def onelineaddress(self, address: str) -> CensusMatch | None:
        """The first match for address, or None (no match, empty, or over 100 characters).

        Raises atlas.net.FetchError when the service fails after retries, and GeocoderError when
        it answers with something else; a 4xx answer counts as no match and is not cached.
        """
        from atlas.net import FetchError, fetch  # httpx on use

        text = self.normalize(address)
        if not text or len(text) > CENSUS_MAX_ADDRESS:
            return None
        path = self.cache_path(text)
        if path.exists():
            try:
                cached = parse_census_response(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, GeocoderError):
                path.unlink()  # a damaged cache entry is fetched again
            else:
                self.cache_hits += 1
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
            if e.status is not None and 400 <= e.status < 500:
                return None
            raise
        try:
            doc = json.loads(result.content)
        except ValueError as e:
            raise GeocoderError(f"Census Geocoder: response is not JSON: {e}") from e
        match = parse_census_response(doc)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(result.content)
        return match


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
    """Census 2025 Gazetteer places and counties: internal points by state and name.

    Read-only after load, so one instance can be shared.
    """

    def __init__(self, places: list[Place], counties: list[GazCounty]) -> None:
        self._places: dict[tuple[str, str], list[Place]] = {}
        for p in places:
            for key in {normalize_place(p.base_name), normalize_place(p.name)}:
                self._places.setdefault((p.state_abbr, key), []).append(p)
        self._counties = {c.geoid: c for c in counties}
        self._county_names = {(c.state_abbr, normalize_place(c.name)): c for c in counties}
        self.place_count = len(places)
        self.county_count = len(self._counties)

    @classmethod
    def load(
        cls,
        place_zip: Path = PLACES_ZIP,
        county_zip: Path = GAZ_COUNTIES_ZIP,
        *,
        verify_sha256: bool = True,
    ) -> Gazetteer:
        """Load both files; verify_sha256 checks them against the committed checksums first.

        The parsed files are kept for the life of the process (keyed by path, size and mtime), so
        repeated imports and tests parse them once.
        """
        key = (_file_key(place_zip), _file_key(county_zip), verify_sha256)
        cached = _LOADED.get(key)
        if cached is None:
            cached = cls._read(place_zip, county_zip, verify_sha256=verify_sha256)
            _LOADED[key] = cached
        return cached

    @classmethod
    def _read(cls, place_zip: Path, county_zip: Path, *, verify_sha256: bool) -> Gazetteer:
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
        return cls(places, counties)

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

    def county(self, abbr: str, name: str) -> tuple[float, float, str] | None:
        """(lat, lon, GEOID) of a county's internal point, by its full name ("Madison County")."""
        c = self._county_names.get((abbr.upper(), normalize_place(name)))
        return (c.lat, c.lon, c.geoid) if c else None

    def county_full_name(self, fips: str) -> str | None:
        """The county's full name ("Madison County", "Richland Parish"), for display."""
        c = self._counties.get(fips)
        return c.name if c else None


_LOADED: dict[tuple[tuple[str, int, int], tuple[str, int, int], bool], Gazetteer] = {}


# ---------------------------------------------------------------------------- the chain


def _round(v: float) -> float:
    return round(float(v), 6)


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


def _from_census(
    req: GeocodeRequest, oneline: str, census: CensusGeocoder, counties: CountyIndex
) -> GeocodeResult | None:
    m = census.onelineaddress(oneline)
    if m is None:
        return None
    if m.state_abbr and not _state_ok(req, m.state_abbr.upper()):
        return None
    lat, lon = _round(m.lat), _round(m.lon)
    wanted = house_number(req.street or oneline)
    precision: Precision = (
        "address" if wanted is not None and wanted == m.house_number else "street"
    )
    county = _county_for_point(counties, lat, lon, m.county_fips)
    if county is None or not _state_ok(req, county.state_abbr):
        return None
    if not counties.contains(county.fips, lat, lon):
        return None
    city = req.city if req.city and m.city and req.city.casefold() == m.city.casefold() else None
    return GeocodeResult(
        lat=lat,
        lon=lon,
        precision=precision,
        method="census_geocoder",
        state_abbr=county.state_abbr,
        county_fips=county.fips,
        county_name=county.name,
        city=city or (m.city.title() if m.city else None),
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
    place = gazetteer.place_entry(state, locality)
    if place is None or not counties.in_state(state, place.lat, place.lon):
        return None
    named = counties.by_name(state, req.county_name) if req.county_name else None
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
) -> GeocodeResult | None:
    """Run the chain; the first step that succeeds wins (07 §6.5). census=None skips step 2.

    Source coordinates that fail the county or state check fall through to the later steps.
    """
    state = req.state_abbr.upper() if req.state_abbr else None
    if req.lat is not None and req.lon is not None:
        result = _from_source(req, req.lat, req.lon, counties)
        if result is not None:
            return result
    if census is not None and req.oneline:
        result = _from_census(req, req.oneline, census, counties)
        if result is not None:
            return result
    if state and req.locality:
        result = _from_gazetteer(req, state, req.locality, gazetteer, counties)
        if result is not None:
            return result
    if state and req.county_name:
        return _from_county(req, state, req.county_name, counties)
    return None
