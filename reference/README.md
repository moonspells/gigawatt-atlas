# Reference files

Files committed so that validation, geocoding and tests run offline and reproducibly. Each is
checked against the SHA-256 below (the county file also in code: `atlas/geo/counties.py`).

| File | Source URL | Bytes | SHA-256 | License |
|---|---|---|---|---|
| `census/cb_2025_us_county_500k.zip` | <https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_county_500k.zip> | 11,758,981 | `aa976c00b181939755d0da757f4c7c2dc0103c3b3b4530fb2a91c2bb62fc777c` | U.S. Census Bureau, public domain, retrieved 2026-10-08 |
| `census/cb_2025_us_county_5m.zip` | <https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_county_5m.zip> | 2,983,552 | `faec522080681e79be5be435c981009a77891206ff8a7f1d142f3bf5da9ebd74` | U.S. Census Bureau, public domain, retrieved 2026-10-07 |
| `census/2025_Gaz_place_national.zip` | <https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_Gaz_place_national.zip> | 1,214,053 | `49644173a453469d9bd77fb7a493b027f87567e209edaf2078aac7543ac2ee29` | U.S. Census Bureau, public domain, retrieved 2026-10-07 |
| `census/2025_Gaz_counties_national.zip` | <https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_Gaz_counties_national.zip> | 138,993 | `4c90d0f805779923b5958ab13d0c1e9b99fe4932b786bfcf75dd739bb2dcb4ea` | U.S. Census Bureau, public domain, retrieved 2026-10-07 |

Both county files have the same 3,235 features (the states, DC and the territories) in EPSG:4269,
with the columns `GEOID`, `NAME`, `NAMELSAD`, `STUSPS`, `STATEFP` and `geom`. Connecticut appears
as its nine planning regions, which are county equivalents in the 2025 files.

County point-in-polygon (validation rule 5, 07 §3.6, and the importers' county FIPS) uses the
1:500,000 file, `cb_2025_us_county_500k`. The 1:5,000,000 generalization moves county lines by more
than the 0.003-degree tolerance in places that matter: it puts Amazon IAD100 to IAD103 in Manassas
city instead of Prince William County, VA, where the Census Geocoder and PNNL place them
(`tests/geo/test_counties_500k.py`). Elsewhere the two files agree on all 1,382 PNNL centroids.
`cb_2025_us_county_5m` stays for map display (07 §10).

The two Gazetteer files arrive with the Epoch AI and AI GridWatch importers (geocoding, 07 §6.5).

## Downloaded on first use

Two more Census files are needed by the importers and are not committed, to keep the repository
small (owner decision of 2026-10-09). Both are U.S. Census Bureau works in the public domain:

| File | Source URL | Bytes | SHA-256 | License |
|---|---|---|---|---|
| `cb_2025_us_place_500k.zip` | <https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_place_500k.zip> | 23,057,021 | `ce0e4019ecd4123d03d53aaa936eed0459b82e3e14b89a3dcd4d5e8b3308627d` | U.S. Census Bureau, public domain, Last-Modified 2026-04-23, checked 2026-10-09 |
| `2025_Gaz_cousubs_national.zip` | <https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_Gaz_cousubs_national.zip> | 1,478,116 | `5e498edbf27e2426ec6661415851ea42c6a635f3cc3a2b79eccc2c3c19b2892e` | U.S. Census Bureau, public domain, Last-Modified 2025-09-10, checked 2026-10-09 |

- The place polygons (32,629 incorporated places and census designated places) give a record's
  `location.city`: the Census place that contains its point, when the point lies more than
  0.001° (about 100 m) inside the place's line, never the postal city of its address
  (`atlas/geo/places.py`, `ImportContext.places()`; used by the OpenStreetMap importer, and by the
  Epoch AI importer for a Census Geocoder match).
- The county-subdivision Gazetteer (36,427 New England towns, townships and other county
  subdivisions) places a town or township a source names, with its county and `municipality`
  (`atlas/geocode.py`, `ImportContext.gazetteer()`; on for every import that geocodes a place
  name: AI GridWatch's localities. Epoch AI states no town or township).

An import that needs one uses the copy in `{--cache-dir}/reference/census/` (by default
`.cache/atlas/reference/census/`, which git ignores). When there is none, it downloads the file
from the URL above through `atlas.net` (the address guard, robots.txt, the byte cap and content
type of every fetch), checks its size and SHA-256 against the pin in `atlas/geo/reference.py`
before writing anything, and writes it atomically: a download that is not the pinned file fails the
run and leaves no file. Later runs reuse the copy, checked again each time; a copy that differs
fails the run. `atlas import … --offline` with no copy fails with a message naming the file: put
the file in that directory first (download it, check the SHA-256 above), or run once without
`--offline`. `--places PATH` reads the place polygons from another path, checked the same way.
Tests use small samples (`tests/fixtures/geocode/`) and never download these.

To refresh a file: download it into an empty directory, check its size and SHA-256, replace it
here (or, for a downloaded file, in the cache), and update its table above, the constants in code
(`atlas/geo/counties.py`, `atlas/geocode.py`, `atlas/geo/reference.py`) and the tests that pin
them (`tests/test_repo_files.py`, `tests/geo/test_counties_500k.py`, `tests/geo/test_places.py`,
`tests/geo/test_reference.py`) in one pull request.
