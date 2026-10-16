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
| `--dissolve-m M` | The same-operator join distance between bounding boxes, default 300 m (also the same-address distance of rule 3 and the sibling-name distance of rule 4). |
| `--overrides PATH` | The reviewers' decisions on OSM elements (default `config/overrides/osm.json`, section 9). |

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
)->.dc;
.dc out tags bb;
(
  way.dc[!"building"];
  way.dc["building"="no"];
  relation.dc[!"building"];
  relation.dc["building"="no"];
  way["industrial"="data_centre"](area.us);
  way["industrial"="data_center"](area.us);
  relation["industrial"="data_centre"](area.us);
  relation["industrial"="data_center"](area.us);
);
out geom;
(node(w.dc:1); node.dc;)->.pts;
.pts is_in->.inside;
(
  way(pivot.inside)["landuse"~"^(construction|industrial|commercial)$"];
  relation(pivot.inside)["landuse"~"^(construction|industrial|commercial)$"];
);
out geom;
```

Changes from the query in research 02 §2.1:

- `out tags bb` instead of `out center tags`. The campus rule needs each way's extent. The center
  used everywhere is the bounding-box midpoint, which is what `center` returns.
- A `construction=data_center` clause. About 30 US objects are tagged `building=construction` +
  `construction=data_center`, often with `telecom=data_center` as well; the crosswalk maps them to
  `under_construction` (docs/status-crosswalk.md).
- A second statement for outlines (`out geom`: a way's nodes, a relation's member ways): the data
  center objects that are areas (no `building` tag, or `building=no`: the campus polygons) and
  the site polygons, the ways and relations tagged `industrial=data_centre` (or `data_center`),
  usually with `landuse=industrial` or `landuse=construction`. The seed check of 2026-10-08
  found that the first statement never returns the sites, so the buildings of one site became
  several unnamed records (Microsoft Cheyenne, Microsoft Bison, Project Cardinal) and the 15 PNNL
  campus rows that lie in them were filed as dropped; 172 US elements on 2026-10-09. The second
  seed check (2026-10-09) found campus polygons judged by their bounding box: STACK's NVA04
  polygon and Microsoft's Quail Ridge Lane polygon hold other operators' buildings, and the
  contractor-tagged polygon at 10051 Brickyard Way covers AWS buildings in its box but not in its
  outline. With the outlines, containment tests the polygon.
- A third statement for container polygons: the `landuse=construction`, `industrial` or
  `commercial` areas that hold the first node of a data center way, or a data center node
  (`is_in` on those nodes, then the ways and relations that make the areas). One node per way
  keeps the lookup cheap: on a Loudoun County probe of 2026-10-09 it found the same containers
  as every node of every way, but one, in a third of the time. OSM often draws a campus's site as
  such a polygon without any data center tag: the Lancium Clean Campus near Abilene (40 halls,
  eight records on 2026-10-09), Stream Data Centers San Antonio III, Compass's Red Oak site
  (`landuse=industrial`, `industrial=communication`), Google's 321-acre Council Bluffs site. They
  are read as `site` objects that are containers (section 3). Overpass makes areas only of the
  polygons its area rules cover (a Loudoun County probe on 2026-10-09 found named and unnamed
  ones), so a container it has no area for is not found; the dissolve rules of section 4 still
  join most such halls.

An element that comes more than once (a campus polygon in the first and second statements) is
read once, with the outline of the copy that has one. The statements are one request, so the
query is as polite as before: one POST per run to the first endpoint that answers. Relations may
come with or without member geometry; without it, the bounding box stands in.

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
`ODbL-1.0`. With `--input`, the URL is the first configured endpoint and `retrieved_at` is
`--input-retrieved-at` (when the saved file was fetched), else `--now`; a local `--pnnl` file's
too.

## 3. Objects

Every element becomes an `OsmObject(ref, lat, lon, bounds, tags, kind, outline)`. `ref` is
`node/1`, `way/1` or `relation/1`; `lat`/`lon` is the node position or the bounding-box midpoint;
`outline` is the polygon from `out geom` (areas only), else empty. Tag values go through
`text.clean_text`: invisible characters and control characters (DEL, ESC, BEL and the rest that
are not whitespace) are removed and whitespace is collapsed, so a stray control character in a tag
does not make the record fail the text rule. `kind` is:

| kind | Rule | US count (2026-10-08) |
|---|---|---|
| `campus` | a way or relation with `telecom`, `construction:telecom`, `proposed:telecom` or `construction` = `data_center` and no `building` tag, or with `building=no`. A lifecycle building tag (`proposed:building=*`, `construction:building=*`, any value but `no`) counts as a `building` tag: a planned hall drawn as its own polygon is one building of a site. A construction site drawn as `landuse=construction` + `construction=data_center` is a campus, so the halls being built on it join it ("AWS IAD-500 and IAD-501", way 1319699416) | 98 |
| `site` | any other way or relation without a `building` tag that is tagged `industrial=data_centre` or `industrial=data_center` (second query statement), or a container: `landuse=construction`, `industrial` or `commercial` with no data center tag (third statement). `keep_containers` keeps a container only when it holds the centers of two data center objects and, unless it is a construction site, its name says data center or starts with an operator of the input; a container leads a record only when the cluster has no other area | 154 (2026-10-09) |
| `building` | any other way or relation | 1,654 |
| `point` | a node | 136 |

Two tags make an element's `operator` tag not the data center's, so the element has no operator
for the dissolve and the record (`OsmObject.operator`):

- **another primary use** (`OsmObject.other_use`): `amenity`, `shop`, `tourism`, `healthcare` or
  `healthcare:speciality` on the element. The USPO Terminal Annex is `amenity=post_office`,
  operator United States Postal Service, and holds CoreSite LA2 (a node); the record was
  "United States Postal Service USPO Terminal Annex". Now the node joins the building and the
  record is "CoreSite - LA2", with the building's name as an alias. `office` and `government` do
  not count: H5 Data Centers runs the former Seattle Times building (`office=newspaper`), and
  state IT buildings carry `government=it`.
- **a general contractor** (`OsmObject.builder`, `dissolve.GENERAL_CONTRACTORS`: HITT, Clark,
  DPR, Holder, Turner, Whiting-Turner, Mortenson, JE Dunn, Gray, Gilbane, Skanska, Hensel Phelps,
  McCarthy, Fortis, Structure Tone, Kiewit). The construction polygon at 10051 Brickyard Way is
  tagged `operator=HITT`, the builder of Digital Realty IAD51 at that address; it was published
  as "HITT data center". An `unverified_upstream` item names the tag.

## 4. Dissolving objects into campuses

`dissolve(objects, radius_m=300, regions=...)` joins objects with union-find. Distances are gaps
between bounding boxes (a node is its point; 0 when the boxes touch or overlap), not distances
between centers, so two large halls that touch join although their centers are 320 m apart
(Google, Douglas County, GA, ways 844352473 and 844352474).

1. **Containment.** A building or point whose center lies inside a campus object (its outline,
   or without one its bounding box expanded by 30 m) joins that campus. If it lies in several, it
   joins the smallest one. Any object but a site (a campus too) whose center lies inside a site
   polygon joins the smallest such site: inside its outline when the query returned one (a real
   point-in-polygon test, even-odd over the outline's segments, so a relation's member ways need
   not be assembled and inner rings are holes), else inside its box + 30 m.
   *Operator guard:* the object does not join when the two carry comparable operator information
   that disagrees: different `operator:wikidata` ids when both have one, otherwise normalized
   operators (`text.normalize_org`) of which neither starts the other, word by word, also once
   generic words are set aside ("Bank of America" and "Bank America"). "Amazon" and "Amazon Web
   Services us-east-2 datacenter" both agree with "Amazon Web Services", so AWS buildings join AWS
   campus polygons tagged that way (Hilliard, OH, way 460067225, and way 697021949). Two
   operators of six letters or more that differ by one letter agree (`dissolve.one_edit`): the
   CyrusOne campus in Freestone County is tagged `operator=CyrysOne`, and its halls (CyrusOne)
   were a record of their own; an `unverified_upstream` item names both spellings. A bounding box
   overstates a polygon, and in Ashburn the box of the fenced "Amazon Web Services Datacenter
   Complex" (way 460053028) covers Equinix DC17 and DC18; without the guard the whole Equinix row
   of buildings would chain into the AWS campus.
   *An area without an operator over two operators:* when the objects whose centers an area
   without an operator covers name operators that disagree, the area holds only its anchors and
   the objects compatible with them (`dissolve.mixed_anchors`): the objects at its own street
   address, else those of the operator most of them name, else none. It also holds an object
   whose `owner` tag names the area's owner or operator, or the anchors' operator
   (`dissolve.owner_agrees`): a landlord's building leased to a tenant. The HITT polygon's box
   covers Digital Realty IAD51 (at its address) and three AWS buildings, which stay apart; the
   Digital Loudoun Plaza polygons (`owner=Digital Realty`, no operator) cover Digital Realty's
   halls and Amazon IAD-6 and IAD-13 (operator AWS, `owner=Digital Realty`), which all chained into
   one record before fix round 5 and were two records after it (the plaza's and "Amazon IAD-6 and
   IAD-13"). Since fix round 6 they are one record, operated by Digital Realty (the most common
   operator), with the AWS buildings and their power tags in `buildings[]` and `mw_as_stated`.
   An object that shares an area's street address (any number of a ";" list on the same street)
   but whose operator disagrees is an `address_conflict` note (section 5, held clusters).
2. **Operator distance.** Two non-campus objects whose boxes are within `radius_m` of each other
   join when they have the same operator, compared without generic words
   (`dissolve.operator_group`: "PowerHouse" and "PowerHouse Data Centers" are one), or the same
   `operator:wikidata`. An object without operator information whose name starts with the
   distinctive words of an operator (or `operator:short`) named anywhere in the input counts as
   that operator's (`dissolve.implied_operators`): "Google Leesburg Building 3" (180 m from
   Google's buildings), "CyrusOne San Antonio IV" (its box overlaps San Antonio III's) and the
   eleven "Compass Data Center" halls at Red Oak, 54 m apart (Compass Datacenters is named
   elsewhere). A name that two operators fit equally, or an operator whose words are all short or
   generic, implies none. The implied operator steers the joins only; no record publishes it.
   *Areas of one operator that adjoin* join the same way (`dissolve.adjoining_gap_m`): two campus
   or site polygons (not containers) of one operator whose outlines are within 150 m
   (`ADJOIN_M`), or, when either has no outline, whose boxes overlap (a box overstates its
   polygon). Meta New Albany Data Center and its expansion site, "Meta LCO 3 Project" (its outline
   touches the campus's), were two records, and so were Google's two Lenoir outers, 108 m apart:
   one multipolygon until 2026-07-10, which PNNL's one 78.2-acre campus row still spans. Two that
   each list a street address, and different ones, are two projects of one operator and stay apart
   with a `same_operator_nearby` note (Microsoft's Project Alluvion, 550 Southeast White Crane
   Road, and Project Ginger East, 1475 Southeast Maffitt Lake Road, 126 m apart in West Des
   Moines; Iron Mountain's AZP-1 and AZP-2 polygons in Phoenix).
3. **Neighbours without an operator.** Two non-campus objects that both have no operator
   information join when their boxes are within 50 m and they have the same `normalize_name(name)`
   or neither has a name, or when they have the same `addr:housenumber` and `addr:street` and
   their boxes are within `radius_m`. Most unnamed `telecom=data_center` buildings carry no
   operator, and without this rule each one became its own record: 32 records "Data center
   (Venango, PA)" within 213 m, and 11 "Blockfusion Niagra Falls" at 5380 Frontier Avenue. The gap
   is 50 m because at 100 m unnamed buildings chain across 1.5 km (8 buildings in Kendall County,
   IL, which turned out to be the halls of Project Cardinal, inside one site polygon). Two unnamed
   halls of at least 5,000 m² each (`LARGE_HALL_M2`, by box) join within 100 m
   (`LARGE_HALL_GAP_M`): Stream San Antonio III's three halls (74 and 79 m apart), QTS Atlanta DC3
   and DC4 (94 m) and a Prince William County construction site's two buildings (99.7 m) were
   each a record. The Abilene halls 104 m apart stay apart (their site is a container, section 1).
4. **The same specific name.** Two objects of any kind with the same `normalize_name(name)` join
   when their boxes are within 500 m and the operators of their two clusters agree (so an object
   without an operator never bridges two operators' clusters). The name must be specific: some
   word must not be a word of an operator named anywhere in the input, a number, a single letter
   or a generic word (data, center, building, hall, campus, site, phase, north, the, inc and the
   like). "QTS Data Center - Hillsboro 3", "Meta Sarpy Data Center" and "Compugen New Albany" are
   specific; "Google", "Amazon Web Services" and "Building 2" are not. On 2026-10-08 this joined
   Hillsboro 3's operator-less node to its two polygons (7 m), the two Meta Henrico clusters (387
   m) and the two Compugen New Albany buildings (275 m); two TulsaConnect nodes 558 m apart stay
   apart. A name that is not specific joins the same way when the two objects also have the same
   operator: QTS Phoenix II's two buildings named "QTS" (345 m apart), QTS's Fayetteville pair
   (314 m) and Google Council Bluffs' four expansion halls (478 m from the other fourteen) were
   separate records. AWS's Morrow County groups, 512 m and more apart, stay apart.
   *Sibling names:* names that differ only in their numbers and a final direction or Roman
   numeral (`dissolve.sibling_stem`: "KOMO Plaza East" and "West", "EAT12" to "EAT14",
   "PowerHouse Pacific Building 1" to "3", "Microsoft Leesburg Building 3" to "5") join the same
   way within `radius_m`, when the stem says something once the operator it starts with is set
   aside ("Building 2" and "Equinix DC10" do not) and the name is not a street address.
5. **The same address.** An unnamed object without operator information joins a non-campus
   object with an operator at the same `addr:housenumber` and `addr:street` within 50 m, unless the
   objects at that address name operators that disagree (Lakeside Technology Center, 350 East
   Cermak Road, has Digital Realty, Centersquare and Equinix). A named one may be another tenant,
   so it does not join this way. This joins the unnamed way at 8100 Boone Boulevard to Digital
   Realty's node (5 m) and the unnamed unit 2 at 2220 De La Cruz Boulevard to Digital Realty SC1.
6. **A node inside a building.** A node inside a building's bounding box joins it when the
   building and every node inside it have compatible operators (The Pittock Internet Exchange in
   the Pittock Block, and CoreSite LA2 in the USPO Terminal Annex, whose post office operator does
   not count). Flexential inside the Equinix Infomart does not join; it is a review item
   (section 8).
7. **An unnamed building in one operator's row.** A building without a name or operator
   information, in a cluster that has no operator, joins the nearest building with an operator
   within 50 m when every object with operator information within `radius_m` names that one
   operator. Way 597876723 at 904 Quality Way, 18 m from Digital Realty DFW18 among the six
   Digital Dallas buildings, was a record "Data center (Richardson, TX)". A node marks where a
   tenant is, not a row of buildings, so it neither joins nor is joined this way, and a way drawn
   over the building (half of the smaller box or more) is the same building mapped twice, which
   is held instead (section 5). A building whose cluster holds a campus or site polygon is passed
   over when none of those polygons covers the unnamed building's center: a building outside a
   campus's own polygon is not a building of that campus. Way 45583037, the former AOL building at
   7777 Infantry Ridge Road, is 39 m from Equinix DC14's building but outside Equinix's site
   polygon, and was listed as a building of DC14; since fix round 6 it is its own record, "Data
   center (Prince William County, VA)", for a reviewer to name. (A different house number on the
   same street was measured as the guard too, and rejected: it also splits Evoque Allen DA1, 900
   Guardians Way, from the unnamed way at 800, which may be a hall of that campus.) Three unnamed
   buildings join on the 2026-10-09 input (Evoque Allen DA1, Digital Realty EWR11 and EWR12,
   Amazon IAD-73 and IAD-74).
8. **A numbered sibling or a street neighbour just beyond the radius.** Two non-campus objects of
   one operator (rule 2's keys) whose boxes are more than `radius_m` but at most 500 m apart join
   when one's name continues a numbered series of the other's cluster (`dissolve.series_key`: the
   name without its last number, and that number, one apart from a member's: "Iron Mountain VA-6",
   364 m from VA-5, with VA-1 to VA-5 and VA-7, one 142-acre campus by Iron Mountain's own page) or
   they have the same `addr:street` (Digital Realty IAD41, Building P at 44751 Round Table Plaza,
   348 m from IAD39; Digital Realty's filing names Buildings L, M, N and P one campus). The series
   keeps a number before the last ("QTS NAL1 DC2" and "QTS NAL 2 DC1" are two series). They do not
   join, and a `same_operator_nearby` note says why, when something separates them: ZIP codes that
   differ, a site polygon of each, an object of another operator whose center lies in the box that
   spans the two, or two points (a point shows where an operator is, not which building: Digital
   Realty's JFK12 and JFK13 nodes in Manhattan, 419 m apart, are two carrier hotels). On the
   2026-10-09 input this joins Iron Mountain VA-6, Digital Realty IAD41, CoreSite VA3 (Reston),
   Digital Realty SJC11 (Santa Clara), CloudHQ MCC2, Microsoft Leesburg Buildings 3 to 5 and AWS
   IAD-140 to IAD-145 with their campuses.
9. **Two links** (`dissolve.multi_link_joins`, fix round 6). After rule 8, two clusters of one
   operator (rule 2's keys) whose members are linked in two or more independent ways within 1 km
   (`SERIES_GAP_M`) join: a name that continues the other's numbered series, the same
   `addr:street`, the same web page (a `website` with a path). Every member with operator
   information must be compatible, and the clusters must not be separated by different ZIP codes,
   two points or a site polygon of each. An object of another operator between them does not
   separate them, as it does for rule 8's single link: two links are not chance. CloudHQ LC1 to
   LC3 and LC4 to LC14, about 650 m apart, share the series LC3-LC4, Loudoun County Parkway and the
   page `cloudhq.com/campus/lc-campus/` ("fourteen data centers" by CloudHQ), with STACK NVA04C's
   node between them; they were two records and are one, "CloudHQ Ashburn Campus (Ashburn, VA)",
   with three phases. Of the 15 `same_operator_nearby` pairs of the 2026-10-09 input, only this
   one has two links (RagingWire's pair has a series only, Equinix's a street only, and the
   Aligned, LightEdge and Stream pairs share a page but are 1.4 km or more apart).

After the joins, `same_operator_nearby` notes also name the clusters of one operator that may
still be one site: a numbered series or a street split within 1 km (`SERIES_GAP_M`: RagingWire
CA3, 720 m from CA1 and CA2, "one of three data centers on our 52.7MW campus" by NTT), or members
that give one web page (a `website` with a path, read for this comparison only and never
published) within 2 km (`SITE_PAGE_GAP_M`), with one link (two make rule 9's join). `tenant_building` notes name a cluster that is one building with
an operator within 150 m (`TENANT_GAP_M`) of a row of at least three buildings of one other
operator, with no third operator within `radius_m`: the Rackspace-tagged building 42 m from
Digital Dallas, which Digital Realty lists as DFW25 of that campus. Both become
`possible_duplicate` items when both clusters make in-scope records (section 5).

**Regions.** The importer passes each object's Census county (point-in-polygon at its center), and
no rule joins two objects in different counties, so a cluster does not cross a county or state
line: xAI's building in Southaven, DeSoto County, MS, joined a Shelby County, TN, record by rule 2
before (and its PNNL row was a `county_mismatch` item), as did Digital Realty IAD53 in Manassas
city with a Prince William County record. One exception: containment in an outline (rule 1) joins
across the line, because a campus inside one site polygon is one facility (Microsoft TRP3, in
Medina County, inside the Texas Research Park Campus outline in Bexar County; Google NBY-4 to
NBY-6, in Franklin County, inside Google's New Albany site in Licking County). The record takes
the representative's county and gets a `county_mismatch` item naming the members across the line.
Every other join a county line blocks is a `county_blocked` note, and the smaller of the two
clusters is held (section 5).

`dissolve_with_notes` returns the clusters and these notes (`DissolveNote`, with `why` for the
last two): `county_line`, `county_blocked`, `mixed_operators` (an object an area without an
operator does not hold), `address_conflict`, `same_operator_nearby` and `tenant_building`;
`dissolve` returns the clusters only.

**Reviewers' joins and splits** (section 9) come around the dissolve, in `osm.py`; the rules
themselves are unchanged. An element a config/overrides/osm.json entry sets `alone` is left out
of the dissolve and is a cluster of its own. After the dissolve, `osm.join_clusters` merges, for
each `join` entry in ref order, the clusters that hold the element and its target, whatever rules
1 to 9, the operator guard and the county regions say; the representative is chosen as above. A
join across a county line adds a `county_line` note (rule `join`): the record keeps its
representative's county and gets a `county_mismatch` item naming the members across the line.
Every other note between two refs that now share a cluster is dropped, since the two are one
campus (the "may be one campus" notes, a `county_blocked` note that would hold one side). A
joined cluster lists its parts (`Cluster.parts`, the clusters the joins brought together), so
that on a populated store only the merge of stored records in different parts is the reviewer's
(section 9).

Two campus objects join each other directly only when they adjoin (rule 2), by name (rule 4) or
inside one site. Joins are
transitive, so a row of same-operator buildings 250 m apart forms one cluster; the widest
cluster on 2026-10-07 spans about 1.6 km (16 AWS objects in Ashburn). Each group of candidates is
swept in latitude order, so only pairs that overlap in latitude are measured.

On the 2026-10-07 snapshot (1,886 objects) the rules before the 2026-10-08 seed check gave 1,114
clusters. The earlier rules (center distances, an exact operator guard, no rule 3, lifecycle-
tagged polygons as campuses) gave 1,367. Box gaps alone give 1,280, the word-prefix guard 1,278
and rule 3 1,117 (its address clause adds 4 joins). Counting lifecycle-tagged polygons as
buildings (section 3) took that to 1,114. On the 2026-10-08 seed input (1,888 objects, without
site polygons, which that query did not fetch) rules 1 to 6 and the regions give 1,106 clusters
(1,115 before); with the 172 site polygons of 2026-10-09 added, 2,044 objects give 1,073. On the
second seed input (2026-10-09, 2,046 objects, without campus outlines or containers) the rules of
the second seed check give 1,037 clusters, from 1,075 (section 13).

The **representative** is the campus or site object (the largest, if a cluster has several, one
that is not planned first: an area tagged `landuse=construction`, `construction=*`, `proposed=*`
or a `construction:` or `proposed:` key does not lead the campus it joined, so the Meta LCO 3
construction site, larger than Meta New Albany's site, does not; a container only when the cluster
has no other area), else the way or relation with the largest bounding box, else the node with the
lowest id. Members and
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
| `canonical_name` | base (`osm._base_name`) = the name of a named campus or site member: of several (a campus and its expansion site), one whose name is not a development's codename ("Project ..."), then one that is not planned, then the largest ("Meta New Albany Data Center", not "Meta LCO 3 Project"; "Google Lenoir Data Center", not "Google Project Cardinal"); a container's name comes last. Else, when two or more building members carry distinct names, their common name (`osm.campus_name`): the words they all begin with, without trailing generic words, when that says more than an operator of the input ("PowerHouse Pacific" for Buildings 1 to 3, "QTS NAL1" for DC1 and DC2, "Digital Realty Dallas"), else their series when each ends in one code and number ("Amazon IAD-124 to IAD-127" for numbers without a gap, "NTT VA1 and VA2", "Digital Realty IAD39, IAD40, IAD41 and IAD73", "CloudHQ LC4, LC5, LC8 and 3 more"); a campus of several buildings is not named after one of them (97 records renamed on the 2026-10-09 input). Names that are no siblings as they stand are compared again (`osm._series_name`, fix round 6): without the leading words that are an operator of the input, series first, with the operator put back ("Digital Realty Silicon Valley SJC31 and SJC37" for "Digital Realty Silicon Valley SJC37" and "Silicon Valley SJC31"; the common words first would give "Digital Realty Silicon Valley", the name of another Digital Realty campus 0.78 km away), else without the names that are only words of another member's name ("Aligned IAD-01 and IAD-02", not "Aligned IAD-02", beside a hall named "IAD"). Names that say which site they mean only by their number, the operator, generic words and a number (`osm._numbered_name`: "Microsoft Data Center 1", for which `dissolve.specific_name` finds no word of its own), name the campus as a series too (fix round 7: "Microsoft Data Center 1 to 3", not its first building's name). When the buildings name no campus, the named points take part like buildings (fix round 7): "DartPoints Baton Rouge" for two nodes BTR01 and BTR02, "NTT VA10 and VA11" for a building and a node; the buildings come first, so the node "Equinix Ashburn DC18" does not break the series of Equinix DC1 to DC22 (with them together, nothing would name the campus and it would take its first building's name). With the points, words they all begin with that, with a number or a code with one after them, are the name of a named element outside the campus, of one of its operators or of none (`osm._stem_names_another`), give the series instead, since the common words would read as the name of every such site and drop the numbers that tell this one apart (round 7 review: "Digital Fortress - Portland 2 and 3" in Beaverton, not "Digital Fortress - Portland", beside the separate record "Digital Fortress - Portland 1" in Portland; "DartPoints Baton Rouge" keeps its common words, no other element being "DartPoints Baton Rouge" with a number). Buildings alone keep their common words as before: on the 2026-10-09 input the same test would also rename Digital Realty New York EWR11 and EWR12, Digital Realty Silicon Valley SJC10 to SJC30, QTS Data Center - Hillsboro 1 and 2 and Switch Las Vegas 4 and 5, names left to a reviewer. Else the representative's `name` (unless it has another primary use: then the most common name of a member without one, "CoreSite - LA2" for the Terminal Annex), else the most common member name, else "{operator} data center", else "Data center". The operator is prefixed unless the base already starts with the operator, an `operator:short` of a member with that operator, or the operator's first word, also misspelt by one letter ("Lumen Ashburn" for Lumen Technologies stays as is; "CyrusOne NVA14" operated by PowerHouse becomes "PowerHouse CyrusOne NVA14"; "CyrusOne Data Center Campus" is not prefixed with "CyrysOne"), or the operator's words as a phrase, legal suffixes dropped, one character apart over the whole phrase (fix round 7: "Date Center West - Eugene" under operator "Data Center West, Inc." is not prefixed: one letter of the phrase "data center west", though the first word alone is under the six letters a one-letter typo needs, `dissolve.MIN_TYPO_LENGTH`). Then " ({city}, {ST})" with `location.city`, the Census place that contains the point (below), else " ({county}, {ST})" with the county's full Census name from the Gazetteer ("Loudoun County", "Manassas city", "Orleans Parish"). The county contains the point; `addr:city` is the postal city of the address, which need not (Flexential Atlanta - Norcross, `addr:city=Norcross`, lies in Peachtree Corners; "Sterling" and "Dulles" are postal names for points in Ashburn), so it never names the record. A bare county name also reads as a city: Taylor County, TX is Abilene, while the city of Taylor, TX is 300 km away. |
| `aliases[]` | every other distinct member name (`osm_name`), the representative's too when the record goes by another name; each cites the members that carry it |
| `parties.operator` | the most common `operator` tag, citing every member that carries it, not counting a member with another primary use or a contractor's tag (section 3); an operator a config/overrides/osm.json entry gives cites that entry's source (section 9) |
| `parties.owner` | the `owner` tag of the largest campus or site member that has one, else the owner every building and point member names when each has one and they agree (normalized), citing those members; one building's owner is not the campus's (only Equinix DC10 of six buildings carried `owner=Digital Realty`) |
| `location.lat`, `lon` | the campus or site object's bounding-box center, else the mean of the member centers, unless that mean point lies in another county than the representative: then the representative's center; 7 decimals |
| `location.precision` | `footprint` if any member is a way or relation, else `site`; `site` also when a member's `note`, `fixme` or `description` says its position is approximate ("location is approximate", "Location approximated from Sentinel 2 low resolution imagery"), with an `unverified_upstream` item |
| `location.geocode_method` / `geometry_ref` | `osm` / `osm:{representative}` |
| `location.street`, `postcode` | "{addr:housenumber} {addr:street}", `addr:postcode`: the representative's value, else the value every member that has one agrees on, else null. A value only some members share would put the representative's name at another building's address (QTS Manassas DC5 was published at DC1's 9400 Godwin Drive). OSM separates several values with ";": a list of house numbers gives the street alone ("Lockridge Road" for "22271;22275;22285;22295"), a list of streets ("FM56;County Road 3610A") no street, since neither is one postal address. |
| `location.city` | The Census place that contains the record's point, more than 0.001° (about 100 m) inside its line, in the record's state (`ctx.places()`: the place polygons `cb_2025_us_place_500k`, downloaded on first use, see reference/README.md; one batch query per import), else null. Never `addr:city`, the postal city of the address: where the two agree, `addr:city` is that place; elsewhere it is only the post office's town (QTS Manassas DC5, mailed to Manassas, lies in Innovation CDP). 831 of the 1,103 records of the 2026-10-08 input have one. |
| `location.county_fips`, `county_name`, `state_abbr` | the representative's county, point-in-polygon against the Census 2025 counties (every member's county, section 4); `field_meta["/location/county_fips"]` = 0.90, `derived` |
| `buildings[]` | one per building or point member, each supported by its own element: `ref` "osm:{ref}", `name`, `phase_id` when the cluster has phases. `sqft` stays null: PNNL's sqft is the footprint's area, not a floor area (section 7). A member drawn twice (an unnamed building without an operator over half or more of another building member's box: way 567575425 on the Apple Data Center in its Mesa site) is listed in `external_ids.osm` only, none of its tags is read, and a `possible_duplicate` item names it |
| `capacity` | see below |
| `status_history`, `phases` | see below; each event and phase cites the members whose tags give its status; `field_meta["/status"]` = 0.60, `imported`, the members of the most advanced group |
| `evidence_level` / `purpose` | `reported` / `unknown`, or `telecom` for a telecom site (then s1 also supports `/purpose`) |
| `site.acreage` | from PNNL campus rows (section 7); `site.building_sqft` stays null |
| `sources[0]` (s1) | the representative: `https://www.openstreetmap.org/{representative}`, "OpenStreetMap contributors", title "OpenStreetMap {representative}", `open_dataset`, `ODbL-1.0`. It supports what its own tags give (the name, its building entry, its street, postcode or power tags) and the point's Census place and county (`/location/city`, `/county_name`, `/state_abbr`), and the point itself when the representative is an area |
| `sources[1]` (s2) | PNNL, only when a PNNL row matched (section 7) |
| `sources` s3, ... | the config/overrides/osm.json entries applied (section 9), then one source per other member whose tags give a published value, in ref order, built like s1: its name as the record's name or an alias, the operator, the owner, the street or postcode, a power tag, its building entry, the status of its group, or the point (the mean of the member centers, when the representative is not an area). A reader of `sources[]` finds each value on the element that states it: before this, every record cited only s1, so Canton's construction status, Flexential Brentwood's operator and street and ACC9's 210 MW pointed at an element that does not state them (326 of 985 records on the 2026-10-09 input) |

`apply_rollup` runs on every candidate, and `atlas import` validates every record before writing it
(07 §3.6). Only these tags reach a record: `name`, `operator`, `operator:short`,
`operator:wikidata` (for joins), `owner`, the `addr:*` fields above, `it_power`,
`input:electricity`, `start_date` and `opening_date` (in notes and review items) and the status
tags. Contact tags such as `phone`, `email` and `website` are never copied (rule 8's notes compare
`website` values without publishing them).

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
- **A carrier hotel's building** (`possible_duplicate`, fix round 6): a building without operator
  information and with a street address, whose box holds the data center points of two or more
  operators that disagree at its own address, leading a cluster with no operator. Each tenant's
  point is its own record (rule 6 joins a point to its building only when the operators agree), so
  a record of the building would count the facility again. Lakeside Technology Center (way
  156520409, 350 East Cermak Road) holds Digital Realty ORD10, Centersquare ORD1 and Equinix CH1,
  and its record duplicated ORD10's; it is the only such building in the 2026-10-09 input.
  `data.other` lists the points.

After the records are built, `_hold_after_build` holds clusters when two records would describe
one site (07 §2.1: one campus, one count). A held cluster makes no record; its item names the
record that stays, if any, until a reviewer decides:

- **An area whose buildings contradict its operator** (`possible_duplicate`): an area with no
  member of its own that lists the street address of objects inside it whose operator disagrees
  (`address_conflict`). Microsoft's Quail Ridge Lane polygon lists the six house numbers of AWS
  IAD-124 to IAD-127 inside it; it was published as "Microsoft (Loudoun County, VA)", counting
  the AWS campus twice under the wrong operator.
- **A site OpenStreetMap names two ways** (`conflict`, on both clusters): an area with members
  of its own that lists the address of another operator's objects inside it. STACK's NVA04
  polygon lists 22275 and 22285 Lockridge Road, the buildings tagged "Amazon IAD-285" and
  "IAD-286" (operator AWS), which directories call STACK NVA04A and NVA04B; both records stood.
- **Two clusters a county line split** (`possible_duplicate`, on the smaller): a rule would join
  them but for the line (`county_blocked`). Digital Realty IAD53 in Manassas city stands 155 m
  from IAD51 in Prince William County, one Manassas Brickyard campus; xAI's Southaven building
  155 m from Colossus 2 in Memphis.
- **A development mapped hall by hall** (`possible_duplicate`, on all but the largest): three or
  more in-scope records whose members are all unnamed, operator-less halls of at least 5,000 m²,
  in one county and chained within 1 km. The 40 Lancium Clean Campus halls near Abilene were
  eight records "Data center (Taylor County, TX)"; when the query finds the site's polygon
  (section 1) they are one record.

The PNNL rows on a held cluster count as matched (`pnnl_on_held`), and the cluster's other items
(start dates, approximate positions, PNNL conflicts) are not filed.

Two more kinds of `possible_duplicate` item are about records that both stand, from the dissolve's
notes (section 4), one per pair of in-scope records (records of one name have the same-name item
instead):

- **One operator's buildings that no rule joined** (`same_operator_nearby`): a numbered series,
  a street or a web page links them, beyond rule 8's reach or with something between them; the
  item says what links and what separates them, and names the other record (`data.other`):
  "'RagingWire CA3' continues the series of 'RagingWire CA2', 720 m apart"; CloudHQ's two LC
  records on Loudoun County Parkway; QTS NAL1 and NAL 2 on Beech Road Southwest, each in its own
  polygon.
- **A lone building beside another operator's row** (`tenant_building`): it may be a building of
  that campus whose operator tag names a tenant (Rackspace's DFW3, which Digital Realty lists as
  DFW25 of Digital Dallas), or a neighbour; the item is on the lone building's record.

### Telecom sites

07 §2.2 puts telecom central offices and edge sites out of scope, and OSM `telecom=data_center`
also tags cable landing stations and telephone company offices. A cluster is a telecom site when
every member that has a `name`, `alt_name` or `operator` names a cable landing station, a landing
station, a central office, a wire center, or a telephone company or cooperative ("Telephone Co",
"Telephone Coop", "Cooperative Telephone"), or is tagged `telecom=exchange` or
`telecom=central_office`. Since fix round 6 a member is also a telecom site when it is a
satellite teleport or earth station: its name, `alt_name` or operator says "earth station" or
"satellite (ground) station", or its `description` or `note` says teleport, earth station or
uplink, unless those texts also say data center, colocation, hosting or the like (the strong
words below). "Teleport" in a name alone does not count: Telehouse Teleport (way 247428943, 7
Teleport Drive) is a data center. Encompass's Lino Lakes facility ("houses a state-of-the-art
teleport", by its description) and SES's Hawley Earth Station were published as data centers.
Unnamed members count neither way, and one data center name in the cluster keeps it in scope. The record gets `scope: "out_of_scope"`, `purpose: "telecom"` and an
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
  building", a tape vault, vital records or records storage, a genealogy research room (a family
  history or FamilySearch center, "genealogy"): "SDSU Computer Room", "Vital Records", "Iron
  Mountain Records Storage", the "Family History Center" of the Church of Jesus Christ of
  Latter-day Saints in Montpelier, ID, which was published as an operating data center;
- **every member** is a `building=shed`, `hut`, `cabin`, `kiosk`, `container`, `garage` or
  `carport`, or is tagged `utility=telecom` (a 5 m by 5 m telecom shed on a school campus);
- **every member** is a house: `building=house`, `detached`, `semidetached_house`, `bungalow`,
  `terrace` or `static_caravan` (fix round 6: "Lumen" in St. George, UT, a 169 m² house among 25
  others, was published as an operating data center). `building=apartments` is not one, as
  Cologix Jacksonville JAX1 is tagged that way; and "Lumen" is not a telephone brand of the screen,
  because Lumen's Spokane, Phoenix and Tustin buildings are likely its data centers.

The crypto rule also reads `resource` and `quarry` values (Compute North's Kearney site is
`resource=cryptocurrency` + `quarry=mining`), and the room rule the word "lab" (not "Labs", a
common company name, nor "Laboratory": a national laboratory's computing center is in scope) and
"workstations": Duke's "Brandaleone Lab for Data and Visualization" is nine workstations on a
library floor.

The other rules apply only when no member's name, description or operator says data center,
colocation, hosting, cloud, a server farm, a compute or computing center, supercomputing or HPC,
or names a DC number, a NAP, an IDC, a carrier hotel or an internet exchange. Since fix round 6
"server", "computer(s)", "compute", "computing" and "computation" alone no longer count: they
name businesses as often (a process server's point, "All About Serving Process Server", and "QES
Computers", an 87 m² shop, were published; "Alembic Computer Services, Inc." and LSU's "Center
for Computation & Technology" are now held too), while "Iron City ... Compute Center" and "Date
Center West" stay in scope:

- another primary use: every member with a name or operator carries `amenity`, `shop`,
  `tourism`, `healthcare` or `healthcare:speciality` (section 3): "Myndshift Technologies" is
  tagged `healthcare:speciality=neurology` next to a neurology clinic, Clarkson University's
  "Old Main" `amenity=university`;
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
object would be used instead); otherwise the field is null. An IT load cannot exceed the
facility's input: when the summed `it_power` exceeds the summed `input:electricity`, or one
member's `it_power` exceeds its own `input:electricity`, one tag is wrong, so neither field is set
and a `conflict` item names the members (ways 701930929 and 701930930, 47 MW IT on 40 MW input
each). `mw_as_stated` always lists the raw tags, for example "OSM it_power: Amazon IAD-78 45 MW;
Amazon IAD-79 47 MW; Amazon IAD-80 38 MW | OSM input:electricity: …". `field_meta["/capacity/it_mw"]` and `["/capacity/facility_mw"]` =
0.70, `imported`, citing the members whose tags were summed, when set; every member with a power
tag supports `/capacity/mw_as_stated`. Every value seen in the US data was "N MW".

### Status, events and phases

Each member's tags go through `crosswalk.from_osm_tags` (planned, then construction, then
operating), which reads a proposed tag as `announced`, because 07 §2.3's `proposed` needs a formal
application or filing and no OSM tag is one, and writes every status as an `other` observation. A
building tagged `industrial=data_centre` (or `data_center`), with no `data_center` value elsewhere,
counts like `building=data_center` ("IBM Quantum Data Center", way 195803647, is tagged only
`building=industrial` and `industrial=data_center`), lifecycle tags first; a site polygon without
a building tag gets no status. Members are grouped by the resulting status.

- **One status (almost every cluster).** One event: seq 1, the status, event `other`, `as_of` =
  the snapshot date (the date of `timestamp_osm_base`, day precision), citing the members whose
  tags give the status (Canton's construction status cites its five halls, not the site polygon,
  which has no building tag), note "Tagged
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
- **A construction site that has not opened** (`osm._construction_site_groups`, fix round 6):
  when the representative is an area tagged as planned or being built (`landuse=construction` or a
  `construction` tag) and its `opening_date` is after the snapshot date, the members read as
  operating (a bare `telecom=data_center` or `building=data_center`) are regrouped with the site
  into one `under_construction` group, and the site's `opening_date` adds the planned `energized`
  event; a `conflict` item asks a reviewer to check whether a building operates. PowerHouse CTP-02
  (way 1493522242: `landuse=construction`, `industrial=data_centre`, `opening_date=2027`) holds a
  hall tagged only `telecom=data_center`, and was published as operating. A construction polygon
  without a future `opening_date` keeps its groups (the review accepted operating for Meta
  Cheyenne, QTS NAL1, Digital Realty IAD51 and Meta Rosemount), and Arcola Tech Park's
  `opening_date=2030` is on a `landuse=industrial` polygon, which is not planned.
- On later runs, an observation (`other`, or a `first_reported` event an earlier version wrote)
  keeps the earliest `as_of` already stored for the same status from OpenStreetMap sources (the
  members it cites change as the cluster does), and the note names no date, so weekly runs change
  no record. That holds for an observation a correction removed or redated as well, which the
  stored record keeps only among its `field_meta["/status_history"]` conflicts
  (`corrections.lost_events`), and for one a Fix's `support` (or a `replace_event` that keeps its
  date) cites beside the elements: the correction then summarises the observation the same way
  every week (Wichita Falls, Project Cardinal and Elk Grove Village's `remove_event` gave a new
  `/status_history` correction, and rewrote the record, with each snapshot before fix round 6's
  review). The phase is not compared: when a building under
  construction appears next to an operating campus, or later goes, the operating event moves
  between no phase and `osm-operating` but its status has not changed. A status change replaces
  the event; recording transitions as new events is the M2 diff (07 §4.2 step 5).
- **A reviewer's status** (an entry's `status`, section 9) replaces the crosswalk for the element,
  and an entry on a campus or site polygon for every member of its cluster without an entry of its
  own (a site polygon has no status of its own). Those members form a group of their own: one
  `other` event dated the entry's `status_as_of`, else the snapshot date, so it dates nothing
  either, citing the entry's source (s3, ...), not the members' elements; as a phase it is
  `review-{status}`, named after the source's publisher ("Announced per Baxtel: Equinix Ashburn
  DC18"). Its `opening_date` path is unchanged (PowerHouse CTP-02's site still adds its planned
  energized event), its `start_date` is not read, and the construction-site rule leaves it alone.
  When such a group leads the record, `field_meta["/status"]` is `human_entered` with the 07 §3.4
  confidence of the entry's source, the conflict term counted against OpenStreetMap's tag. A
  paused, denied or cancelled status ranks after the active ones. On later runs, an undated entry's
  observation keeps the earliest stored `as_of` of an observation of its status citing the same
  URL; a dated one keeps its `status_as_of`.
- **Corrections** (an entry's `corrections`, section 9) change the built record's events after all
  of this: `add_event`, `replace_event`, `remove_event`, `observe` and `first_report` (Wichita
  Falls' approval replaces OSM's operating observation, matched as `{"event": "other", "status":
  "operating"}`, never by its snapshot date). The status and dates are then rolled up again, and a
  removed or redated event stays in `field_meta["/status_history"]` conflicts
  (docs/corrections.md). OpenStreetMap's observations date nothing, so the first dated event of
  an OSM record is a reviewer's, and a reviewer's event never dates a first report it is not: an
  entry whose earliest dated event is no announcement, filing or first report stops the run until
  it has a cited `first_report` op (section 9).

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
  the polygon; the projection difference was at most 1.3%), so it is a `conflict` item. So is a
  row whose name gives the member another building number of the same code (`pnnl.site_number`,
  the last code and number of a name, so "Amazon IAD78" and "Amazon IAD-78" agree): PNNL's "NTT
  Ashburn VA8 Data Centre" lies on the way OpenStreetMap renamed "NTT VA9" on 2026-07-24, and
  directories put the operating VA8 at its address; the match still goes by position, and a
  reviewer decides which building it is (four rows on the 2026-10-09 input, with the override
  that names that way VA8 again, section 9).
- A `campus` row gives `site.acreage` = sqft / 43,560, one decimal, only when it hit a campus or
  site object, and only when every campus or site member that no larger one covers has such a
  row; a nested campus does not add to its site. A campus row whose polygon the import did not
  read lands on a building (Microsoft Boydton, 258.8 acres, on a 4.9-acre building before the
  site polygons were fetched), so it is a `conflict` item instead. No acreage is set, and a
  `conflict` item says why, when OSM calls the member's position approximate (Meta Cheyenne:
  "location and boundary very approximate", 260.6 acres from an older version of a polygon now
  about 372 acres), or when the rows' area is more than 5% off the area of the outline the
  import read (`dissolve.ring_area_m2`, for a way; Amazon New Albany's Jug and Beach Road row,
  35.0 acres, against a 113.7-acre outline; Google Project Cardinal, 78.2 against 52.4), or,
  without an outline, more than 5% above the bounding box.
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
| `osm.jsonl` | `out_of_scope` | the record's state is outside the 50 states and DC, it is a telecom site, the scope screen doubts it is a data center, or an override drops it (the record is still written, with `scope: "out_of_scope"`; sections 5 and 9) |
| `osm.jsonl` | `unit_parse` | an `it_power` or `input:electricity` value that is not a power value |
| `osm.jsonl` | `missing_location` | an element without a position, or a cluster no Census county contains |
| `osm.jsonl` | `unknown_status` | a held cluster with no status: a site polygon with no data center object inside, or no member tag the crosswalk knows |
| `osm.jsonl` | `unverified_upstream` | a `start_date` not used as a date; a position OSM calls approximate; a held cluster planned only in OSM, with no name and no operator; an operator tag that names a general contractor (not used); two operators one letter apart, read as one; buildings of one record with one numbered name and different statuses (way 1560827941, under construction, and the operating way 1188691868 are both "NTT VA8" once the override gives the second its name back; Stack Infrastructure's four "POR03" ways) |
| `osm.jsonl` | `conflict` | an `opening_date` that has passed while OSM still tags the site as not operating (not used); members read as operating on a construction site whose `opening_date` is still to come (the record is under construction; `data.refs`, `opening_date`); a held cluster of a site OSM names two ways (section 5); an `it_power` above the `input:electricity` it is drawn from (neither is published; `data.it_mw`, `facility_mw`, `members`); a config/overrides/osm.json entry that gives an element a name another element carries within 2 km (section 9; `data.osm`, `other`, `name`, `gap_m`) |
| `osm.jsonl` | `possible_duplicate` | records with the same canonical name chained within 1 km (one item per group); an object inside a campus or site that has no building of its own, or lists its house number, or a node inside a building, whose operator disagrees; an area without an operator that does not hold all its box covers; a held unnamed building drawn over another, or a member drawn over another of its own record (left out of `buildings[]`); a held carrier hotel's building (section 5); a cluster held after building (an area its buildings contradict, the smaller side of a county line, a hall of a development mapped hall by hall; section 5); two records of one operator that a series, a street or a web page links, and a lone building beside another operator's row (section 5) |
| `osm.jsonl` | `county_mismatch` | a record whose site outline, or a reviewed join (section 9), holds members across a county line (they are named; the record has its representative's county) |
| `osm.jsonl` | `invalid` | a cluster that does not fit the record schema (for example a point in Guam, whose longitude the schema does not accept) |
| `pnnl.jsonl` | `unmatched` | a PNNL row that no data center element the import read takes |
| `pnnl.jsonl` | `county_mismatch` | PNNL's county is not the record's county (no row with that key agrees) |
| `pnnl.jsonl` | `possible_duplicate` | a second building row on one OSM building |
| `pnnl.jsonl` | `conflict` | a campus row on a building; a building row larger than its building's box, or whose name gives the building another number; a campus row on a member whose position OSM calls approximate, or more than 5% off its outline (or above its box) |

A config/overrides/osm.json correction (section 9) adds items of these kinds too: its `hold` op
files one of the kind it names (default `conflict`) and its `out_of_scope` op an `out_of_scope`
item, each naming the cluster's representative, with `data: {source_url, quote, reviewed_at,
basis, file}`. The items about a record whose corrections were applied (any of its refs) get
`data.corrected_by`, "config/overrides/osm.json (reviewed {date}): {reason}", so the review page
shows the decision; no item is dropped.

`atlas import` adds its own kinds (`conflict`, `held_human_reviewed`, `held_merged`, `invalid`,
`removed_upstream`) to `osm.jsonl`. Review files are rewritten on every run, sorted.

The receipt `data/imports/osm.json` has the metrics `objects`, `clusters`, `held` (clusters held
for review, with no record), `out_of_scope`, `unit_parse`, `skipped`, `overrides` (the entries
applied), the corrections counts (`corrections_entries`, `corrections_ops`, `records_corrected`,
`corrections_held`, `corrections_flagged`, `corrections_out_of_scope`,
`corrections_importer_gap`; docs/corrections.md),
and with PNNL `pnnl_rows`,
`pnnl_matched`, `pnnl_matched_by_id`, `pnnl_matched_by_name`, `pnnl_on_held` and
`pnnl_match_rate`.

## 9. Reviewers' decisions: config/overrides/osm.json

Some errors are OpenStreetMap's own: an operator tag that outlived a sale (Ntirety's San Francisco
site, run by Data Canopy since it bought Ntirety's colocation business), a tenant's name on a
building it left (the Virginia Information Technologies Agency at 11751 Meadowville Lane). The
importer cannot see them, so a reviewer records the decision in `config/overrides/osm.json`, the
smallest form of the plan's `/drop` memory (07 §6.7): the next run applies it, and no later run
proposes the stale value again. Each entry is keyed by OSM ref and has two layers
(docs/corrections.md). Its top-level keys are **element decisions**, applied before the dissolve
with one top-level citation: it drops the element from scope, replaces its name or operator, joins
it to another element's campus, sets it alone or gives its status. Its `corrections` are cited
**record ops**, applied to the record built from the element (below):

```json
"node/10780794741": {
  "operator": "Data Canopy",
  "reason": "The node's operator=Ntirety is out of date: ...",
  "source_url": "https://baxtel.com/data-center/datacanopy-san-francisco",
  "publisher": "Baxtel",
  "source_type": "commercial_aggregator",
  "quote": "The just-bought colocation company Data Canopy took over the colocation data center hosting division of IT security provider Ntirety.",
  "retrieved_at": "2026-10-09T13:18:55Z",
  "reviewed_at": "2026-10-09"
}
```

| Key | Meaning |
|---|---|
| `drop` | `true`: the element is not a data center in scope (07 §2.2). It leaves the dissolve and becomes its own record with `scope: "out_of_scope"`, never published, and an `out_of_scope` item gives the reason and the source. |
| `name` | replaces the element's `name` tag; `null` removes it (a tenant that left: the record is then named by its operator or "Data center"). |
| `operator` | replaces the element's `operator` tag and removes `operator:wikidata`, `operator:short` and `operator:wikipedia`, which name the old operator; `null` removes it. |
| `join` | `"<ref>"`: the element is one campus with that element. After the dissolve, the clusters that hold the two are merged, whatever the dissolve rules, the operator guard and the county regions say (section 4): NTT's RagingWire CA3, 720 m from CA1 and CA2, beyond every rule. The record cites the entry for the element's building entry. |
| `alone` | `true`: the element is a campus of its own: it leaves the dissolve and is its own cluster, in scope unless the scope screen doubts it (`drop` without the scope change). |
| `status`, `status_as_of` | the element's lifecycle status as the source gives it, in place of the crosswalk's; on a campus or site polygon, the status of every member of its cluster without an entry of its own. `status_as_of` (`YYYY`, `YYYY-Qn`, `YYYY-MM` or `YYYY-MM-DD`, never after the run's day) is the day the source shows it; without it the observation is dated with the snapshot (section 5). A building planned on an operating campus (Equinix DC18) becomes a phase of its own. |
| `expect` | `{tag: value}`, `null` for a tag the element must not have: a guard on the element's tags as read, before any decision; a mismatch stops the import (the element was retagged upstream). |
| `corrections` | a list of cited `Fix`es with record ops (below, and docs/corrections.md). |
| `reason`, `reviewed_at` | why, and the day of the decision. |
| `source_url`, `publisher`, `source_type`, `quote`, `retrieved_at` | the source that states the correction, the verbatim span (at most 300 characters) that states it, and when it was read; `archive_url` is the Wayback snapshot read when the page refuses the project's user agent. `source_type` defaults to `base.classify_source`. |

The element decisions are `drop`, `name`, `operator`, `join`, `alone` and `status`; an entry with
one of them has the top-level citation (`reason`, `source_url`, `publisher`, `quote`,
`retrieved_at` and `reviewed_at`, as before), and an entry with none must have `corrections` and
no top-level citation: each Fix cites its own page. An entry must drop the element or replace its
name or operator (or join it, set it alone or give its status), or carry corrections; `drop`
excludes every other decision and `corrections`, `join` and `alone` exclude each other,
`status_as_of` needs `status`, and a paused, denied or cancelled status needs a government,
regulator, utility or ISO, SEC, company, trade press or news source (07 §6.4 gate 6). Any other
key, a key that is not a ref, or a malformed value stops the import (exit 1), and so does, before
anything is built:

- an entry whose element the input does not have: an element deleted or retagged upstream must be
  reviewed, not silently stop applying (also for the target of a `join`);
- an `expect` guard the element's tags do not meet;
- a decision that changes nothing: a `name` or `operator` the element has already (an operator
  entry that also removes `operator:wikidata` changes something), a `join` whose two elements are
  in one cluster already (by the dissolve or an earlier join; joins apply in ref order) unless
  different stored records hold the two (below), an `alone` element the dissolve would leave
  alone anyway unless a stored record that holds it is or was merged (below), a `status` the
  crosswalk gives every member it decides already (a member the construction-site rule regroups,
  section 5, is the entry's to decide, whatever status it gives: PowerHouse CTP-02's hall, tagged
  `telecom=data_center`, operates if a source says so);
- a `join` whose target an entry drops or sets alone, or that joins its own element;
- two campus or site polygons of one cluster that each give a status, or two entries of one
  status in one cluster with different `status_as_of`;
- a `status_as_of` or a Fix's date after the run's day (`check_fix_dates`).

An entry that gives an element a name another element of the input
carries within 2 km (`OVERRIDE_NAME_GAP_M`, centers, `normalize_name`), when no entry drops that
element or replaces its name, is a `conflict` item instead, and the import goes on
(`osm.override_name_items`, fix round 6): the entry naming way/1188691868 "NTT VA8" while way
1560827941, 160 m away, is "NTT VA8" in OSM made one campus list two buildings of one name. A test fixture names its own file with `--overrides`
(`tests/fixtures/osm/overrides-empty.json`).

A record built from an element an entry changed cites the entry's source (s3, s4, ...) with its
quote (`quote_match: "human"`): the operator's `source_ids` and, when the record's name comes from
the entry, `/canonical_name`; the element's own OSM source then no longer supports those values
(the element's building entry cites both). A `join` entry's source supports the joined element's
building entry, an `alone` entry's source is listed with no value of its own, and a `status`
entry's source is cited by its group's event and phase in place of the members' elements. The receipt's metric
`overrides` counts the entries applied.

**Corrections.** After the build and every hold, `osm.correct_candidates` applies each entry's
`corrections` to the record that holds its element, whatever cluster the dissolve and the joins
put it in, the entries of one record in ref order (`corrections.apply_fixes`: the ops, their
guards and provenance are in docs/corrections.md). A correction whose guard no longer matches the
built record stops the import, and so does one for an element whose cluster was held (no record
was built: release the hold, by a `join` for example, or remove the correction); an entry whose
corrections only `hold` or put out of scope an element the importer held already is fine. So does
an entry whose corrected record's earliest dated event is one its ops add or change and is no
announcement, filing or first report (an approval, a construction start, a denial: fix round 7,
`corrections.reviewer_first_report`, as AI GridWatch holds such a row): it would be the record's
`first_reported`, but a project is public before it is approved or built. The message names every
such entry with its event and date; a cited `first_report` op (`from` null, an `as_of` before the
first day of every dated event's period) dates the first report instead. The
review items about a corrected record name the decision (`data.corrected_by`, section 8). A
postcode OSM gives wrong:

```json
"way/818167571": {"corrections": [{
  "basis": "upstream_error",
  "ops": [{"op": "set", "path": "/location/postcode", "from": "68106", "value": "68102"}],
  "publisher": "1623 Farnam", "source_type": "company_release",
  "quote": "1623 Farnam Street, Suite 860 Omaha, NE 68102",
  "reason": "The way's addr:postcode 68106 is wrong; the building's own site gives 68102",
  "retrieved_at": "2026-10-10T12:00:00Z", "reviewed_at": "2026-10-10",
  "source_url": "https://www.1623farnam.com/"}]}
```

A join with a cited rename: `"way/190868312": {"join": "way/32560290", "expect": {"name":
"RagingWire CA3"}, <the citation of NTT's CA3 page>, "corrections": [<a Fix with {"op": "rename",
"from": "NTT RagingWire", "base": "NTT Sacramento CA1 to CA3"}, cited to NTT's campus page>]}`.

How the reviewers' kinds of correction are written: a duplicate within OSM is a `join` (a building
that is a tenant's is then an `add_party` with role `tenant`, citing the way, and a `set_party`
operator where the most common operator is wrong); a split is `alone`; a campus name is the
record op `rename` (`old_as_alias` keeps OSM's name as an alias); a whole multi-element site that
is not a data center is the record op `out_of_scope` (`drop` takes one element out of the
dissolve); a wrong address or owner is the record op `set` on `/location/street` or
`/location/postcode`, or `set_party` owner, not a tag change, since none of the address
corrections needs the corrected tag inside the dissolve; a stale status is an element `status` or
the event ops (`remove_event` of OSM's observation, matched by event and status, with `add_event`
or `observe`).

On a store that holds the records already, the candidate of a joined cluster gives the join's
parts in `Candidate.joined` and an alone element's ref in `split_from`, so `merge_import` merges
the stored records into the oldest one (`merged_into`, 07 §6.6) or takes the element back out
(docs/corrections.md); on the empty-store seed a join gives one record and no `merged_into`. Only
stored records in different parts merge: when an OSM edit makes the dissolve take another stored
record's element into a joined campus, the candidate stays a `conflict` item, as on a campus
without a join. Three cases are for a populated store:

- a `join` between two elements the dissolve puts in one campus already, held by different
  stored records (a name or operator entry made the dissolve join two records, Csquare EWR2's two
  buildings): the `conflict` the entry leaves becomes the reviewer's merge, the part split where
  the target's record ends. On an empty store such a join changes nothing and stops the run;
- `alone` on the element of a merged record: the split that takes it back (it stays valid once
  the record is unmerged, and keeps the two apart). On an empty store no record was merged and
  the dissolve leaves the element alone anyway, so, as with the in-cluster join, such an entry
  changes nothing and stops the run: remove it before a reseed;
- withdrawing a `join`: the merged record is taken back as well (docs/corrections.md).

Entries of 2026-10-09 (every one read with the project's user agent from the cited page):

| Element | Decision | Source |
|---|---|---|
| node/10780794741 (630 3rd Street, San Francisco) | operator Ntirety → Data Canopy | Baxtel, DataCanopy: San Francisco |
| way/293211687 (Intergate Seattle East Building 4, 3433 South 120th Place) | operator Digital Realty → Sabey Data Centers; the building joins Sabey's Intergate campus | PeeringDB facility 4226 |
| way/439365340 (11751 Meadowville Lane, Chesterfield County) | name "Virginia Information Technologies Agency" removed (the agency left in 2022) | Baxtel news, 2023-05-15 |
| way/460212563 (Burbank, `owner=Digital Realty`) | operator Centurylink → Centersquare | Baxtel, Centersquare: Burbank BR1 |
| way/635022480 (Ascent - Atlanta Data Center, Alpharetta) | name removed, operator Ascent → GI Partners | Baxtel, GI Partners: Alpharetta |

Entries of fix round 5 (2026-10-09, the record errors of the fourth seed's pre-check whose
correction cites a source for a stale or wrong OSM name, operator or scope; every page read with
the project's user agent):

| Element | Decision | Source |
|---|---|---|
| node/3732415309 (The Pittock Internet Exchange, Portland) | operator Alco Properties → 1547 Critical Systems Realty (Alco sold the Pittock Block in 2021) | 1547 Critical Systems Realty, Portland OR / PTOR1 |
| way/138643160 (601 Benjamin Drive, Springfield, OH) | name "LexisNexus Springfield" → "5C Data Centers CMH01", operator 5C Data Centers | Baxtel, LexisNexus Springfield |
| way/396296733 (17300 Highway 99, Lynnwood) | name "Centersquare Lynnwood SE1" → "Csquare Lynnwood SEA3", operator Csquare | Csquare, Seattle spec sheet |
| way/440666998 (1928 East Deere Avenue, Santa Ana) | name "Xo Communications Colocation Data Center" removed, operator Verizon | Baxtel, Verizon: Santa Ana |
| way/487170215 (28401 Betka Road, Harris County, TX) | operator DXC Technology → Serverfarm | Baxtel, Serverfarm: Houston HTX1 |
| way/903642490 (Family History Center, Montpelier, ID) | dropped: a genealogy research room, not a data center (the scope screen now doubts it too) | FamilySearch, Bear Lake Idaho FamilySearch Center |
| way/1188691868 (44710 Gigabit Plaza, Ashburn) | name "NTT VA9" → "NTT VA8" (renamed in OSM on 2026-07-24) | Baxtel, NTT: VA8 |

Not entered: way/210917861 ("Data Storage Centers", Phoenix; the BBB profile refuses the
project's user agent and the Wayback Machine has no copy), node/3468203102 (LSU's Center for
Computation & Technology: its page calls it a research center, which does not say no computing is
housed there), way/1560827941 (Baxtel puts NTT VA9 at 44470 Gigabit Plaza, not the way's 44725),
way/682979410 ("Google Project Cardinal": the cited article places the project on about 60 acres
adjacent to the campus, so it does not show this way to be the campus; the way joins Google's
Lenoir campus by rule 2, which names the record "Google Lenoir Data Center").

Entries of fix round 6 (reviewed 2026-10-13: the owner's corrections of the fifth seed's review,
PR #2; every page read with the project's user agent on 2026-10-13, the site polygon way
1387391200 through the OSM API, because the website challenged automated clients). Element
decisions carry the top-level citation and an `expect` guard on the tags they replace or read;
record ops are cited `corrections` with `basis` and the review PR as `issue_url`:

| Element | Decision | Source |
|---|---|---|
| node/9669340912 (111 Market Place, Baltimore) | operator Crown Castle Fiber → Zayo (`set_party`); renamed "Zayo Baltimore - 111 Market Pl", the old name a former_name alias | Crown Castle, directory page (it sold its fiber business to Zayo); PeeringDB facilities in Baltimore |
| node/10910879064 (Digital Realty ORD10) | joins way/156520409, the Lakeside Technology Center at the same address; the record is renamed "Digital Realty ORD10", Lakeside Technology Center an osm_name alias | Baxtel, Digital Realty: ORD10; OpenStreetMap way 156520409 |
| node/10910879065 (350 East Cermak Road) | name "Chicago (Cermak) ORD1", operator Centersquare → Csquare | Csquare, Chicago |
| way/190868312 (RagingWire CA3) | joins way/32560290 (CA1 and CA2, 720 m); renamed "NTT Sacramento CA1 to CA3", "NTT RagingWire" a former_name alias | NTT, Sacramento CA3 and Sacramento data centers |
| way/138643160 (601 Benjamin Drive, Springfield) | the fix-round-5 entry, cited to Baxtel's CMH01 page instead of the LexisNexus page, which calls the site decommissioned | Baxtel, 5CDC: Columbus CMH01 |
| way/155739337 (2500 West Union Hills Drive, Phoenix) | name "Aligned PHX-01/02/03", operator Aligned Data Centers (Aligned Energy is its former name) | Aligned Data Centers, Phoenix |
| way/220518160 (2055 East Technology Circle, Tempe) | name and operator Digital Realty removed: the building is Mapletree Industrial Trust's, vacant until a new lease in FY25/26 | Mapletree Industrial Trust, investor presentation of May 2026 |
| way/460050155 (Equinix DC10 and its cluster) | joins way/234722029's cluster (Filigree Court, 710 m); renamed "Equinix Ashburn" | Equinix, release of 2020-11-17 |
| node/14156477686 (Equinix Ashburn DC18) | `status` announced: a phase of its own | Baxtel, Equinix: DC18 |
| way/279410762, way/279410766 (1919 Park Avenue and 300 JFK Blvd East, Weehawken) | name "EWR2", operator Cyxtera and Digital Realty → Csquare; rule 2 joins them | Csquare, New York/New Jersey spec sheet |
| way/318069356 (John F. Simms Building, Santa Fe) | postcode 87501 → 87505 | New Mexico Department of Information Technology |
| way/336387583 (1401 Russell Street, Baltimore) | name "Baltimore (BAL)", operator TierPoint | TierPoint, Baltimore |
| way/377345296 (2425 Busse Road, Elk Grove Village) | name "Elk Grove Village ORD2", operator Centersquare → Csquare | Csquare, Chicago |
| way/300969615 (Amazon IAD-6, Digital Loudoun Plaza) | tenant Amazon Web Services (rule 1's owner check joins IAD-6 and IAD-13 to the plaza) | OpenStreetMap way 300969615 |
| way/460212563 (3015 Winona Avenue, Burbank) | the fix-round-4 entry replaced: name "BUR10", operator Centurylink → Digital Realty | Digital Realty, Los Angeles |
| way/597835788 (Stream DFW IV) | operator Stream removed: Stream sold the building in 2011 | Stream Data Centers, Dallas |
| way/597876719 (Rackspace Richardson DFW3, 1215 Integrity Drive) | joins way/597876730, Digital Realty's DFW18; the record keeps "Digital Realty Dallas" (`rename`) and gains tenant Rackspace; no owner, though the correction asked for owner Digital Realty: no page read states who owns the building (Baxtel says only that Rackspace does not, Digital Realty that DFW25 is on "our 69-acre Digital Dallas campus") | Digital Realty, DFW25; Baxtel, Rackspace Richardson DFW3 |
| way/1560827941 (44725 Gigabit Plaza, under construction) | name "NTT VA8" → "NTT VA9": Baxtel's VA9, under construction for 2027, has its map point 40 m from the way (its address reads 44470 Gigabit Plaza); the NTT VA8 conflict item goes | Baxtel, NTT: VA9 |
| way/747985149 (2236 Airport Drive, Wichita Falls) | OSM's operating observation replaced by an approval of 2026-08 (`remove_event`, `add_event`); first report 2024-06-05, announced: DataNovaX's Pioneer Park on Airport Drive (`first_report`, fix round 7) | KAUZ News Channel 6, 2026-08-13; Dallas Innovates, 2024-06-05 |
| way/818167571 (1623 Farnam Street, Omaha) | postcode 68106 → 68102 | 1623 Farnam |
| way/818790301 (Credit Suisse: Clifton Data Center) | operator and owner DXC Technology; renamed "DXC Technology Clifton Data Center", OSM's name an osm_name alias | DXC Technology, release of 2019-05-13; The Register, 2019-05-14 |
| way/1000784035, way/1059030596, way/1280340215 (9750, 9740 and 9730 Hornbaker Road) | name "AWS" removed, operator Amazon Web Services → Stack Infrastructure: rule 2 joins them to STACK's way/1059036248 | Baxtel, Stack: NVA02A, NVA02B and NVA02C |
| way/1210421871 (5635 South Avenue, Boardman) | dropped: Carrier Services Group's telecom-equipment warehouse | Carrier Services Group |
| way/1238705019 (Involta Independence) | name "Ark Independence", operator Ark Data Centers; street 7300 East Pleasant Valley Road (`set`) | Baxtel, Ark: Independence |
| way/1276518687 (Digital Realty IAD55) | street 22588 Fitness Court → 22588 Relocation Drive | Loudoun County, address points |
| way/1343774537 (CloudHQ's LC campus, joined by rule 9) | renamed "CloudHQ LC Campus", "CloudHQ Ashburn Campus" an osm_name alias | CloudHQ, LC Campus |
| way/1081973944 (QTS San Antonio II) | street 8635 → 8535 Potranco Road | QTS, San Antonio |
| way/1387391200, way/909455922, way/1292897241 to way/1292897243 (True North Data Centers) | operator True North Data Solutions → Compass Datacenters: rule 1 takes Compass IAD IE (way/1387391201) into the polygon; renamed "Compass Loudoun County I", "True North Data Centers" an osm_name alias | Dgtl Infra, 2022-03-09; OpenStreetMap way 1387391200 |
| way/1483070134 (Project Cardinal, Yorkville) | the under-construction observation of the planned outlines replaced by the approval of 2025-11-10 (Ordinance No. 2025-78); the land is still farmed; first report 2025-03-14, proposed: Pioneer Development's annexation, rezoning and PUD applications (`first_report`, fix round 7) | United City of Yorkville (the project page; the application package of 2025-03-14) |
| way/1486780799 (Aligned DFW-04) | street 401 → 601 North Star Road | Texas Department of Licensing and Regulation, TABS2025024707 |
| way/1539603380 (32 container-sized buildings, Venango County) | `out_of_scope` for the whole cluster: the Scrubgrass bitcoin mine | explore Clarion, 2026-06-02 |
| way/1547829412 (Lonestar Dream -Tarbush Site) | `out_of_scope` for the whole cluster: a bitcoin-mining hosting site | Bitcoin Magazine, 2024-04-03 |
| way/1558344919 (unnamed, 50 NW Point Boulevard, Elk Grove Village) | OSM's operating observation replaced by construction start 2024: Aligned's ORD-03; first report 2023-06-09, proposed: Aligned's plans for its Northwest Point Boulevard campus submitted to the village (`first_report`, fix round 7) | Baxtel, Aligned: ORD-03; Journal & Topics, 2023-06-09 |

Fix round 7 stops a run whose correction dates the first report by an approval or a construction
start (`corrections.reviewer_first_report`, "Corrections" above), which three entries did: round 6
published Wichita Falls' approval of 2026-08, Project Cardinal's of 2025-11-10 and the Elk Grove
Village building's construction start of 2024 as their first reports. Each entry now ends with a
cited `first_report` Fix (`from` null, `basis` out_of_date, reviewed 2026-10-13, every page read
with the project's user agent on 2026-10-13, 12:10–12:17 UTC): the earliest public report found
that names the project, before the event. Dallas Innovates reported DataNovaX's Pioneer Park on
Airport Drive on 2024-06-05 (KAUZ calls the building part of it); Pioneer Development's application
package for Project Cardinal is dated 2025-03-14 (local news reported it on 2025-03-24); and
Journal & Topics reported on 2023-06-09 that Aligned had submitted plans for its campus on
Northwest Point Boulevard (its May 2023 report of Aligned's purchases states no plan; The Real
Deal's and DCD's 2023 pages refuse the project's user agent, and no Wayback copy of them could be
read).

The importer rules of fix round 6 make eight of the owner's corrections without an entry (an
entry would change nothing, and a join, alone or name that is true already stops the run): the
process server and QES Computers (the strong-word gate), Lumen in St. George (a house), Encompass's
teleport (a telecom site), PowerHouse CTP-02 (under construction), way 45583037 apart from Equinix
DC14 (rule 7), and the names "Aligned IAD-01 and IAD-02" and "Digital Realty Silicon Valley SJC31
and SJC37". Not entered: way/1019812125 (EdgeConneX POR03 at 6501 NE Bennett Street, under
construction per the correction: the only page that says so, datacenterHawk's, refuses the
project's user agent and has no Wayback copy; EdgeConneX's own page calls POR03 planned and
Baxtel lists it as fully operational with expansion in progress, so it stays as tagged).

The re-check of the corrected records (reviewed 2026-10-13, PR #2) adds one element decision:
way/1543931283, Equinix DC17 at 44710 Performance Circle, `status` announced with `expect` name
"Equinix DC17", citing Baxtel's DC17 page ("Year Planned: 2027", read with the project's user
agent on 2026-10-13, 15:50 UTC). The way's bare `building=data_center` and `telecom=data_center`
read as operating, the defect DC18's entry corrected. Like DC18, DC17 is now a building of the
Equinix Ashburn record's announced phase, and the operating phase and its observation no longer
cite its way. The campus rename's `from` ("Equinix DC1, DC2, DC4 and 10 more") still holds,
since the campus name counts the named buildings whatever their phase. Equinix's Washington,
D.C. metro page refuses the project's user agent, and the Wayback Machine could not be reached,
so it is not cited.

The review of the integrated Phase 2 build (reviewed 2026-10-16) adds one element decision:
node/8641856300 in Macomb County, MI, `drop`, with `expect` name "Chick-fil-A Hiring Center" and
telecom data_center. The node is a fast-food chain's hiring center whose bare
`telecom=data_center` made it an operating data center in release 20261013-1702, and its
operator tag is a person's name, which that release published as the record's operator and in
its name. The entry cites the node's own tags, read through the OSM API with the project's user
agent on 2026-10-16, 16:24 UTC (the website challenged automated clients; the node is at
version 1, its tags as in the seed input), and quotes the name tag only. The record keeps its id
and is out of scope, so no later release publishes it; a dropped element replaces nothing, so
the stored record still holds the operator tag as OSM gives it (section 10, "Names").

The same review's scan of the published records found nine more elements, each named for a business
that does not run data centers, whose only data center signal is a bare `telecom=data_center`
(PNNL's rows, for the eight it has, repeat OSM's names and footprints, section 6). Each was checked
against the company's own pages, PeeringDB, Baxtel and the county's or city's records, every page
read with the project's user agent on 2026-10-16. Two are entries (reviewed 2026-10-16), `drop`
with `expect` on the tags they read:

| Element | Decision | Source |
|---|---|---|
| way/828228235 (StrongMind, Chandler, AZ) | dropped: StrongMind's head office; the Maricopa County Assessor's parcel under the way is 2501 N Arizona Ave, the address StrongMind's contact page gives | City of Chandler, software development employers |
| way/958728802 (E-Foam, Winter Garden, FL) | dropped: the building of E-Foam, LLC, which makes architectural decorative foam and gives the way's address as its own (`expect` also on the house number) | E-Foam, LLC |

Five stay as tagged, because a source shows a data center at the site: way/223783259 at 3101
Western Avenue, Seattle, named for a biopharma tenant, is PeeringDB's Digital Fortress (SEA) at
that address; way/451413811 at 150 South 1st Street, San Jose, the former Pavilion mall, is the
data center Digital Realty sold to Hines in 2021 (Rackspace SJC2, PeeringDB's DataPipe Silicon
Valley One; Baxtel calls it fully operational); way/195165374 at 7901 NW 112th Street, Kansas City,
named for a financial-management firm, is Liberty Mutual's building in the city's energy
benchmarking, with a site energy use of about 400 kBtu per square foot a year, several times an
office's, and Site Selection reported Liberty Mutual's 100,000 sq ft Kansas City data center in
2003; way/553884511 is the Carfax Data Center in Columbia, MO (Digital Dealer); and way/1227550179
is Baxtel's Novartis data center at 6549 South Freeway, Fort Worth. The Seattle and Kansas City
names are an occupant's, not the data center's, for a reviewer to correct with a cited `name`
entry. Not entered: way/837139011 ("ForceAmp.com", Coralville, IA) and way/1528887658 (Landmax Data
Systems, Rush, NY) name a software developer and a property-data company, but no page read places
the company in the building or says what the building is used for (a building of a commercial
condominium; a one-story small structure on New York's assessment roll), so they stay as tagged
until a reviewer decides.

## 10. Known limits

- **Bounding-box containment in saved inputs.** A response saved before 2026-10-09's second
  check (the seed inputs) has no campus outlines and no containers, so a campus is its bounding
  box there: a diagonal or L-shaped campus covers neighbours. The operator guard stops
  cross-operator merges, and an area without an operator holds only the objects at its address
  or of its contents' main operator, but an unrelated object without an operator inside the box
  still joins. Containment across a county line needs an outline.
- **Single-linkage chaining.** Same-operator buildings join transitively, so a long row of one
  operator's buildings becomes one campus (about 1.6 km at most on 2026-10-07). Unnamed buildings
  without an operator join only within 50 m (large halls within 100 m), so a large unnamed site
  that OSM draws no site polygon for, or whose polygon the query does not find, still splits; a
  development of three or more such parts publishes its largest part and holds the others
  (section 5). One operator's numbered siblings and buildings on one street join up to 500 m apart
  (rule 8) when nothing separates them; further apart, or across another operator's building, a
  site polygon each or two ZIP codes, they stay two records with a review item. QTS's
  Fayetteville campus keeps two records: its third building pair is 1,019 m from the others,
  beyond every rule. Rule 8 can join two campuses of one operator that happen to sit on one
  street or carry consecutive numbers within 500 m with nothing between them; AWS names every
  building IAD-n, so an AWS series within 500 m (IAD-140 to IAD-145 at Oak Grove) is read as one
  campus.
- **Holding is not merging.** A held cluster's buildings are in no record until a reviewer
  decides, so the record that stays describes part of the site (Abilene: 10 of 40 halls on the
  seed inputs). A reviewer acts through config/overrides/osm.json (section 9) or the data PR.
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
  2026-10-08 seed input (28 with the site polygons, 15 in scope on the 2026-10-09 input after fix
  round 5). A campus's common name can be a former brand rather than the site ("NTT RagingWire"
  for RagingWire CA1 and CA2), and a series names its buildings, not the campus ("Equinix DC1,
  DC2, DC4 and 5 more"). Operator tags are copied as written; a few
  name a person or a non-data-center business, which the seed review should catch: release
  20261013-1702 published a fast-food chain's hiring center whose operator tag is a person's name
  (node/8641856300, dropped since, section 9). No OSM rule reads an operator tag as a person (the
  AI GridWatch importer drops persons from its parties), and a dropped element's out-of-scope record still holds the
  tag in `data/records/`, unpublished. The place is
  the Census place that contains the point, else the county, never the postal city; a point within
  about 100 m of a place's line names the county.
- **Doubly mapped buildings.** An unnamed way drawn on another building's footprint is held
  (section 5), or, inside the other's record, left out of its `buildings[]`; a named one, or one
  with its own operator, stays a record, and a node inside
  another operator's building is a `possible_duplicate` item: Flexential in the Equinix Infomart
  is a tenant of Equinix's building, so no rule picks a record. CoreSite LA2 joins the USPO
  Terminal Annex, whose operator tag is the post office's (section 3).
- **OSM's own errors.** An operator tag that outlived a sale, a tenant's name on a building it
  left, or a data center tag on a clinic is what OSM says; the scope screen and the rules catch
  some, and a reviewer corrects the rest with a cited entry (section 9). Twelve entries on
  2026-10-09, 38 more (and two of them replaced) in fix round 6, one in the re-check of
  2026-10-13 and three in the review of 2026-10-16 (a hiring center, a foam maker's building and
  a company's head office, each tagged only `telecom=data_center`); the seed check's other stale
  tags (SEGRA, Charlotte) have no source that states the present, so they stay as tagged. Since
  fix round 6 an entry can also give a status or correct the record's events, so the pre-check's
  stale statuses (Microsoft East Point, EdgeCore AS02, Crane PDX01, Prime SJC03, Lambda's Kansas
  City building, EdgeConneX POR03 and others) are the reviewers' to enter with a cited source;
  until then they stay as tagged.
- **A reviewer's join is not checked.** A `join` bypasses the operator guard and the county
  regions, so a wrong one can merge two operators' buildings; `atlas validate` catches only a
  record whose point lies outside its county, not a wrong campus.
- **Cross-source duplicates.** OSM and Epoch or AI GridWatch can describe the same site; entity
  resolution is M4 (owner decision of 2026-10-08).

## 11. Live run, 2026-10-07

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
  (section 4) now keep clusters in one county, and the seed re-run has none (section 12).

These counts were measured on the saved 22:39:49Z response with the code after the 2026-10-08
review fixes and their integration (the 1:500,000 county file for county FIPS, lifecycle-tagged
polygons as buildings); the first run (22:42 UTC) gave the figures before them, quoted in
section 4.

The seed records are not committed by this change; the bulk seed pull request runs the import after
integration (07 §6.7).

## 12. Re-run of the 2026-10-08 seed input

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

## 13. Re-run of the 2026-10-09 seed input

The second seed check (1,298 records, 103 sampled) found an error in 17 sampled records and 75
flagged ones; 54 were OSM records, most of them one campus published as several records. The
importer was re-run offline on the saved input of that import (`--input
overpass-us-20261009T095131Z.json --pnnl im3_datacenter_centroids.geojson --now
2026-10-09T09:50:24.684005Z --offline`, with the committed overrides) into a scratch store. That
response predates the campus outlines and the containers of section 1, so the figures are what the
dissolve rules, the holds and the overrides do without them:

| | Seed (before) | Re-run |
|---|---|---|
| Objects / clusters | 2,046 / 1,075 | 2,046 / 1,037 |
| Records (in scope) | 1,034 (955) | 985 (902) |
| Held, no record | 41 | 52 |
| Operating / under construction / announced | 996 / 33 / 5 | 957 / 23 / 5 |
| `site.acreage` / records with an operator / with an owner | 34 / 663 / 36 | 31 / 648 / 28 |
| Streets with a ";" list | 8 | 0 |
| Review items (osm / pnnl) | 176 / 21 | 180 / 27 |
| PNNL matched | 1,372 (99.28%) | 1,372 (99.28%) |

25 groups of former records joined (one site, one record: Compass Red Oak's nine, Stream San
Antonio III's three, the KOMO Plaza, PowerHouse Pacific, EAT, QTS Phoenix, Google Council Bluffs
and Microsoft Texas Research Park campuses, CoreSite LA2 with its building, Digital Realty IAD51
with its construction polygon, among others); 11 clusters were held after building (the Microsoft
Quail Ridge Lane polygon, STACK NVA04 and the AWS-tagged buildings at its addresses, IAD53 and
xAI's Southaven building across a county line, six Abilene hall groups); 4 records went out of
scope (Compute North, Duke's lab, Myndshift Technologies, Clarkson's Old Main); 5 records took a
cited override. `atlas validate` passes for all 985. On the 2026-10-09 response the same-name
groups left are AWS's separately addressed Morrow County and Hermiston groups and three pairs or
triples of unnamed buildings that the check found to be different sites.

A probe of the new query on two small areas (2026-10-09, maps.mail.ru): around Abilene it returns
the 40 halls and the Lancium Clean Campus polygon, which the importer makes one record "Lancium
Clean Campus (Taylor County, TX)"; around Ashburn (38.90-39.10 N, 77.38-77.60 W) it returns 360
elements, among them seven containers that hold one operator's buildings each (Vantage Ashburn I
and III, PowerHouse Pacific, two AWS sites, CloudHQ LC10 and BlackChamber Dulles 28).


## 14. Fix round 5: re-run of the 2026-10-09 seed input

The pre-check of the fourth seed import (1,239 records, 985 of them OSM's; a 98-record sample
with 11 errors under the cited standard and 28 under today's) found OSM records that split one
campus (an expansion site, a sibling or a same-street building just beyond 300 m, an unnamed
building in a row), a campus named after one of its buildings, a building listed twice, a
genealogy room published as a data center, a PNNL row naming another building number, and every
record citing only its representative. The importer was re-run offline on the same saved input
(`--input overpass-us-20261009T095131Z.json --pnnl im3_datacenter_centroids.geojson --now
2026-10-09T09:50:24.684005Z --offline`, the committed overrides) into an empty scratch store:

| | Seed 4 (before) | Re-run |
|---|---|---|
| Objects / clusters | 2,046 / 1,037 | 2,046 / 1,021 |
| Records (in scope) | 985 (902) | 971 (887) |
| Held, no record | 52 | 50 |
| Operating / under construction / announced | 957 / 23 / 5 | 943 / 23 / 5 |
| Records with an operator / an owner / an acreage | 648 / 28 / 31 | 642 / 29 / 31 |
| Records with a value its cited element does not state | 326 | 0 |
| Records named after one of several buildings, renamed | | 97 |
| In-scope names shared by more than one record | 17 | 15 |
| Review items (osm / pnnl) | 180 / 27 | 209 / 30 |
| `possible_duplicate` items (osm) | 18 | 46: 15 one-operator splits, 10 lone buildings beside a row, 3 buildings drawn twice in a record |
| PNNL matched | 1,372 (99.28%) | 1,372 (99.28%) |
| Overrides applied | 5 | 12 |

16 clusters gained members: Meta New Albany with its LCO 3 site, Google Lenoir's two outers,
Apple Waukee with its proposed site, Microsoft Fairwater with Phase 2 (rule 2 between areas); Iron
Mountain VA-6, Digital Realty IAD41, CoreSite VA3, Digital Realty SJC11, CloudHQ MCC2, Microsoft
Leesburg Buildings 3 to 5 and AWS IAD-140 to IAD-145 (rule 8); and the unnamed buildings at
Digital Dallas, Evoque Allen DA1, Digital Realty EWR11 and EWR12, Amazon IAD-73 and IAD-74 and
Equinix DC14 (rule 7). `atlas validate` passes for all 971.

## 15. Fix round 6: re-run of the 2026-10-09 seed input

The review of the fifth seed import (1,211 records, a 90-record sample with 11 errors under the
cited standard, and 45 cited errors in all) found OSM records that were not data centers (a
process server's point, a computer shop, a house, a satellite teleport), a construction site read
as operating, a building outside a campus polygon listed as the campus's, a carrier hotel's
building counted beside its tenant's record, one campus as two records twice (a landlord's
buildings leased to a tenant, CloudHQ's LC campus), names taken from one building, and an
override that gave a building the name of its neighbour. Each importer rule was measured alone,
then all together, on the same saved input with the committed overrides, into an empty scratch
store (`--input overpass-us-20261009T095131Z.json --pnnl im3_datacenter_centroids.geojson --now
2026-10-09T09:50:24.684005Z --offline`); the rules (OSM importer version 5) are in sections 4, 5
and 9:

| | Fix round 5 | Fix round 6 |
|---|---|---|
| Objects / clusters | 2,046 / 1,021 | 2,046 / 1,020 |
| Records (in scope) | 971 (887) | 969 (878) |
| Held, no record | 50 | 51 |
| Operating / under construction / announced | 943 / 23 / 5 | 940 / 24 / 5 |
| Out of scope | 84 | 91 |
| Review items (osm / pnnl) | 211 / 30 | 218 / 30 |

Changed: 7 records held out of scope (All About Serving Process Server, QES Computers, Alembic
Computer Services and LSU's Center for Computation & Technology by the strong-word gate; Lumen in
St. George, a house; Encompass's teleport and SES's Hawley Earth Station, telecom sites);
PowerHouse CTP-02 under construction with a planned energized event in 2027; way 45583037 split
from Equinix DC14; Lakeside Technology Center held; Digital Loudoun Plaza with Amazon IAD-6 and
IAD-13, and CloudHQ's LC campus, one record each; "Aligned IAD-01 and IAD-02" and "Digital Realty
Silicon Valley SJC31 and SJC37" renamed. No record of the sample that the review judged right
changes. The NTT VA8 entry's name is a `conflict` item until the owner's entry for way 1560827941
lands (section 9: fix round 6 names it NTT VA9). A second run on the same store leaves every
candidate unchanged, and `atlas validate` passes.

The cited-correction keys of section 9 (`join`, `alone`, `status`, `expect` and `corrections`)
change nothing while no entry uses them: the same run with the committed overrides gives
byte-identical records and review items, and the receipt gains the corrections counts, all 0.

With fix round 6's entries (section 9), the same input with the run's own time (`--now
2026-10-13T04:09:42Z`) gives 1,014 clusters, 964 records (870 in scope: 933 operating, 24 under
construction, 5 announced, 2 permitted), 50 clusters held, 94 records out of scope and 212 / 31
review items. The receipt counts 20 corrected records, 29 record ops, 2 out of scope and 2
`importer_gap` Fixes: the tenants Amazon Web Services and Rackspace, which the importer does not
read from a joined building's operator tag. A scratch checker finds each record by its OSM ref and
confirms all 48 of the owner's OSM corrections (47 made, by an entry or a rule, and EdgeConneX
POR03 left as tagged), `atlas validate` passes, and a second run on the same store leaves every
candidate unchanged.
