"""The Census place that contains a point (07 §6.5): the city a record names.

reference/census/cb_2025_us_place_500k.zip (1:500,000 cartographic boundary file, 32,629
incorporated places and census designated places of the states, DC, Puerto Rico and the island
areas, with GEOID, NAME, NAMELSAD, LSAD and STUSPS in EPSG:4269) gives the place a point lies in.
Its GEOIDs are the Gazetteer's. A postal city is not that place: OpenAI's Lordstown site, mailed to
Warren, lies in Lordstown village, and STACK NVA02, mailed to Manassas, in Innovation CDP. So when
a record has a point and no source states its place, its city comes from here, or is left out
when no place contains the point (Vantage TX1, west of San Antonio city).

The lines are generalized, and a Census address point lies on a street that is often the line
itself, so a point counts as inside a place only when it lies more than PLACE_MARGIN_DEG inside
it: Microsoft Fairwater Atlanta's Census point is 5 m inside Fayetteville city on these lines and
outside it on the Census Geocoder's own. Near a boundary, and on one two places share, no place is
returned and the record names its county. Unlike the county rule, there is no tolerance outward:
a point just outside a place is not in it.

The file is loaded on use (DuckDB), not on import.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from atlas.geo.counties import resolve_reference, sha256_file
from atlas.geo.duck import connect

if TYPE_CHECKING:
    import duckdb

PLACE_POLYGONS_ZIP = Path("reference/census/cb_2025_us_place_500k.zip")
PLACE_POLYGONS_SHA256 = "ce0e4019ecd4123d03d53aaa936eed0459b82e3e14b89a3dcd4d5e8b3308627d"
PLACE_POLYGONS_BYTES = 23_057_021
PLACE_POLYGONS_SHP = "cb_2025_us_place_500k.shp"
# LSAD codes of places that are not municipalities: census designated place, comunidad, zona urbana.
UNINCORPORATED_LSAD = frozenset({"57", "55", "62"})
# How far inside a place's generalized boundary a point must lie to be named by it (about 100 m).
# A Census address point lies on the street's centre line, which is often the city line: of the
# seed's 31, seven lie within 15 m of a place line, thirteen 106 m to 6.7 km inside a place.
PLACE_MARGIN_DEG = 0.001


@dataclass(frozen=True)
class CensusPlace:
    """One Census place: an incorporated place or a census designated place (CDP)."""

    geoid: str
    name: (
        str  # NAME as published ("Cedar Rapids", "Innovation", "Nashville-Davidson ... (balance)")
    )
    lsad_name: str  # NAMELSAD ("Cedar Rapids city", "Innovation CDP")
    lsad: str
    state_abbr: str

    @property
    def incorporated(self) -> bool:
        return self.lsad not in UNINCORPORATED_LSAD

    @property
    def base_name(self) -> str:
        """The name without its LSAD descriptor, as the Gazetteer step writes city."""
        from atlas.geocode import place_base_name

        return place_base_name(self.name)


class PlaceIndex:
    """Point-in-polygon over the Census place polygons."""

    def __init__(self, con: duckdb.DuckDBPyConnection, places: dict[str, CensusPlace]) -> None:
        self._con = con
        self._places = places

    @classmethod
    def load(cls, path: Path = PLACE_POLYGONS_ZIP, *, verify_sha256: bool = True) -> PlaceIndex:
        """Load a place file: PLACE_POLYGONS_ZIP, checked against PLACE_POLYGONS_SHA256 first, or
        (verify_sha256=False, for test fixtures) any zip whose shapefile is named after it."""
        path = resolve_reference(path)
        if not path.exists():
            raise FileNotFoundError(f"place file not found: {path}")
        if verify_sha256:
            actual = sha256_file(path)
            if actual != PLACE_POLYGONS_SHA256:
                raise ValueError(f"{path}: sha256 {actual} != expected {PLACE_POLYGONS_SHA256}")
            shp = PLACE_POLYGONS_SHP
        else:
            shp = f"{path.stem}.shp"
        con = connect()
        con.execute(
            "CREATE TABLE places AS SELECT GEOID AS geoid, NAME AS pname, NAMELSAD AS lsad_name,"
            " LSAD AS lsad, STUSPS AS st, geom::GEOMETRY AS geom,"
            " ST_XMin(geom) AS xmin, ST_YMin(geom) AS ymin, ST_XMax(geom) AS xmax,"
            " ST_YMax(geom) AS ymax FROM ST_Read(?)",
            [f"/vsizip/{path.resolve()}/{shp}"],
        )
        rows = con.execute(
            "SELECT geoid, pname, lsad_name, lsad, st FROM places ORDER BY geoid"
        ).fetchall()
        places = {
            geoid: CensusPlace(
                geoid=geoid, name=name, lsad_name=lsad_name, lsad=lsad, state_abbr=st
            )
            for geoid, name, lsad_name, lsad, st in rows
        }
        return cls(con, places)

    def close(self) -> None:
        self._con.close()

    def __len__(self) -> int:
        return len(self._places)

    def get(self, geoid: str) -> CensusPlace | None:
        return self._places.get(geoid)

    def containing(
        self, lat: float, lon: float, *, margin_deg: float = PLACE_MARGIN_DEG
    ) -> CensusPlace | None:
        """The place the point lies in, more than margin_deg inside its boundary, or None (no
        place, a point near a boundary, or one on a boundary two places share)."""
        return self.containing_many([(lat, lon)], margin_deg=margin_deg)[0]

    def containing_many(
        self, points: Sequence[tuple[float, float]], *, margin_deg: float = PLACE_MARGIN_DEG
    ) -> list[CensusPlace | None]:
        """containing() for (lat, lon) points, in one SQL join."""
        if not points:
            return []
        lats = [float(p[0]) for p in points]
        lons = [float(p[1]) for p in points]
        rows = self._con.execute(
            "WITH pts AS (SELECT unnest(range(?::BIGINT)) AS i, unnest(?::DOUBLE[]) AS lat,"
            " unnest(?::DOUBLE[]) AS lon)"
            " SELECT pts.i, min(p.geoid), count(*),"
            " min(ST_Distance(ST_Boundary(p.geom), ST_Point(pts.lon, pts.lat)))"
            " FROM pts JOIN places p"
            " ON pts.lon BETWEEN p.xmin AND p.xmax AND pts.lat BETWEEN p.ymin AND p.ymax"
            " AND ST_Intersects(p.geom, ST_Point(pts.lon, pts.lat))"
            " GROUP BY pts.i",
            [len(points), lats, lons],
        ).fetchall()
        out: list[CensusPlace | None] = [None] * len(points)
        for i, geoid, n, inside_by in rows:
            if n == 1 and inside_by > margin_deg:
                out[int(i)] = self._places[geoid]
        return out

    def city_at(self, lat: float, lon: float, state_abbr: str | None = None) -> str | None:
        """The base name of the place the point lies in ("Cedar Rapids", "Innovation"), for
        location.city and the canonical name; None when no place of state_abbr (if given)
        contains it."""
        place = self.containing(lat, lon)
        if place is None or (state_abbr is not None and place.state_abbr != state_abbr.upper()):
            return None
        return place.base_name
