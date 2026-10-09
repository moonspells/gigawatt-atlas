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
(
  way["industrial"="data_centre"](area.us);
  way["industrial"="data_center"](area.us);
  relation["industrial"="data_centre"](area.us);
  relation["industrial"="data_center"](area.us);
);
out geom;
```

Three changes from the query in research 02 §2.1:

- `out tags bb` instead of `out center tags`. The campus rule needs each way's extent. The center
  used everywhere is the bounding-box midpoint, which is what `center` returns.
- A `construction=data_center` clause. About 30 US objects are tagged `building=construction` +
  `construction=data_center`, often with `telecom=data_center` as well; the crosswalk maps them to
  `under_construction` (docs/status-crosswalk.md).
- A second statement for site polygons: the ways and relations tagged `industrial=data_centre`
  (or `data_center`), usually with `landuse=industrial` or `landuse=construction`, with their
  outlines (`out geom`: a way's nodes, a relation's member ways). The seed check of 2026-10-08
  found that the first statement never returns them, so the buildings of one site became several
  unnamed records (Microsoft Cheyenne, Microsoft Bison, Project Cardinal) and the 15 PNNL campus
  rows that lie in them were filed as dropped. Nine of those sites never had a telecom tag; six
  were retagged from `telecom=data_center` to `industrial=data_centre` during 2026. On 2026-10-09
  the statement returned 172 US elements (163 ways, 9 relations, about 230 kB with `out tags
  geom`); 16 of them are also in the first statement, and an element that comes twice is read
  once, with the outline of the copy that has one. The two statements are one request, so the
  query is as polite as before: one POST per run to the first endpoint that answers.
  Relations may come with or without member geometry; without it, the bounding box stands in.

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

Every element becomes an `OsmObject(ref, lat, lon, bounds, tags, kind, outline)`. `ref` is
`node/1`, `way/1` or `relation/1`; `lat`/`lon` is the node position or the bounding-box midpoint;
`outline` is the polygon from `out geom` (sites only), else empty. Tag values go through
`text.clean_text`: invisible characters and control characters (DEL, ESC, BEL and the rest that
are not whitespace) are removed and whitespace is collapsed, so a stray control character in a tag
does not make the record fail the text rule. `kind` is:

| kind | Rule | US count (2026-10-08) |
|---|---|---|
| `campus` | a way or relation with `telecom`, `construction:telecom`, `proposed:telecom` or `construction` = `data_center` and no `building` tag, or with `building=no`. A lifecycle building tag (`proposed:building=*`, `construction:building=*`, any value but `no`) counts as a `building` tag: a planned hall drawn as its own polygon is one building of a site. A construction site drawn as `landuse=construction` + `construction=data_center` is a campus, so the halls being built on it join it ("AWS IAD-500 and IAD-501", way 1319699416) | 98 |
| `site` | any other way or relation without a `building` tag that is tagged `industrial=data_centre` or `industrial=data_center` (second query statement) | 154 (2026-10-09) |
| `building` | any other way or relation | 1,654 |
| `point` | a node | 136 |

## 4. Dissolving objects into campuses

`dissolve(objects, radius_m=300, regions=...)` joins objects with union-find. Distances are gaps
between bounding boxes (a node is its point; 0 when the boxes touch or overlap), not distances
between centers, so two large halls that touch join although their centers are 320 m apart
(Google, Douglas County, GA, ways 844352473 and 844352474).

1. **Containment.** A building or point whose center lies inside a campus object's bounding box,
   expanded by 30 m, joins that campus. If it lies in several, it joins the smallest one. Any
   object but a site (a campus too) whose center lies inside a site polygon joins the smallest
   such site: inside its outline when the query returned one (a real point-in-polygon test, even-
   odd over the outline's segments, so a relation's member ways need not be assembled and inner
   rings are holes), else inside its box + 30 m.
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
   IL).
4. **The same specific name.** Two objects of any kind with the same `normalize_name(name)` join
   when their boxes are within 500 m and the operators of their two clusters agree (so an object
   without an operator never bridges two operators' clusters). The name must be specific: some
   word must not be a word of an operator named anywhere in the input, a number, a single letter
   or a generic word (data, center, building, hall, campus, site, phase, north, the, inc and the
   like). "QTS Data Center - Hillsboro 3", "Meta Sarpy Data Center" and "Compugen New Albany" are
   specific; "Google", "Amazon Web Services" and "Building 2" are not. On 2026-10-08 this joined
   Hillsboro 3's operator-less node to its two polygons (7 m), the two Meta Henrico clusters (387
   m) and the two Compugen New Albany buildings (275 m); two TulsaConnect nodes 558 m apart stay
   apart.
5. **The same address.** An unnamed object without operator information joins a non-campus
   object with an operator at the same `addr:housenumber` and `addr:street` within 50 m, unless the
   objects at that address name operators that disagree (Lakeside Technology Center, 350 East
   Cermak Road, has Digital Realty, Centersquare and Equinix). A named one may be another tenant,
   so it does not join this way. This joins the unnamed way at 8100 Boone Boulevard to Digital
   Realty's node (5 m) and the unnamed unit 2 at 2220 De La Cruz Boulevard to Digital Realty SC1.
6. **A node inside a building.** A node inside a building's bounding box joins it when the
   building and every node inside it have compatible operators (The Pittock Internet Exchange in
   the Pittock Block). CoreSite LA2 inside the USPO Terminal Annex (operator USPS) and Flexential
   inside the Equinix Infomart do not join; they are review items (section 8).

**Regions.** The importer passes each object's Census county (point-in-polygon at its center), and
no rule joins two objects in different counties, so a cluster never crosses a county or state
line: xAI's building in Southaven, DeSoto County, MS, joined a Shelby County, TN, record by rule 2
before (and its PNNL row was a `county_mismatch` item), as did Digital Realty IAD53 in Manassas
city with a Prince William County record. Because every union keeps a cluster in one county, the
record's county is its members' county.

Two campus objects join each other directly only by name (rule 4) or inside one site. Joins are
transitive, so a row of same-operator buildings 250 m apart forms one cluster; the widest
cluster on 2026-10-07 spans about 1.6 km (16 AWS objects in Ashburn). Each group of candidates is
swept in latitude order, so only pairs that overlap in latitude are measured.

On the 2026-10-07 snapshot (1,886 objects) the rules before the 2026-10-08 seed check gave 1,114
clusters. The earlier rules (center distances, an exact operator guard, no rule 3, lifecycle-
tagged polygons as campuses) gave 1,367. Box gaps alone give 1,280, the word-prefix guard 1,278
and rule 3 1,117 (its address clause adds 4 joins). Counting lifecycle-tagged polygons as
buildings (section 3) took that to 1,114. On the 2026-10-08 seed input (1,888 objects, without
site polygons, which that query did not fetch) rules 1 to 6 and the regions give 1,106 clusters
(1,115 before); with the 172 site polygons of 2026-10-09 added, 2,044 objects give 1,073.

The **representative** is the campus or site object (the largest, if a cluster has several), else
the way or relation with the largest bounding box, else the node with the lowest id. Members and
clusters are sorted by (type, numeric id), and the result does not depend on input order (tested
with shuffles).

## 5. Record mapping

One `campus` record per cluster, unless the cluster is held (below). `external_ids.osm` lists
every member ref, and those refs are the candidate's match values, so a later run updates the same
record (07 §2.1: records are never deleted; a cluster that disappears upstream becomes a
`removed_upstream` review item).

| Field | Value |
|---|---|
| `record_type` / `scope` | `campus`; `in_scope`, or `out_of_scope` outside the 50 states and DC, for a telecom site, or for an object the scope screen doubts (the record is kept, 07 §2.2; see below) |
| `canonical_name` | base = the representative's `name`, else the most common member name, else "{operator} data center", else "Data center". The operator is prefixed unless the base already starts with the operator, an `operator:short` of a member with that operator, or the operator's first word ("Lumen Ashburn" for Lumen Technologies stays as is; "CyrusOne NVA14" operated by PowerHouse becomes "PowerHouse CyrusOne NVA14"). Then " ({county}, {ST})" with the county's full Census name from the Gazetteer ("Loudoun County", "Manassas city", "Orleans Parish"). The county contains the point; `addr:city` is the postal city of the address, which need not (Flexential Atlanta - Norcross, `addr:city=Norcross`, lies in Peachtree Corners; "Sterling" and "Dulles" are postal names for points in Ashburn). A bare county name also reads as a city: Taylor County, TX is Abilene, while the city of Taylor, TX is 300 km away. |
| `aliases[]` | the other distinct member names (`osm_name`, source s1) |
| `parties.operator` / `owner` | the most common `operator` / `owner` tag (source s1) |
| `location.lat`, `lon` | the campus or site object's bounding-box center, else the mean of the member centers, unless that mean point lies in another county than the representative: then the representative's center; 7 decimals |
| `location.precision` | `footprint` if any member is a way or relation, else `site`; `site` also when a member's `note`, `fixme` or `description` says its position is approximate ("location is approximate", "Location approximated from Sentinel 2 low resolution imagery"), with an `unverified_upstream` item |
| `location.geocode_method` / `geometry_ref` | `osm` / `osm:{representative}` |
| `location.street`, `city`, `postcode` | "{addr:housenumber} {addr:street}", `addr:city`, `addr:postcode`: the representative's value, else the value every member that has one agrees on, else null. A value only some members share would put the representative's name at another building's address (QTS Manassas DC5 was published at DC1's 9400 Godwin Drive). `city` is the address's postal city. |
| `location.county_fips`, `county_name`, `state_abbr` | the representative's county, point-in-polygon against the Census 2025 counties (every member's county, section 4); `field_meta["/location/county_fips"]` = 0.90, `derived` |
| `buildings[]` | one per building or point member: `ref` "osm:{ref}", `name`, `phase_id` when the cluster has phases. `sqft` stays null: PNNL's sqft is the footprint's area, not a floor area (section 7) |
| `capacity` | see below |
| `status_history`, `phases` | see below; `field_meta["/status"]` = 0.60, `imported`, s1 |
| `evidence_level` / `purpose` | `reported` / `unknown`, or `telecom` for a telecom site (then s1 also supports `/purpose`) |
| `site.acreage` | from PNNL campus rows (section 7); `site.building_sqft` stays null |
| `sources[0]` (s1) | `https://www.openstreetmap.org/{representative}`, "OpenStreetMap contributors", title "OpenStreetMap {representative}", `open_dataset`, `ODbL-1.0`, supports `/canonical_name`, `/aliases`, `/parties`, `/location`, `/buildings`, `/capacity`, `/status_history/0` |
| `sources[1]` (s2) | PNNL, only when a PNNL row matched (section 7) |

`apply_rollup` runs on every candidate, and `atlas import` validates every record before writing it
(07 §3.6). Only these tags reach a record: `name`, `operator`, `operator:short`,
`operator:wikidata` (for joins), `owner`, the `addr:*` fields above, `it_power`,
`input:electricity`, `start_date` and `opening_date` (in notes and review items) and the status
tags. Contact tags such as `phone`, `email` and `website` are never copied.

### Held clusters

A cluster that OSM gives too little to publish makes no record; a review item holds it instead
(07 §2.3: no status or date is invented):

- **A site polygon with no data center object inside** (`unknown_status`): OSM says a data center
  site is there, not whether it is built. Microsoft Hoffman Estates was tagged `landuse=industrial`
  before construction began. 40 sites on the 2026-10-09 polygons, among them Meta Cheyenne, Riot
  Rockdale and Wave Broadband's telecom site in Tahoma Terra. The item lists the PNNL rows the
  site matched.
- **A cluster planned only in OSM, with no name and no operator** (`unverified_upstream`): a lead,
  like an unverified AI GridWatch row (the two Manchester Township, NJ, lots, `proposed=yes`,
  where no application was filed).
- **An unnamed building without an operator drawn over another building** (`possible_duplicate`):
  a single such way whose box shares at least half of the smaller box with another cluster's
  building is the same building mapped twice (way 567575425 on the Apple Data Center, way
  300974499, Mesa, AZ).

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

### Objects the scope screen doubts

The seed check of 2026-10-08 found crypto mines, an in-building computer room, a tape vault,
company offices and a telecom shed among the operating records, all tagged `telecom=data_center`.
`osm.scope_doubt` keeps such a cluster with `scope: "out_of_scope"` (never published) and an
`out_of_scope` item whose reason ends "held with scope out_of_scope until a reviewer decides"; the
purpose stays `unknown`. A reviewer who finds it is a data center sets it in scope. A cluster is
doubted when:

- **a campus or site member, or every member that has a name, operator or note**, is named or
  noted as a cryptocurrency mine (crypto, cryptocurrency, cryptomine, bitcoin, blockchain, BTC,
  mining, miners), is tagged `industrial=mine` or `cryptocurrency_mine` or `data_centre=crypto`,
  or is a power plant (`power=plant`, any `generator:source`): "Nautilus Cryptomine", "Greenidge
  Power Plant and Data Center", MARA, Riot. 07 §2.2 admits crypto sites only when they convert
  to, or are marketed as, data centers. A data center building on a site that also holds a mine
  keeps the cluster in scope (the Susquehanna site holds Nautilus Cryptomine and Amazon's
  buildings);
- **every member with text** is named or described as a room or a non-compute use: a computer,
  server, machine, equipment or network room, lab or closet, a classroom, "somewhere in this
  building", a tape vault, vital records or records storage ("SDSU Computer Room", "Vital
  Records", "Iron Mountain Records Storage");
- **every member** is a `building=shed`, `hut`, `cabin`, `kiosk`, `container`, `garage` or
  `carport`, or is tagged `utility=telecom` (a 5 m by 5 m telecom shed on a school campus).

The other rules apply only when no member's name, description or operator says data center,
colocation, hosting, cloud, servers, computer, computing or HPC, or names a DC number, a NAP, a
carrier hotel or an internet exchange:

- the members' boxes cover under 200 m² in all: a hut, or a suite drawn as its own polygon;
- every member is a node, none has an operator, and no name starts with an operator named
  anywhere in the input: a business's point of interest ("K-Motion Interactive", "Accelera Data
  Systems", "IT"); "TierPoint Milwaukee" stays, because TierPoint operates other objects;
- every named member is called only by a telephone company's brand ("CenturyLink", "Verizon",
  "AT&T", "Windstream"): as often an exchange as a data center;
- every member with a name or operator names a city, county, town, village, borough or township
  ("City of Searcy").

On the 2026-10-08 seed input the screen holds 70 clusters (82 out of scope with the 10 telecom
sites and the 2 Puerto Rico records), 67 with the site polygons of 2026-10-09 (some doubted
clusters join a site that names a data center).

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
operating), and the importer reads a proposed tag as `announced` (`osm.osm_status`): 07 §2.3's
`proposed` needs a formal application or filing, and no OSM tag is one. Members are grouped by the
resulting status.

- **One status (almost every cluster).** One event: seq 1, the status, event `other`, `as_of` =
  the snapshot date (the date of `timestamp_osm_base`, day precision), source s1, note "Tagged
  {tag} in OpenStreetMap; status per 07 §4.7, seen on the snapshot date, not the date it began".
  OSM says what a feature is on the day it was read, not when its status began, so the event sets
  the status and dates nothing: `rollup.derive_dates` skips `other` events, as for an AI GridWatch
  stage, and an OSM-only record has no `dates.first_reported` and no `operating_since`. Before the
  seed check, the event was `first_reported`, and 1,027 operating records published the snapshot
  date, 2026-10-08, as `operating_since`.
- **`start_date`** (YYYY, YYYY-MM or YYYY-MM-DD between 1990-01-01 and the snapshot date, on the
  representative, else the earliest member of an operating group) dates nothing either. In OSM it
  dates the feature, often the building or the start of construction: Apple Mesa's 2012 is the
  former factory's, QTS NAL1 DC1's 2024-04-01 the start of construction. The note says so ("...;
  start_date=2015 on way/556599694 is not read as the start of operation") and an
  `unverified_upstream` item gives a reviewer the date to confirm from a citable source (27 on
  2026-10-08). Earlier dates (old buildings converted to data centers, such as "1938") are
  ignored.
- **Several statuses (18 clusters on 2026-10-08).** Each status group becomes a phase,
  `osm-operating`, `osm-under_construction` or `osm-announced`, named after its members ("Under
  construction in OpenStreetMap: NTT VA8"), with one event per phase and `buildings[].phase_id`
  set. The rollup (07 §2.3) then gives the most advanced active status, so an operating campus with
  a building under construction stays `operating` and is `expanding`, rather than the whole campus
  showing as under construction.
- **`opening_date`** on a group that is not operating adds a planned `energized` event, which never
  changes the status. A date whose whole period has passed by the snapshot (opening_date=2025 on
  a site still tagged under construction on 2026-10-08) is not used; it is a `conflict` item.
- On later runs, an observation (`other`, or a `first_reported` event an earlier version wrote)
  keeps the earliest `as_of` already stored for the same status and source, and the note names no
  date, so weekly runs change no record. The phase is not compared: when a building under
  construction appears next to an operating campus, or later goes, the operating event moves
  between no phase and `osm-operating` but its status has not changed. A status change replaces
  the event; recording transitions as new events is the M2 diff (07 §4.2 step 5).

## 6. PNNL inputs

PNNL v2026.02.09 (DOI 10.57931/3017294, ODbL 1.0) is derived from OSM, with county and state from
2024 Census boundaries and `sqft` = the footprint polygon's area (not a floor area; section 7).

- **The public web-map file** (default):
  `https://immm-sfa.github.io/datacenter-atlas/im3_datacenter_centroids.geojson`, 1,382 Point
  features (1,239 building, 94 point, 49 campus) whose properties are only `state_abb`, `county`,
  `operator`, `name`, `sqft` and `type`. It has **no OSM id and no FIPS code**, so the join is
  spatial. Fetched with the crawler policy (robots.txt, 2 s per host), kept at
  `.cache/atlas/raw/pnnl/{sha256}.geojson`.

  The file does not say which dataset version it is, so `WEBMAP_VERSIONS` records the files whose
  version is established, by SHA-256. The one served on 2026-10-07 and 2026-10-08
  (`2e7bd7e6…45df`, 373,790 bytes) is the v2026.02.09 export: the repository behind the web map,
  `immm-sfa/datacenter-atlas`, committed it on 2026-02-12 in the commit that also set the map's
  own citation to v2026.02.09 (DOI 10.57931/3017294) and its legend to "Last Updated Feb 09,
  2026". The repository README still links the v1 record (MSD-LIVE 65g71-a4731, DOI
  10.57931/2550666) for the layer, a link from before that update, and the file's Last-Modified
  (2026-03-31) is a later deploy that changed only the projected layers. The snapshot's
  `upstream_version` is the file's established version, or null with a warning for any other
  file, which records then cite as the file itself (section 7). The repository is BSD 2-Clause,
  © 2025 Battelle Memorial Institute; the notice is in `ATTRIBUTION.md` and the PNNL fixture
  README. MSD-LIVE gives the dataset as ODbL 1.0, and records keep `ODbL-1.0`.
- **A CSV in the MSD-LIVE layout** (`id, state, state_abb, state_id, county, county_id, ref,
  operator, name, sqft, lat, lon, type`), passed with `--pnnl`, if the owner mirrors the MSD-LIVE
  v2026.02.09 files. Those need an MSD-LIVE sign-in, so the importer never downloads them. With a
  CSV the join uses ids, and the snapshot cites the v2026.02.09 DOI with `upstream_version`
  v2026.02.09.
- **Version check:** `GET https://data.msdlive.org/api/records/p147s-4h760/versions/latest`
  (the public records API, which redirects to the latest record). It is an API read once per run,
  so robots.txt is not consulted (its robots.txt answered 502 on 2026-10-07, which the crawler
  policy would read as "disallow everything"). A failure is a warning, not an error. A
  `metadata.version` other than v2026.02.09 prints a warning, because the mapping was checked
  against that version. It is not the snapshot's `upstream_version`: a newer MSD-LIVE version
  says nothing about which version the web-map file is, and the receipt and the records must name
  the same one.

## 7. The PNNL join

`join(clusters, rows)` matches each row to one member, and so to one cluster:

- **By id**, when the row has one: `point` → `node/{id}`; `building` and `campus` → `way/{id}`,
  then `relation/{id}`. A row whose id the import did not read is unmatched (it is not matched
  spatially): those are the "rows OSM has dropped" of 07 §4.2 step 3, or rows of features that are
  retagged (for example to `telecom=exchange`).
- **Spatially** otherwise: the row point lies inside a member's bounding box expanded by 30 m, or
  within 50 m of a member's center. A member whose own box (no margin) holds the point comes
  first, then one of the row's kind (a campus row: a campus or site; a building or point row: a
  building or point); then the same `normalize_name(name)`, the same `normalize_org(operator)`,
  distance and ref break ties. Position comes before the name because PNNL keeps old names: the
  row "QTS Manassas DC1" lies 6 m from the center of the polygon OSM now calls DC2, still within
  DC1's box + 30 m, and the name used to send it to DC1 (with a misleading `possible_duplicate`).
- **By name**, when the spatial test fails: the nearest member within 250 m with the row's
  `normalize_name(name)` and an operator that does not disagree. Fiberhub LAS1's node moved 119 m
  after PNNL took its position (`pnnl_matched_by_name`, 1 on 2026-10-08).

Effects on a matched cluster:

- `external_ids.pnnl_im3` gets each row's key: `{type}:{id}` when the row has an id, else
  `{type}@{lon:.6f},{lat:.6f}` (for example `building@-77.449520,39.026368`).
- `sources` gets s2 for the file actually read, "Pacific Northwest National Laboratory (IM3)",
  `open_dataset`, `ODbL-1.0`, supports `/site`. When the file's version is established it cites
  that version: `https://doi.org/10.57931/3017294`, title "IM3 Open Source Data Center Atlas
  v2026.02.09, web map file im3_datacenter_centroids.geojson" (just "IM3 Open Source Data Center
  Atlas v2026.02.09" for the CSV). Otherwise it cites the web-map file URL, title "IM3 Open Source
  Data Center Atlas, web map file im3_datacenter_centroids.geojson".
- PNNL's `sqft` is the area of the OSM footprint polygon, not a floor area, so no building row
  sets `buildings[].sqft` or `site.building_sqft`. A multi-storey building's floor area is larger
  (Digital Realty IAD39: a 549,223 sq ft footprint, "Total building size: 1,013,500 ft²"), and
  before the seed check the published `building_sqft` was also a partial sum where some
  buildings had no row (Vantage VA13: 243,191 for a campus of more than a million square feet).
  The rows still check the match: when several building rows hit one member, PNNL has kept an
  older footprint next to the current one (Apple Data Center, Mesa, AZ: 1,338,261 and 1,263,277
  sq ft on way 300974499, whose bounding box is 1,531,607 sq ft). The row with the member's name,
  else the nearest, is the match and the others are `possible_duplicate` items. A row's `sqft`
  more than 5% above the member's bounding-box area describes another footprint (the box bounds
  the polygon; the projection difference was at most 1.3%), so it is a `conflict` item.
- A `campus` row gives `site.acreage` = sqft / 43,560, one decimal, only when it hit a campus or
  site object, and only when every campus or site member that no larger one covers has such a
  row; a nested campus does not add to its site. A campus row whose polygon the import did not
  read lands on a building (Microsoft Boydton, 258.8 acres, on a 4.9-acre building before the
  site polygons were fetched), so it is a `conflict` item instead.
- Rows with the same key are counted once: PNNL repeats a site that straddles a county line, once
  per county.
- A row key whose every row's county differs from the record's point-in-polygon county is a
  `county_mismatch` review item (by `county_id` when the row has one, else by name through
  `CountyIndex.by_name`). A county-line site therefore raises no item when one of its two rows
  agrees.

Every unmatched row is an `unmatched` review item with the row in `data`, and the nearest element
the import read in `data.nearest_osm` and `data.nearest_m`. The reason says what was tested: "no
OSM data center element this import read takes the PNNL building row: no bounding box (+30 m)
holds its point, no center is within 50 m and no element of its name is within 250 m; the nearest
is way/…, 111 m away". OSM may still have the feature under other tags (two of the 2026-10-08
rows are inside buildings retagged `telecom=exchange` and `office=telecommunication`). Rows on a
held cluster count as matched and are listed in its item (`pnnl_on_held`). `pnnl_match_rate` =
matched rows / all rows; below 0.95 (the 07 §15 M1 acceptance threshold) the import prints a
warning.

## 8. Review items

| File | Kind | When |
|---|---|---|
| `osm.jsonl` | `out_of_scope` | the record's state is outside the 50 states and DC, it is a telecom site, or the scope screen doubts it is a data center (the record is still written, with `scope: "out_of_scope"`; section 5) |
| `osm.jsonl` | `unit_parse` | an `it_power` or `input:electricity` value that is not a power value |
| `osm.jsonl` | `missing_location` | an element without a position, or a cluster no Census county contains |
| `osm.jsonl` | `unknown_status` | a held cluster with no status: a site polygon with no data center object inside, or no member tag the crosswalk knows |
| `osm.jsonl` | `unverified_upstream` | a `start_date` not used as a date; a position OSM calls approximate; a held cluster planned only in OSM, with no name and no operator |
| `osm.jsonl` | `conflict` | an `opening_date` that has passed while OSM still tags the site as not operating (not used) |
| `osm.jsonl` | `possible_duplicate` | records with the same canonical name chained within 1 km (one item per group, 33 on 2026-10-08); an object inside a campus or site that has no building of its own, or lists its house number, or a node inside a building, whose operator disagrees (8); a held unnamed building drawn over another (1) |
| `osm.jsonl` | `invalid` | a cluster that does not fit the record schema (for example a point in Guam, whose longitude the schema does not accept) |
| `pnnl.jsonl` | `unmatched` | a PNNL row that no data center element the import read takes |
| `pnnl.jsonl` | `county_mismatch` | PNNL's county is not the record's county (no row with that key agrees) |
| `pnnl.jsonl` | `possible_duplicate` | a second building row on one OSM building |
| `pnnl.jsonl` | `conflict` | a campus row on a building, or a building row larger than its building's box |

`atlas import` adds its own kinds (`conflict`, `held_human_reviewed`, `held_merged`, `invalid`,
`removed_upstream`) to `osm.jsonl`. Review files are rewritten on every run, sorted.

The receipt `data/imports/osm.json` has the metrics `objects`, `clusters`, `held` (clusters held
for review, with no record), `out_of_scope`, `unit_parse`, `skipped`, and with PNNL `pnnl_rows`,
`pnnl_matched`, `pnnl_matched_by_id`, `pnnl_matched_by_name`, `pnnl_on_held` and
`pnnl_match_rate`.

## 9. Known limits

- **Bounding-box containment.** A campus is represented by its bounding box, so a diagonal or
  L-shaped campus covers neighbours. The operator guard stops cross-operator merges, but an
  unrelated object without an operator inside the box still joins. Site polygons come with their
  outlines and use a real point-in-polygon test; `telecom=data_center` campus polygons do not yet
  (`out geom` for the first statement is the refinement).
- **Single-linkage chaining.** Same-operator buildings join transitively, so a long row of one
  operator's buildings becomes one campus (about 1.6 km at most on 2026-10-07). Unnamed buildings
  without an operator join only within 50 m, so a large unnamed site that OSM draws no site
  polygon for still splits into several records (the halls of the Lancium Clean Campus near
  Abilene, in Taylor County, TX, give 8); each such group is a `possible_duplicate` item.
- **The spatial PNNL join.** Without ids a row matches whatever OSM object is within reach, so a
  PNNL row for a building OSM has deleted can match its neighbour. Ids would make the cross-check
  exact (owner question: mirror the MSD-LIVE CSV). With the site polygons, 10 of 1,382 rows are
  unmatched (27 before).
- **OSM `it_power` semantics.** `it_power` and `input:electricity` are mapper-entered and rare (47
  and 71 US objects). Whether `input:electricity` is the facility's total draw or a utility
  connection is not defined by the tag; the importer stores it as `facility_mw` with confidence
  0.70 and keeps the raw text.
- **Statuses are assumed, and OSM dates nothing.** `telecom=data_center` with no lifecycle tag is
  taken as operating (confidence 0.60); `construction:*`, `building=construction` and
  `landuse=construction` give under construction and `proposed:*` announced (section 5). OSM says
  what a feature is, not when it changed, so an OSM-only record has no dates; `start_date` is a
  review item, not a date. Dates come from Epoch, AI GridWatch and, from M2, primary records.
- **Planned buildings.** A polygon tagged `proposed:building=*` or `construction:building=*` is a
  building, not a campus, so Rowan Green's four Percheron DC halls (ways 1501824326 to 1501824329)
  and QTS Data Center - Hillsboro 3's two polygons (ways 1465196735 and 1465196736) each dissolve
  under rule 2 (same operator, within 300 m) into one announced record; Hillsboro 3's operator-less
  node joins its polygons by name (rule 4). PNNL campus rows that land on such a polygon become
  `conflict` items (two, Hillsboro 3).
- **The scope screen is a set of word and tag rules.** It holds some real data centers for review
  (a colocation suite under 200 m², "Southwest Cyberport" as a bare point) and cannot see a node
  inside a library or office building that OSM does not tag as a data center. Only a reviewer
  decides; nothing it holds is published or dropped.
- **Names.** Records with no name and no operator are called "Data center ({county}, {ST})", for
  example "Data center (Taylor County, TX)"; 50 names are shared by more than one record on the
  2026-10-08 seed input (28 with the site polygons). Operator tags are copied as written; a few
  name a person or a non-data-center business, which the seed review should catch. The place is
  the county, not the postal city: a containing place (Census places) would need place polygons.
- **Doubly mapped buildings.** An unnamed way drawn on another building's footprint is held
  (section 5); a named one, or one with its own operator, stays a record, and a node inside
  another operator's building is a `possible_duplicate` item: CoreSite LA2 is a tenant of the USPO
  Terminal Annex, but Flexential in the Equinix Infomart is one too, so no rule picks a record.
- **Cross-source duplicates.** OSM and Epoch or AI GridWatch can describe the same site; entity
  resolution is M4 (owner decision of 2026-10-08).

## 10. Live run, 2026-10-07

`uv run atlas import osm` into a scratch repository at 22:42 UTC:

- **Overpass:** overpass-api.de reset the connection (twice, with the retry); maps.mail.ru answered
  HTTP 200 with 1,886 elements: 135 nodes, 1,740 ways and 11 relations, 826,409 bytes,
  `timestamp_osm_base` 2026-10-07T22:39:49Z. The same query took 26–27 s in the live test and the
  earlier probe. An attempt a few minutes before failed on all four endpoints (reset, 504, 500,
  500) and stopped cleanly with exit 1.
- **Dissolve:** 81 campus objects, 1,670 buildings, 135 points → 1,114 clusters (830 single
  objects, 284 with several members, at most 32). The rules before the 2026-10-08 review fixes
  gave 1,367 (section 4).
- **Records:** 1,114 candidates, 0 invalid; `atlas validate` passes for all 1,114 (1,102 in scope;
  12 out of scope: 2 in Puerto Rico and 10 telecom sites). Status: 1,054 operating, 53 under
  construction, 7 proposed (lifecycle tags, docs/status-crosswalk.md); 17 with phases. Precision:
  998 footprint, 116 site. `it_mw` on 13 records and `facility_mw` on 19; 27 dated by
  `start_date`; 2 planned `opening_date` events.
- **PNNL:** MSD-LIVE latest version v2026.02.09, and the web-map file read is the established
  v2026.02.09 export (`upstream_version` v2026.02.09); 1,355 of 1,382 rows matched (**98.05%**,
  above the 95% threshold), all spatially; 862 records gained s2 and `pnnl_im3`, 28 an acreage.
- **Review:** in `pnnl.jsonl`, 27 `unmatched` (15 campus, 7 building, 5 point rows), 11 `conflict`
  (6 campus rows on buildings, 5 building rows larger than their building's box), 4
  `county_mismatch` and 4 `possible_duplicate`; in `osm.jsonl`, 12 `out_of_scope`. The
  `county_mismatch` rows were right: each row's point lies in the county PNNL names (a "Manassas
  city" row on Digital Realty IAD53, which the importer's own county index puts in Manassas city;
  a Licking County row; xAI's DeSoto County, MS, building; a Medina County, TX, row), and the
  record was in the next county because rule 2 joined buildings across the line. The regions
  (section 4) now keep clusters in one county, and the seed re-run has none (section 11).

These counts were measured on the saved 22:39:49Z response with the code after the 2026-10-08
review fixes and their integration (the 1:500,000 county file for county FIPS, lifecycle-tagged
polygons as buildings); the first run (22:42 UTC) gave the figures before them, quoted in
section 4.

The seed records are not committed by this change; the bulk seed pull request runs the import after
integration (07 §6.7).

## 11. Re-run of the 2026-10-08 seed input

The seed check of 2026-10-08 (1,394 records, 129 sampled) found an error in 89 sampled records,
70 of them the snapshot date as `operating_since`. The importer was re-run offline on the saved
seed input (`--input overpass-us-20261008T204734Z.json --pnnl im3_datacenter_centroids.geojson
--now 2026-10-08T20:47:41.963950Z --offline`) into a scratch store:

| | Seed (before) | Re-run | Re-run + site polygons |
|---|---|---|---|
| Objects / clusters | 1,888 / 1,115 | 1,888 / 1,106 | 2,044 / 1,073 |
| Records (in scope) | 1,115 (1,103) | 1,103 (1,021) | 1,031 (952) |
| Held, no record | 0 | 3 | 42 |
| Operating / under construction / proposed or announced | 1,054 / 53 / 8 proposed | 1,048 / 50 / 5 announced | 993 / 33 / 5 announced |
| `dates.operating_since` / `first_reported` | 1,054 / 1,115 | 0 / 0 | 0 / 0 |
| `site.building_sqft` / `buildings[].sqft` / `site.acreage` | 745 / 1,216 / 28 | 0 / 0 / 28 | 0 / 0 / 34 |
| Records with an operator | 649 | 651 | 663 |
| Review items (osm / pnnl) | 12 / 46 | 159 / 41 | 176 / 21 |
| PNNL matched | 1,355 (98.05%) | 1,356 (98.12%) | 1,372 (99.28%) |

The third column adds the 172 site polygons of a separate query run on 2026-10-09 (the second
statement of section 1, `out tags geom`) to the saved response, because the seed query did not
fetch them; the live import with the new query gives those figures up to a day of OSM edits.
