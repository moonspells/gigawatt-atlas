# Geocoding fixtures from the seed import

Small extracts of U.S. Census Bureau data, a work of the U.S. Government and in the public domain.
They hold the cases the seed check of 2026-10-08 found (07 §6.5): a postal city that is not the
place a point lies in, a long TIGER edge, a New England town.

## `census/`

Recorded responses of the Census Geocoder (`geographies/onelineaddress`, benchmark
`Public_AR_Current`, vintage `Current_Current`, layers `Counties`), retrieved 2026-10-08 (UTC) by the
seed import's live Epoch AI run and copied unchanged from its saved inputs. Each file is named like
the importer's cache, `{sha256 of the address}.json`, as in `tests/fixtures/census/`.

| File | Address | Match |
|---|---|---|
| `4896177da29422b92d9eed28f04623787e064816f7383003422880ce085c9868.json` | 601 Kuna Mora Rd, Kuna ID 83634 | 601 KUNA MORA RD on the edge numbered 1229-1 (a long range: `street`) |
| `34e13ab45781143c0b534db734cfd32b25398b966dbdb23a5374753041d29bfb.json` | 9590 Hornbaker Rd, Manassas, VA 20109 | on 9512-10542 (`street`); the point is in Innovation CDP |
| `f15478e22c7f1da71f4988e45514dfaab4415fc51ab1305cd61c9a2cd677b74d.json` | 6200 76th Ave SW, Fairfax, IA 52228 | USPS city FAIRFAX; the point is in Cedar Rapids city |
| `aeda0c57d105f0d707c2a7c0f437c8db2b06ea12521a7b61ba16b8f8ec71626f.json` | 2300 Hallock Young Rd, Warren, OH 44481 | USPS city WARREN; the point is in Lordstown village |
| `1343cbf56ce2144bdf1ca71c0f7f4e3cbbcdea71890a7b81eaf6f34b82c1af5c.json` | 11110 State St, Omaha, NE 68142 | USPS city OMAHA; the point is in no place |
| `4ad3f1802be9da014f867c046cc92c40271f3ef1c3717821b1e1a5f11e22c4ea.json` | 14720 Omicron Dr, San Antonio, TX 78245 | USPS city SAN ANTONIO; the point is in no place |
| `43c37fc21edab486cc5d30630fbdee081ee336b1e07ddfc9f9c9142873ad823e.json` | 1125 Electron Ave, Berwick, PA 18603 | none |
| `c3bf81d8ac386c4177a0178bc8e8fba6733f2d5539a69f0c9316427881af2819.json` | 7601 State Hwy 105, Trenton, SC 29847 | none |
| `f15f7b16e6bd05ed6c9b452cdac333b18e6123b472cd296e854006aebd301cb6.json` | 15000 Lambda Drive, San Antonio, TX 78245 | none |

## `places/places_sample.zip`

Eight polygons of the 2025 cartographic boundary file of places,
<https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_place_500k.zip> (retrieved 2026-10-09,
SHA-256 `ce0e4019ecd4123d03d53aaa936eed0459b82e3e14b89a3dcd4d5e8b3308627d`), with their columns
`STATEFP`, `PLACEFP`, `GEOID`, `NAME`, `NAMELSAD`, `STUSPS` and `LSAD`, written unchanged as one
shapefile (EPSG:4269) with DuckDB spatial: Cedar Rapids city and Fairfax city (IA), Lordstown village
and Warren city (OH), Memphis city (TN), Innovation CDP (VA), Berwick borough (PA) and Omaha city
(NE).

## `gazetteer/2025_Gaz_cousubs_sample.zip`

Fifteen rows, with the header, of the 2025 Gazetteer file of county subdivisions,
<https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_Gaz_cousubs_national.zip>
(retrieved 2026-10-09, SHA-256 `5e498edbf27e2426ec6661415851ea42c6a635f3cc3a2b79eccc2c3c19b2892e`),
unchanged: Bloomfield, Hartford and Waterford towns (CT), the five Salem townships of Pennsylvania,
Somerset town (NY), and Bloomfield entries that are not governments (a CCD in Kentucky, precincts in
Illinois, a village coextensive with an incorporated place in Wisconsin) or are ambiguous (two
Bloomfield towns in Wisconsin).
