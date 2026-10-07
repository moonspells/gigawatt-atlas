# Reference files

Files committed so that validation, geocoding and tests run offline and reproducibly. Each is
checked against the SHA-256 below (the county file also in code: `atlas/geo/counties.py`).

| File | Source URL | Bytes | SHA-256 | License |
|---|---|---|---|---|
| `census/cb_2025_us_county_5m.zip` | <https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_county_5m.zip> | 2,983,552 | `faec522080681e79be5be435c981009a77891206ff8a7f1d142f3bf5da9ebd74` | U.S. Census Bureau, public domain, retrieved 2026-10-07 |
| `census/2025_Gaz_place_national.zip` | <https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_Gaz_place_national.zip> | 1,214,053 | `49644173a453469d9bd77fb7a493b027f87567e209edaf2078aac7543ac2ee29` | U.S. Census Bureau, public domain, retrieved 2026-10-07 |
| `census/2025_Gaz_counties_national.zip` | <https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_Gaz_counties_national.zip> | 138,993 | `4c90d0f805779923b5958ab13d0c1e9b99fe4932b786bfcf75dd739bb2dcb4ea` | U.S. Census Bureau, public domain, retrieved 2026-10-07 |

`cb_2025_us_county_5m` has 3,235 features (the states, DC and the territories) in EPSG:4269, with
the columns `GEOID`, `NAME`, `NAMELSAD`, `STUSPS`, `STATEFP` and `geom`. Connecticut appears as its
nine planning regions, which are county equivalents in the 2025 files.

The two Gazetteer files arrive with the Epoch AI and AI GridWatch importers (geocoding, 07 §6.5).

To refresh a file: download it into an empty directory, check its size and SHA-256, replace it
here, and update this table, the constant in code and `tests/test_repo_files.py` in one pull
request.
