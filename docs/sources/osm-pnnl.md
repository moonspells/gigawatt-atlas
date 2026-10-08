# OpenStreetMap seed and the PNNL cross-check

`atlas import osm` builds the operating baseline of the Atlas (07 §4.1, §4.2): every US data center
in OpenStreetMap, dissolved into one `campus` record per site, and checked against the PNNL IM3 Open
Source Data Center Atlas. The code is `atlas/sources/osm.py` (the importer), `atlas/dissolve.py`
(buildings to campuses) and `atlas/sources/pnnl.py` (the PNNL join). Both inputs are ODbL 1.0
(07 §5.1); every record credits them in `sources[]`.

```sh
uv run atlas import osm                                     # Overpass + the public PNNL GeoJSON
uv run atlas import osm --input saved-overpass.json --pnnl centroids.geojson --offline
uv run atlas import osm --no-pnnl --overpass-url https://overpass.example/api/interpreter
```

| Option | Meaning |
|---|---|
| `--input PATH` | Read a saved Overpass JSON response instead of querying (no element minimum). |
| `--pnnl PATH` | A PNNL file: the public GeoJSON, or a CSV in the MSD-LIVE layout. Without it, the public GeoJSON is fetched. |
| `--no-pnnl` | Skip the cross-check. |
| `--overpass-url URL` | Repeatable; replaces the default endpoint list. |
| `--dissolve-m M` | The same-operator join distance between bounding boxes, default 300 m (also the same-address distance of rule 3). |

The common `atlas import` options (`--records`, `--review-dir`, `--receipts-dir`, `--cache-dir`,
`--now`, `--dry-run`, `--offline`) are described in the README. The importer's name, match key and
receipt file are `osm`. It owns the `external_ids` keys `osm` and `pnnl_im3` and the review files
`review/queue/osm.jsonl` and `review/queue/pnnl.jsonl`.

## 1. The Overpass query

```overpassql
[out:json][timeout:180];
area["ISO3166-1"="US"][admin_level=2]->.us;
(
  nwr["telecom"="data_center"](area.us);
  nwr["building"="data_center"](area.us);
  nwr["construction:telecom"="data_center"](area.us);
  nwr["proposed:telecom"="data_center"](area.us);
  nwr["construction"="data_center"](area.us);
);
out tags bb;
```

Two changes from the query in research 02 §2.1:

- `out tags bb` instead of `out center tags`. The campus rule needs each way's extent. The center
  used everywhere is the bounding-box midpoint, which is what `center` returns.
- A `construction=data_center` clause. About 30 US objects are tagged `building=construction` +
  `construction=data_center`, often with `telecom=data_center` as well; the crosswalk maps them to
  `under_construction` (docs/status-crosswalk.md).

## 2. Endpoints and failover

The query is POSTed (`data=<query>`) to each endpoint in turn:

1. `https://overpass-api.de/api/interpreter`
2. `https://maps.mail.ru/osm/tools/overpass/api/interpreter`
3. `https://overpass.kumi.systems/api/interpreter`
4. `https://overpass.private.coffee/api/interpreter`

Each request goes through `atlas.net.fetch` (the address guard, a 240 s timeout, a 50 MB cap,
`application/json` only, no per-host delay, one retry after 10 s on HTTP 429, 5xx or a connection
error). robots.txt is not consulted, because this is an API rather than crawling. An endpoint has
failed, and the next one is tried, when the request raises, the status is not 200, the body is not
JSON, a `remark` contains "runtime error", `osm3s.timestamp_osm_base` is missing, or there are fewer
than 1,000 elements. Overpass can answer HTTP 200 with a partial result, which is why the last three
checks exist; the US result had 1,886 elements on 2026-10-07. When every endpoint fails, the import
stops with a `FetchError` naming each failure, and `atlas import` exits 1.

The response is kept at `.cache/atlas/raw/overpass/{sha256}.json`. The receipt's input `overpass`
records the endpoint that answered, `upstream_version` = `timestamp_osm_base`, and license
`ODbL-1.0`. With `--input`, the URL is the first configured endpoint and `retrieved_at` is `--now`.

## 3. Objects

Every element becomes an `OsmObject(ref, lat, lon, bounds, tags, kind)`. `ref` is `node/1`, `way/1`
or `relation/1`; `lat`/`lon` is the node position or the bounding-box midpoint. Tag values have
invisible characters removed and whitespace collapsed. `kind` is:

| kind | Rule | US count (2026-10-07) |
|---|---|---|
| `campus` | a way or relation with `telecom=data_center` and no `building` tag, or with `building=no` | 88 |
| `building` | any other way or relation | 1,663 |
| `point` | a node | 135 |

## 4. Dissolving objects into campuses

`dissolve(objects, radius_m=300)` joins objects with union-find. Distances are gaps between
bounding boxes (a node is its point; 0 when the boxes touch or overlap), not distances between
centers, so two large halls that touch join although their centers are 320 m apart (Google,
Douglas County, GA, ways 844352473 and 844352474).

1. **Campus containment.** An object whose center lies inside a campus object's bounding box,
   expanded by 30 m, joins that campus. If it lies in several, it joins the smallest one.
   *Operator guard:* the object does not join when the two carry comparable operator information
   that disagrees: different `operator:wikidata` ids when both have one, otherwise normalized
   operators (`text.normalize_org`) of which neither starts the other, word by word. "Amazon" and
   "Amazon Web Services us-east-2 datacenter" both agree with "Amazon Web Services", so AWS
   buildings join AWS campus polygons tagged that way (Hilliard, OH, way 460067225, and way
   697021949). A bounding box overstates a polygon, and in Ashburn the box of the fenced "Amazon
   Web Services Datacenter Complex" (way 460053028) covers Equinix DC17 and DC18; without the guard
   the whole Equinix row of buildings would chain into the AWS campus. On the 2026-10-07 snapshot
   the guard blocks 8 containment joins, all between different companies (Equinix in an AWS box,
   AWS in Microsoft and Google boxes, Compass in a True North box).
2. **Operator distance.** Two non-campus objects whose boxes are within `radius_m` of each other
   join when they have the same `normalize_org(operator)` or the same `operator:wikidata`.
3. **Neighbours without an operator.** Two non-campus objects that both have no operator
   information join when their boxes are within 50 m and they have the same `normalize_name(name)`
   or neither has a name, or when they have the same `addr:housenumber` and `addr:street` and
   their boxes are within `radius_m`. Most unnamed `telecom=data_center` buildings carry no
   operator, and without this rule each one became its own record: 32 records "Data center
   (Venango, PA)" within 213 m, and 11 "Blockfusion Niagra Falls" at 5380 Frontier Avenue. The gap
   is 50 m because at 100 m unnamed buildings chain across 1.5 km (8 buildings in Kendall County,
   IL). An object without an operator does not join a neighbour that has one this way (no such
   pair with the same name was found on 2026-10-07).

Two campus objects never join each other directly (they can meet through a shared member). Joins
are transitive, so a row of same-operator buildings 250 m apart forms one cluster; the widest
cluster on 2026-10-07 spans about 1.6 km (16 AWS objects in Ashburn). Each group of candidates is
swept in latitude order, so only pairs that overlap in latitude are measured.

On the 2026-10-07 snapshot (1,886 objects) the rules give 1,117 clusters. The earlier rules
(center distances, an exact operator guard, no rule 3) gave 1,367; box gaps alone give 1,280, the
word-prefix guard 1,278, and rule 3 the rest (its address clause adds 4 joins). Single objects
without an operator fell from 601 clusters to 393, and canonical names used by more than one
record from 79 (414 records) to 51 (188 records).

The **representative** is the campus object (the largest, if a cluster has two), else the way or
relation with the largest bounding box, else the node with the lowest id. Members and clusters are
sorted by (type, numeric id), and the result does not depend on input order (tested with shuffles).

## 5. Record mapping

One `campus` record per cluster. `external_ids.osm` lists every member ref, and those refs are the
candidate's match values, so a later run updates the same record (07 §2.1: records are never
deleted; a cluster that disappears upstream becomes a `removed_upstream` review item).

| Field | Value |
|---|---|
| `record_type` / `scope` | `campus`; `in_scope`, or `out_of_scope` outside the 50 states and DC or for a telecom site (the record is kept, 07 §2.2; see below) |
| `canonical_name` | base = the representative's `name`, else the most common member name, else "{operator} data center", else "Data center". The operator is prefixed unless the base already starts with the operator, its `operator:short`, or the operator's first word ("Lumen Ashburn" for Lumen Technologies stays as is; "CyrusOne NVA14" operated by PowerHouse becomes "PowerHouse CyrusOne NVA14"). Then " ({city}, {ST})" with `location.city`, else with the county's full Census name from the Gazetteer ("Taylor County", "Manassas city", "Orleans Parish"), as in the Epoch and AI GridWatch importers. A bare county name reads as a city and sometimes is one elsewhere: Taylor County, TX is Abilene, while the city of Taylor, TX is 300 km away. |
| `aliases[]` | the other distinct member names (`osm_name`, source s1) |
| `parties.operator` / `owner` | the most common `operator` / `owner` tag (source s1) |
| `location.lat`, `lon` | the campus object's bounding-box center, else the mean of the member centers; 7 decimals |
| `location.precision` | `footprint` if any member is a way or relation, else `site` |
| `location.geocode_method` / `geometry_ref` | `osm` / `osm:{representative}` |
| `location.street`, `city`, `postcode` | "{addr:housenumber} {addr:street}", `addr:city`, `addr:postcode`: the representative's value, else the most common member value |
| `location.county_fips`, `county_name`, `state_abbr` | point-in-polygon against the Census 2025 counties; `field_meta["/location/county_fips"]` = 0.90, `derived` |
| `buildings[]` | one per building or point member: `ref` "osm:{ref}", `name`, `sqft` from PNNL, `phase_id` when the cluster has phases |
| `capacity` | see below |
| `status_history`, `phases` | see below; `field_meta["/status"]` = 0.60, `imported`, s1 |
| `evidence_level` / `purpose` | `reported` / `unknown`, or `telecom` for a telecom site (then s1 also supports `/purpose`) |
| `site.building_sqft`, `site.acreage` | from PNNL (section 7) |
| `sources[0]` (s1) | `https://www.openstreetmap.org/{representative}`, "OpenStreetMap contributors", title "OpenStreetMap {representative}", `open_dataset`, `ODbL-1.0`, supports `/canonical_name`, `/aliases`, `/parties`, `/location`, `/buildings`, `/capacity`, `/status_history/0` |
| `sources[1]` (s2) | PNNL, only when a PNNL row matched (section 7) |

`apply_rollup` runs on every candidate, and `atlas import` validates every record before writing it
(07 §3.6). Only these tags reach a record: `name`, `operator`, `operator:short`,
`operator:wikidata` (for joins), `owner`, the `addr:*` fields above, `it_power`,
`input:electricity`, `start_date`, `opening_date` and the status tags. Contact tags such as `phone`,
`email` and `website` are never copied.

### Telecom sites

07 §2.2 puts telecom central offices and edge sites out of scope, and OSM `telecom=data_center`
also tags cable landing stations and telephone company offices. A cluster is a telecom site when
every member that has a `name`, `alt_name` or `operator` names a cable landing station, a landing
station, a central office, a wire center, or a telephone company or cooperative ("Telephone Co",
"Telephone Coop", "Cooperative Telephone"), or is tagged `telecom=exchange` or
`telecom=central_office`. Unnamed members count neither way, and one data center name in the
cluster keeps it in scope. The record gets `scope: "out_of_scope"`, `purpose: "telecom"` and an
`out_of_scope` review item naming the member and the words that matched. On 2026-10-07 this
catches 10 sites: 8 cable landing stations (Tuckerton, Manasquan, Wall Township, Shirley, Norma
Beach, Myrtle Beach) and 2 telephone cooperative offices. "AT&T Center" in San Diego, operated by
"American Telephone & Telegraph", stays in scope: neither its name nor its operator says what the
building is.

### Capacity

`it_power` becomes `it_mw` and `input:electricity` becomes `facility_mw` (07 §2.4 keeps the bases
apart). Values are "N MW"; "kW", "GW" and a bare number (read as MW) are accepted. Anything else,
or a value outside (0, 10000] MW, is a `unit_parse` review item. A value is summed over the building
and point members only when every one of them has a usable value (a campus-level tag on the campus
object would be used instead); otherwise the field is null. `mw_as_stated` always lists the raw
tags, for example "OSM it_power: Amazon IAD-78 45 MW; Amazon IAD-79 47 MW; Amazon IAD-80 38 MW |
OSM input:electricity: …". `field_meta["/capacity/it_mw"]` and `["/capacity/facility_mw"]` =
0.70, `imported`, s1, when set. Every value seen in the US data was "N MW".

### Status, events and phases

Each member's tags go through `crosswalk.from_osm_tags` (proposed, then construction, then
operating). Members are grouped by the resulting status.

- **One status (almost every cluster).** One event: seq 1, the crosswalk's status, event
  `first_reported`, `as_of` = the snapshot date (the date of `timestamp_osm_base`, day precision),
  source s1, note "Tagged {tag} in OpenStreetMap; status per 07 §4.7". If the status is operating
  and the representative, or else the earliest member, has a `start_date` of the form YYYY,
  YYYY-MM or YYYY-MM-DD between 1990-01-01 and the snapshot date, the event is `energized` at that
  date instead. Earlier dates (old buildings converted to data centers, such as "1938") are
  ignored, because validation accepts nothing before 1990.
- **Several statuses (12 clusters on 2026-10-07).** Each status group becomes a phase,
  `osm-operating`, `osm-under_construction` or `osm-proposed`, named after its members ("Under
  construction in OpenStreetMap: NTT VA8"), with one event per phase and `buildings[].phase_id`
  set. The rollup (07 §2.3) then gives the most advanced active status, so an operating campus with
  a building under construction stays `operating` and is `expanding`, rather than the whole campus
  showing as under construction.
- **`opening_date`** on a group that is not operating adds a planned `energized` event, which never
  changes the status.
- On later runs, a `first_reported` event keeps the earliest `as_of` already stored for the same
  status and source, so weekly runs never move `dates.first_reported` or `operating_since`
  forward. The phase is not compared: when a building under construction appears next to an
  operating campus, or later goes, the operating event moves between no phase and `osm-operating`
  but its status has not changed. A status change replaces the event; recording transitions as
  new events is the M2 diff (07 §4.2 step 5).

## 6. PNNL inputs

PNNL v2026.02.09 (DOI 10.57931/3017294, ODbL 1.0) is derived from OSM, with county and state from
2024 Census boundaries and `sqft` = polygon area.

- **The public web-map file** (default):
  `https://immm-sfa.github.io/datacenter-atlas/im3_datacenter_centroids.geojson`, 1,382 Point
  features (1,239 building, 94 point, 49 campus) whose properties are only `state_abb`, `county`,
  `operator`, `name`, `sqft` and `type`. It has **no OSM id and no FIPS code**, so the join is
  spatial. Fetched with the crawler policy (robots.txt, 2 s per host), kept at
  `.cache/atlas/raw/pnnl/{sha256}.geojson`.
- **A CSV in the MSD-LIVE layout** (`id, state, state_abb, state_id, county, county_id, ref,
  operator, name, sqft, lat, lon, type`), passed with `--pnnl`, if the owner mirrors the MSD-LIVE
  files. Those need an MSD-LIVE sign-in, so the importer never downloads them. With a CSV the join
  uses ids and the snapshot cites the DOI.
- **Version check:** `GET https://data.msdlive.org/api/records/p147s-4h760/versions/latest`
  (the public records API, which redirects to the latest record), `metadata.version` →
  the snapshot's `upstream_version`. It is an API read once per run, so robots.txt is not consulted
  (its robots.txt answered 502 on 2026-10-07, which the crawler policy would read as "disallow
  everything"). A failure is a warning, not an error. A version other than v2026.02.09 prints a
  warning, because the mapping was checked against that version.

## 7. The PNNL join

`join(clusters, rows)` matches each row to one member, and so to one cluster:

- **By id**, when the row has one: `point` → `node/{id}`; `building` and `campus` → `way/{id}`,
  then `relation/{id}`. A row whose id OSM no longer has is unmatched (it is not matched
  spatially): those are the "rows OSM has dropped" of 07 §4.2 step 3.
- **Spatially** otherwise: the row point lies inside a member's bounding box expanded by 30 m, or
  within 50 m of a member's center. Ties break on the same `normalize_name(name)`, then the same
  `normalize_org(operator)`, then distance, then ref.

Effects on a matched cluster:

- `external_ids.pnnl_im3` gets each row's key: `{type}:{id}` when the row has an id, else
  `{type}@{lon:.6f},{lat:.6f}` (for example `building@-77.449520,39.026368`).
- `sources` gets s2: `https://doi.org/10.57931/3017294`, "Pacific Northwest National Laboratory
  (IM3)", title "IM3 Open Source Data Center Atlas v2026.02.09", `open_dataset`, `ODbL-1.0`,
  supports `/site` and `/buildings`.
- A `building` row's `sqft` goes to the matched building's (or point's) `buildings[].sqft`. When
  several building rows hit one member, PNNL has kept an older footprint next to the current one
  (Apple Data Center, Mesa, AZ: 1,338,261 and 1,263,277 sq ft on way 300974499, whose bounding box
  is 1,531,607 sq ft). The row with the member's name, else the nearest, is kept and the others
  are `possible_duplicate` items. A kept `sqft` more than 5% above the member's bounding-box area
  describes another footprint (PNNL's `sqft` is the polygon's area, which the box bounds; the
  projection difference was at most 1.3%), so it is a `conflict` item and is not applied.
- Building rows that hit a campus object count towards `site.building_sqft` only.
  `site.building_sqft` is the sum of the applied building values.
- A `campus` row gives `site.acreage` = sqft / 43,560, one decimal, only when it hit a campus
  object. A campus row whose polygon has left OpenStreetMap lands on a building (Microsoft Boydton,
  258.8 acres, on a 4.9-acre building), so it is a `conflict` item instead.
- Rows with the same key are counted once: PNNL repeats a site that straddles a county line, once
  per county.
- A row key whose every row's county differs from the record's point-in-polygon county is a
  `county_mismatch` review item (by `county_id` when the row has one, else by name through
  `CountyIndex.by_name`). A county-line site therefore raises no item when one of its two rows
  agrees.

Every unmatched row is an `unmatched` review item with the row in `data`. `pnnl_match_rate` =
matched rows / all rows; below 0.95 (the 07 §15 M1 acceptance threshold) the import prints a
warning.

## 8. Review items

| File | Kind | When |
|---|---|---|
| `osm.jsonl` | `out_of_scope` | the record's state is outside the 50 states and DC, or it is a telecom site (the record is still written, with `scope: "out_of_scope"`) |
| `osm.jsonl` | `unit_parse` | an `it_power` or `input:electricity` value that is not a power value |
| `osm.jsonl` | `missing_location` | an element without a position, or a cluster no Census county contains |
| `osm.jsonl` | `unknown_status` | no member tag the crosswalk knows (not seen; every query clause maps) |
| `osm.jsonl` | `invalid` | a cluster that does not fit the record schema (for example a point in Guam, whose longitude the schema does not accept) |
| `pnnl.jsonl` | `unmatched` | a PNNL row with no OSM-derived record |
| `pnnl.jsonl` | `county_mismatch` | PNNL's county is not the record's county (no row with that key agrees) |
| `pnnl.jsonl` | `possible_duplicate` | a second building row on one OSM building (not applied) |
| `pnnl.jsonl` | `conflict` | a campus row on a building, or a building row larger than its building's box (not applied) |

`atlas import` adds its own kinds (`conflict`, `held_human_reviewed`, `held_merged`, `invalid`,
`removed_upstream`) to `osm.jsonl`. Review files are rewritten on every run, sorted.

The receipt `data/imports/osm.json` has the metrics `objects`, `clusters`, `out_of_scope`,
`unit_parse`, `skipped`, and with PNNL `pnnl_rows`, `pnnl_matched`, `pnnl_matched_by_id` and
`pnnl_match_rate`.

## 9. Known limits

- **Bounding-box containment.** A campus is represented by its bounding box, so a diagonal or
  L-shaped campus covers neighbours. The operator guard stops cross-operator merges, but an
  unrelated object without an operator inside the box still joins. Footprints (`out geom` and a
  real point-in-polygon test) are the refinement.
- **Single-linkage chaining.** Same-operator buildings join transitively, so a long row of one
  operator's buildings becomes one campus (about 1.6 km at most on 2026-10-07). Unnamed buildings
  without an operator join only within 50 m, so a large unnamed site still splits into several
  records (40 unnamed halls near Abilene, in Taylor County, TX, give 8).
- **The spatial PNNL join.** Without ids a row matches whatever OSM object is within reach, so a
  PNNL row for a building OSM has deleted can match its neighbour. 15 of the 27 unmatched rows on
  2026-10-07 are PNNL `campus` rows, polygons the query no longer returns. Ids would make the
  cross-check exact (owner question: mirror the MSD-LIVE CSV).
- **OSM `it_power` semantics.** `it_power` and `input:electricity` are mapper-entered and rare (47
  and 71 US objects). Whether `input:electricity` is the facility's total draw or a utility
  connection is not defined by the tag; the importer stores it as `facility_mw` with confidence
  0.70 and keeps the raw text.
- **Statuses are assumed.** `telecom=data_center` is taken as operating (confidence 0.60). OSM says
  what a feature is, not when it changed, so `first_reported` dates are the snapshot date, and
  `operating_since` equals it unless a `start_date` exists. Lifecycle prefixes such as
  `proposed:building=*` on a `telecom=data_center` object are not read by the crosswalk: QTS
  Data Center - Hillsboro 3's two polygons (ways 1465196735 and 1465196736, tagged
  `proposed:building=industrial`) become two operating records, one of them with the proposed node
  11721960464 as a phase.
- **Names.** 187 records have no name and no operator and are called "Data center ({county},
  {ST})", for example "Data center (Taylor County, TX)"; 51 names are shared by 186 records that
  differ only by id. Operator tags are copied as written; a few name a person or a
  non-data-center business, which the seed review should catch.
- **Cross-source duplicates.** OSM and Epoch or AI GridWatch can describe the same site; entity
  resolution is M4.

## 10. Live run, 2026-10-07

`uv run atlas import osm` into a scratch repository at 22:42 UTC:

- **Overpass:** overpass-api.de reset the connection (twice, with the retry); maps.mail.ru answered
  HTTP 200 with 1,886 elements: 135 nodes, 1,740 ways and 11 relations, 826,409 bytes,
  `timestamp_osm_base` 2026-10-07T22:39:49Z. The same query took 26–27 s in the live test and the
  earlier probe. An attempt a few minutes before failed on all four endpoints (reset, 504, 500,
  500) and stopped cleanly with exit 1.
- **Dissolve:** 88 campus objects, 1,663 buildings, 135 points → 1,367 clusters (1,141 single
  objects, 226 with several members, at most 12).
- **Records:** 1,367 candidates, 0 invalid; `atlas validate` passes for all 1,367 (1,365 in scope,
  2 out of scope in Puerto Rico). Status: 1,314 operating, 52 under construction, 1 proposed; 12
  with phases. Precision: 1,248 footprint, 119 site. `it_mw` on 19 records and `facility_mw` on 24;
  31 dated by `start_date`; 2 planned `opening_date` events.
- **PNNL:** MSD-LIVE latest version v2026.02.09; 1,355 of 1,382 rows matched (**98.05%**, above
  the 95% threshold), all spatially; 971 records gained s2 and `pnnl_im3`, 34 an acreage.
- **Review:** 27 `unmatched` (15 campus, 7 building, 5 point rows) and 7 `county_mismatch` in
  `pnnl.jsonl` (clusters near a county line, for example Manassas city against Prince William
  County); 2 `out_of_scope` in `osm.jsonl`.

The seed records are not committed by this change; the bulk seed pull request runs the import after
integration (07 §6.7).
