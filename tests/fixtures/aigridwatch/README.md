# AI GridWatch fixtures

Three small subsets of the AI GridWatch data center project tracker, and two Census samples:

- `projects.json`, used by `tests/sources/test_aigridwatch.py`: one row per stage;
- `seed-2026-10-08.json`, used by `tests/sources/test_aigridwatch_seed.py`: rows of the
  2026-10-08 seed import that the record checks found mapped wrongly;
- `check2-2026-10-08.json`, used by `tests/sources/test_aigridwatch_check2.py`: rows of the second
  seed import (2026-10-09) that its record checks found mapped wrongly;
- `places/agw_places.zip` and `gazetteer/agw_cousubs.zip`: the Census place polygons and county
  subdivisions those rows' points and localities need (below).

Attribution: AI GridWatch (<https://aigridwatch.com>), AI GridWatch data center project tracker.
License: Creative Commons Attribution 4.0 International (CC BY 4.0),
<https://creativecommons.org/licenses/by/4.0/>.

## projects.json: what was taken and what was changed

- Source: <https://aigridwatch.com/data/projects.json>, generated 2026-10-07, retrieved 2026-10-07
  (1,300,431 bytes, 284 projects).
- The top-level metadata (`name`, `generated`, `license`, `license_url`, `attribution`,
  `source_page`, `caveat`, `schema`) is copied unchanged. `count` and `verified_count` are set to
  the subset's numbers (12 and 11).
- 12 projects, copied field for field: one for each of the ten stages (two for Approved, one of
  them without coordinates), plus the one row marked `verified: false`.
- Each project's `events` list is cut to at most three entries. The entries kept were chosen so
  that the fixture names no private individuals.
- `meta-leap-lebanon-in`'s `note` ends at "announced Feb 2026." (the clause "groundbreaking
  construction reported ongoing through mid-2026 ..." is cut), so the row stays the fixture's
  Proposed stage: a row whose note reports construction under way is held for review
  (`tests/sources/test_aigridwatch_check2.py` reads the uncut row).

## seed-2026-10-08.json: what was taken and what was changed

- Source: <https://aigridwatch.com/data/projects.json>, generated 2026-10-08, retrieved
  2026-10-08T20:50:12Z by the seed import (1,302,329 bytes, 285 projects, sha256
  `16330d941c519db1db0747666e3ce319344571b2be0079535f0e5284a4ccab47`).
- The top-level metadata is copied unchanged; `count` and `verified_count` are the subset's (22
  and 22).
- 22 projects, field for field except as listed: Dickerson, 900 Conshohocken Road, EdgeCore
  Louisa, King of Prussia, Microsoft Person County, PORTS (Piketon), Microsoft Three Mile Island,
  Midtown St. Louis, Deep Green Lansing, Project Riverjump, Project Taurus, Karis Plum Farms, DC
  Blox Warren Township, Antelope Data Campus, Project Iron Spur, the six PA DEP rows from
  `amazon-web-services-unnamed-data-center-center-township-pa` to
  `zediker-station-data-center-south-strabane-township-pa` (each under another row's id in the
  file), and the real AWS Center Township row (`…-52`).
- Each `events` list keeps only the entries the tests read (by date); rows not listed keep all.
- Personal names are taken out, so the fixture names no individual: the developer's name in 900
  Conshohocken Road's `owner` (emptied) and `note`, and in King of Prussia's `operator` (now "MLP
  Ventures"); officials' names in Project Riverjump's `note` and withdrawal entry ("the mayor"),
  Karis Plum Farms' `note` ("The governor"), PORTS' groundbreaking entry ("SoftBank's chairman")
  and Project Iron Spur's vote entry (the two commissioners who voted in support).

## check2-2026-10-08.json: what was taken and what was changed

- Source: the same file as `seed-2026-10-08.json` (generated 2026-10-08, sha256
  `16330d941c519db1db0747666e3ce319344571b2be0079535f0e5284a4ccab47`), as saved by the second seed
  import on 2026-10-09. The top-level metadata is copied unchanged; `count` and `verified_count` are
  the subset's (21 and 21).
- 21 projects, field for field except as listed: Plaza 500, Aligned Pataskala, Posey County
  (Boberg/Hoenert Roads), TeraWulf Cayuga, Hanover Township (Starpointe), Compass Red Oak, Mason
  County, Wolcott, Metrobloks, Smithfield Gateway, STAMP Double Reed (Alabama, NY), Powder Mill
  Works, Meta Lebanon (its note uncut), Urbana, Google Botetourt, Avison Young's unnamed Wilkes-Barre
  project, 100 Jersey Avenue (PSE&G), AWS Salem Township, QTS Richmond 2 and 3, and CoreSite DE3
  (Globeville-Elyria-Swansea).
- Each `events` list keeps only the entries the tests read (by date); Hanover, Mason County,
  Wolcott, Meta Lebanon and the two rows without events keep all. The importer gives the same
  records and review items for these rows as for the full rows (checked on the seed's inputs).
- Personal names are taken out, so the fixture names no individual: the organizers in Posey
  County's 2026-06-03 entry; the neighbours who sued and an economic-development official in
  Wolcott's note and entries, and the commissioners' names in its moratorium entry; a councilor in
  two of Metrobloks' texts; the dissenting council member in Urbana's 2026-03-03 entry; the NAACP
  president in 100 Jersey Avenue's entry (each replaced by the role, or cut).

## places/agw_places.zip

28 polygons of the 2025 cartographic boundary file of places,
<https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_place_500k.zip> (retrieved 2026-10-09,
SHA-256 `ce0e4019ecd4123d03d53aaa936eed0459b82e3e14b89a3dcd4d5e8b3308627d`), with their columns
`STATEFP`, `PLACEFP`, `GEOID`, `NAME`, `NAMELSAD`, `STUSPS` and `LSAD`, written unchanged as one
shapefile (EPSG:4269) with DuckDB spatial: the places the three fixtures' points lie in or their
localities name (for example Indianapolis city (balance), East Stroudsburg borough, Lansing village,
Wilkes-Barre city, Lincolnia CDP, Linton Hall CDP). U.S. Census Bureau, public domain.

## gazetteer/agw_cousubs.zip

35 rows, with the header, of the 2025 Gazetteer file of county subdivisions,
<https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_Gaz_cousubs_national.zip>
(retrieved 2026-10-09, SHA-256 `5e498edbf27e2426ec6661415851ea42c6a635f3cc3a2b79eccc2c3c19b2892e`),
unchanged: the fifteen rows of `tests/fixtures/geocode/gazetteer/2025_Gaz_cousubs_sample.zip`, and
the townships and towns the three fixtures name (Hanover and South Strabane townships of Washington
County, Smithfield, Salem of Luzerne, North Beaver and Mahoning, Upper Merion, Plymouth, Center of
Indiana County, Warren and Decatur of Marion County, Lansing and Alabama towns of New York, and
others). U.S. Census Bureau, public domain.
