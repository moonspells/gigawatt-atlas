"""County lookups against the Census 2025 cartographic boundary file (07 §3.6 rule 5, §6.5).

reference/census/cb_2025_us_county_500k.zip (1:500,000) has 3,235 features (states, DC and
territories) with GEOID, NAME, NAMELSAD, STUSPS, STATEFP and geom in EPSG:4269 (NAD83). NAD83 and
WGS84 differ by about a metre in the US, far below the 0.003-degree tolerance, so points are used
as lon/lat as is.

Point-in-polygon uses the 1:500,000 file because the 1:5,000,000 generalization moves county lines
by more than the tolerance in places that matter: it puts Amazon IAD100-IAD103 (Prince William
County, VA, per the Census Geocoder and PNNL) in Manassas city. The 1:5,000,000 file stays in
reference/census for map display and still loads (KNOWN_COUNTY_FILES), with the same attributes.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from atlas.geo.duck import connect

if TYPE_CHECKING:
    import duckdb

COUNTIES_ZIP = Path("reference/census/cb_2025_us_county_500k.zip")
COUNTIES_SHA256 = "aa976c00b181939755d0da757f4c7c2dc0103c3b3b4530fb2a91c2bb62fc777c"
COUNTIES_SHP = "cb_2025_us_county_500k.shp"
COUNTIES_5M_ZIP = Path("reference/census/cb_2025_us_county_5m.zip")
COUNTIES_5M_SHA256 = "faec522080681e79be5be435c981009a77891206ff8a7f1d142f3bf5da9ebd74"
# The county files load accepts: SHA-256 -> the shapefile inside the zip.
KNOWN_COUNTY_FILES = {
    COUNTIES_SHA256: COUNTIES_SHP,
    COUNTIES_5M_SHA256: "cb_2025_us_county_5m.shp",
}
DEFAULT_TOLERANCE_DEG = 0.003

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NAME_SUFFIXES = (
    " city and borough",
    " planning region",
    " census area",
    " municipality",
    " municipio",
    " borough",
    " county",
    " parish",
    " city",
)
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class County:
    fips: str
    name: str
    state_abbr: str
    state_fips: str


@dataclass(frozen=True)
class _Entry:
    county: County
    lsad_name: str  # NAMELSAD, normalized ("baltimore city", "loudoun county")
    base_name: str  # NAME, normalized ("baltimore", "loudoun")
    bbox: tuple[float, float, float, float]  # xmin, ymin, xmax, ymax


def _norm(s: str) -> str:
    s = s.casefold().replace(".", "").replace("'", "").replace("\u2019", "")
    return _SPACE_RE.sub(" ", s).strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_reference(path: Path) -> Path:
    """A relative reference path that does not exist here is looked up in the repo root."""
    if path.exists() or path.is_absolute():
        return path
    candidate = _REPO_ROOT / path
    return candidate if candidate.exists() else path


class CountyIndex:
    """Point-in-polygon, name lookup and centroids over the Census county polygons."""

    def __init__(self, con: duckdb.DuckDBPyConnection, entries: dict[str, _Entry]) -> None:
        self._con = con
        self._entries = entries
        self._by_state: dict[str, list[_Entry]] = {}
        for entry in sorted(entries.values(), key=lambda e: e.county.fips):
            self._by_state.setdefault(entry.county.state_abbr, []).append(entry)

    @classmethod
    def load(cls, path: Path = COUNTIES_ZIP, *, verify_sha256: bool = True) -> CountyIndex:
        """Load a county file: COUNTIES_ZIP (1:500,000, the one rule 5 uses) or the 1:5,000,000
        file. verify_sha256 checks it against KNOWN_COUNTY_FILES first; without the check the
        shapefile is named after the zip."""
        path = resolve_reference(path)
        if not path.exists():
            raise FileNotFoundError(f"county file not found: {path}")
        if verify_sha256:
            actual = sha256_file(path)
            shp = KNOWN_COUNTY_FILES.get(actual)
            if shp is None:
                raise ValueError(
                    f"{path}: sha256 {actual} is not a known Census county file "
                    f"(expected {COUNTIES_SHA256} for {COUNTIES_ZIP.name})"
                )
        else:
            shp = f"{path.stem}.shp"
        source = f"/vsizip/{path.resolve()}/{shp}"
        con = connect()
        con.execute(
            "CREATE TABLE counties AS SELECT GEOID AS fips, NAME AS cname, NAMELSAD AS lsad,"
            " STUSPS AS st, STATEFP AS sf, geom::GEOMETRY AS geom,"
            " ST_XMin(geom) AS xmin, ST_YMin(geom) AS ymin, ST_XMax(geom) AS xmax,"
            " ST_YMax(geom) AS ymax FROM ST_Read(?)",
            [source],
        )
        entries: dict[str, _Entry] = {}
        rows = con.execute(
            "SELECT fips, cname, lsad, st, sf, xmin, ymin, xmax, ymax FROM counties ORDER BY fips"
        ).fetchall()
        for fips, cname, lsad, st, sf, xmin, ymin, xmax, ymax in rows:
            entries[fips] = _Entry(
                County(fips=fips, name=cname, state_abbr=st, state_fips=sf),
                _norm(lsad),
                _norm(cname),
                (xmin, ymin, xmax, ymax),
            )
        return cls(con, entries)

    def close(self) -> None:
        self._con.close()

    def __len__(self) -> int:
        return len(self._entries)

    def all(self) -> list[County]:
        """Every county, sorted by FIPS."""
        return [self._entries[f].county for f in sorted(self._entries)]

    def get(self, fips: str) -> County | None:
        entry = self._entries.get(fips)
        return entry.county if entry else None

    def lookup(self, lat: float, lon: float) -> County | None:
        """The county containing the point, or None."""
        return self.lookup_many([(lat, lon)])[0]

    def lookup_many(self, points: Sequence[tuple[float, float]]) -> list[County | None]:
        """Counties for (lat, lon) points in one SQL join. On a shared boundary the lower FIPS wins."""
        if not points:
            return []
        lats = [float(p[0]) for p in points]
        lons = [float(p[1]) for p in points]
        rows = self._con.execute(
            "WITH pts AS (SELECT unnest(range(?::BIGINT)) AS i, unnest(?::DOUBLE[]) AS lat,"
            " unnest(?::DOUBLE[]) AS lon)"
            " SELECT pts.i, min(c.fips) FROM pts JOIN counties c"
            " ON pts.lon BETWEEN c.xmin AND c.xmax AND pts.lat BETWEEN c.ymin AND c.ymax"
            " AND ST_Intersects(c.geom, ST_Point(pts.lon, pts.lat))"
            " GROUP BY pts.i",
            [len(points), lats, lons],
        ).fetchall()
        out: list[County | None] = [None] * len(points)
        for i, fips in rows:
            out[int(i)] = self._entries[fips].county
        return out

    def _distance_ok(self, fips: str, lat: float, lon: float, tolerance_deg: float) -> bool:
        row = self._con.execute(
            "SELECT ST_Distance(geom, ST_Point(?, ?)) <= ? FROM counties WHERE fips = ?",
            [lon, lat, tolerance_deg, fips],
        ).fetchone()
        return bool(row and row[0])

    @staticmethod
    def _near_bbox(entry: _Entry, lat: float, lon: float, tolerance_deg: float) -> bool:
        xmin, ymin, xmax, ymax = entry.bbox
        return (
            xmin - tolerance_deg <= lon <= xmax + tolerance_deg
            and ymin - tolerance_deg <= lat <= ymax + tolerance_deg
        )

    def contains(
        self, fips: str, lat: float, lon: float, *, tolerance_deg: float = DEFAULT_TOLERANCE_DEG
    ) -> bool:
        """True when the point is inside county fips or within tolerance_deg of it."""
        entry = self._entries.get(fips)
        if entry is None or not self._near_bbox(entry, lat, lon, tolerance_deg):
            return False
        return self._distance_ok(fips, lat, lon, tolerance_deg)

    def in_state(
        self, abbr: str, lat: float, lon: float, *, tolerance_deg: float = DEFAULT_TOLERANCE_DEG
    ) -> bool:
        """True when the point is inside, or within tolerance_deg of, a county of the state."""
        for entry in self._by_state.get(abbr, []):
            if self._near_bbox(entry, lat, lon, tolerance_deg) and self._distance_ok(
                entry.county.fips, lat, lon, tolerance_deg
            ):
                return True
        return False

    def by_name(self, abbr: str, name: str) -> County | None:
        """Case-insensitive county lookup within a state.

        Accepts the bare name ("Loudoun"), the legal name ("Loudoun County", "Richland Parish",
        "Juneau City and Borough", "Baltimore city") and "City of Richmond". A bare name shared by
        a county and an independent city ("Baltimore", "Richmond") means the county.
        """
        entries = self._by_state.get(abbr.upper(), [])
        wanted = _norm(name)
        if wanted.startswith("city of "):
            wanted = wanted.removeprefix("city of ") + " city"
        for entry in entries:
            if entry.lsad_name == wanted:
                return entry.county
        base = wanted
        for suffix in _NAME_SUFFIXES:
            if base.endswith(suffix) and len(base) > len(suffix):
                base = base.removesuffix(suffix)
                break
        matches = [e for e in entries if e.base_name == base]
        if not matches:
            return None
        counties = [e for e in matches if not e.lsad_name.endswith(" city")]
        return (counties or matches)[0].county

    def name_matches(self, fips: str, name: str) -> bool:
        """True when name is county fips's name, bare or legal ("Loudoun", "Loudoun County",
        "Manassas city", "City of Manassas"), ignoring case, periods and apostrophes."""
        entry = self._entries.get(fips)
        if entry is None:
            return False
        wanted = _norm(name)
        if wanted.startswith("city of "):
            wanted = wanted.removeprefix("city of ") + " city"
        return wanted in (entry.lsad_name, entry.base_name)

    def centroid(self, fips: str) -> tuple[float, float]:
        """(lat, lon) of ST_PointOnSurface for the county, rounded to 6 decimals."""
        row = self._con.execute(
            "SELECT ST_Y(p), ST_X(p) FROM (SELECT ST_PointOnSurface(geom) AS p FROM counties"
            " WHERE fips = ?)",
            [fips],
        ).fetchone()
        if row is None:
            raise KeyError(fips)
        return round(float(row[0]), 6), round(float(row[1]), 6)
