# Epoch AI and AI GridWatch: the pipeline seeds

Two CC BY 4.0 datasets seed the pipeline (07 §4.1, §4.3): Epoch AI's frontier data centers, with
dated construction timelines, and AI GridWatch's tracker of contested proposals, with hearing and
decision dates. Both importers are thin mappings onto the shared framework
(`atlas/sources/base.py`) and the status crosswalk (`atlas/crosswalk.py`,
[status-crosswalk.md](../status-crosswalk.md)). Locations go through `atlas/geocode.py` (07 §6.5).

```sh
uv run atlas import epoch                       # fetch, geocode (Census Geocoder) and merge
uv run atlas import epoch --input data_centers.zip --no-geocode --offline
uv run atlas import aigridwatch                 # fetch and merge; run it after epoch
uv run atlas import aigridwatch --input projects.json --offline
```

Run `epoch` before `aigridwatch`: the AI GridWatch importer skips the rows that are Epoch's sites
by looking at the Epoch records already in the store (see
[Epoch AI's sites in AI GridWatch](#epoch-ais-sites-in-ai-gridwatch)). On a store without Epoch
records it refuses to run (exit 1, nothing written) unless `--without-epoch` is given; otherwise a
first seed in the wrong order would import every Epoch site AI GridWatch republishes, and those
records would stay.

Both write `data/records/{id}.json`, `review/queue/{epoch,aigridwatch}.jsonl` and
`data/imports/{epoch,aigridwatch}.json`. A second run with the same inputs rewrites no record. The
receipt's metrics count the rows read, the records by location method, the planned and stage
events, the review items, and for Epoch the Census requests and cache hits and the overrides used
and unused (an unused entry names a site Epoch no longer lists).

## Epoch AI (`atlas/sources/epoch.py`)

| | |
|---|---|
| Input | <https://epoch.ai/data/data_centers/data_centers.zip> (robots.txt allows `/data/`), opened with `atlas.safezip`. Only `README.md`, `data_centers.csv` and `data_center_timelines.csv` are read. |
| License guard | The run fails unless `README.md` links the CC BY 4.0 deed (`creativecommons.org/licenses/by/4.0`) and names no NonCommercial, NoDerivatives or ShareAlike term (`BY-NC`, `BY-ND`, `BY-SA`, "non-commercial", "NoDerivatives", "ShareAlike"). Every Creative Commons license name starts "Creative Commons Attribution", so the name alone proves nothing. The run also fails if a required file or column is missing. |
| Receipt | `InputSnapshot` license `CC-BY-4.0`, `upstream_version` = the ETag (none with `--input`). |
| Rows | `Country == "United States"`. Matched on `external_ids.epoch_name` = the Epoch `Name` (whitespace-normalized). |
| Arguments | `--overrides PATH` (default `config/overrides/epoch.json`), `--no-geocode` (no Census requests: overrides, Gazetteer places and county names only). |

### Mapping

| Record field | From |
|---|---|
| `record_type` | `campus` once a timeline row dated today or earlier has Buildings operational > 0 (or IT power > 0 when that cell is blank, as in the crosswalk), else `project` |
| `canonical_name` | `{Name} ({city or county}, {ST})`; the county is written in full ("Madison County", "Richland Parish") |
| `parties.owner` / `parties.tenant` | `Owner` / `Users`, split on commas. Names tagged `#confident` or `#likely` (and untagged names) are kept, `#speculative` and `#unlikely` are dropped, tags are stripped. Epoch's `Owner` is the company that owns the hardware (Oracle at Stargate Abilene, where Crusoe builds and runs the campus), so it is not mapped to `operator`; the M1 spec said `operator`. |
| `aliases` | `Project`, same tag rule, `kind: "codename"` |
| `capacity.it_mw`, `capacity.facility_mw` | IT power (MW) and Power (MW) of the latest timeline row dated today or earlier, when > 0 |
| `cooling.water_use_mgd` | Water use (MGD) of the same row, when > 0 (Epoch writes 0.0 for "not estimated") |
| `money` | Current total capital cost × 10⁹, `investment_basis: "estimate"`, `currency_year: 2025` |
| `purpose`, `evidence_level` | `unknown`, `reported` |
| `status_history` | the timeline (below) |
| `field_meta` | `/capacity/*`, `/cooling/water_use_mgd`, `/money/investment_usd`: confidence 0.70, `imported`, `["s1"]`. `/location`: `derived` with the geocoder's confidence, or `stated` for an override: 0.85 (07 §3.4, stated with a verbatim quote) plus the source adjustment (government record or regulator +0.10, utility, ISO or SEC filing +0.07, company release +0.05, press 0). |
| `sources` | `s1` = the dataset ("Epoch AI", "AI data centers", `open_dataset`, `CC-BY-4.0`) supporting `/canonical_name`, `/aliases`, `/parties`, `/location`, `/capacity`, `/cooling`, `/money`, `/status_history`. An override adds its source supporting `/location` (and `s1` then drops `/location`), with the override's `quote` (`quote_match: "human"`: copied by hand when the entry was written, not matched by the pipeline), `retrieved_at` and `archive_url`. Up to five `Selected Sources` links follow with empty `supports`: `sec_filing` for sec.gov, `government_record` for other `.gov` and `.us` hosts, else `news`; title = the link text. |

### Status timeline

Rows are sorted by date and mapped with `from_epoch_row(Construction status, Buildings
operational)`: operating when any building is operational, announced when the text is only about
plans, under construction otherwise. An event is kept only where the status changes, with
`seq` in date order, `as_of` at day precision, `source_ids: ["s1"]` and the construction-status text
as the note (markdown links reduced to their text, at most 200 characters, dropped if it looks like
it holds contact details).

Rows dated after today are Epoch's projections (95 of 547 on 2026-10-07, up to 2030-01-01). They
become `planned: true` events, which never set the status or the derived dates (07 §2.3). A site
with no row dated today or earlier has no current status and becomes an `unknown_status` review
item.

### Location

1. A cited entry in `config/overrides/epoch.json` (see the policy below).
2. Otherwise `parse_address(Address)` and `geocode()`: Census Geocoder, then the Gazetteer place
   from a "City, ST" part, then the county by name ("…, Mississippi Madison County"). An address
   that names no state ("13360 Miller Rd NW", "2950 S. Litchfield Road") is not geocoded at all:
   a Census match could come from any state.
3. No address and no override: `missing_location`. An address the chain cannot place, or one
   without a state: `geocode_failed`. Neither becomes a record.

## AI GridWatch (`atlas/sources/aigridwatch.py`)

| | |
|---|---|
| Input | <https://aigridwatch.com/data/projects.json> (robots.txt allows all). |
| License guard | The run fails unless `license == "CC BY 4.0"` and `projects` is a list. |
| Receipt | `InputSnapshot` license `CC-BY-4.0`, `upstream_version` = `generated`. |
| Rows | Rows with `verified: false` are leads, not facts (the dataset says so), and become `unverified_upstream` items. Matched on `external_ids.aigridwatch_id` = the row `id`. |
| Arguments | `--without-epoch`: run on a store that holds no Epoch record (only rows whose `source` is an Epoch AI page are held as Epoch's sites). Without it such a run fails. |

### Mapping

| Record field | From |
|---|---|
| `record_type` | `project` |
| `canonical_name` | `{name} ({city, municipality or county}, {ST})` |
| `location` | `lat`/`lon` as given, `precision: "locality"`, `geocode_method: "source_coords"`. The point must lie in the state (`CountyIndex.in_state`), else `county_mismatch` and no record. Rows without coordinates (52 on 2026-10-07) go through `geocode()` without the Census step: a Gazetteer place from the locality (or from a non-county parenthesis, "Decatur Township (Indianapolis)"), else the named county's centroid; else `geocode_failed`. A Gazetteer place outside the named county is not used ("Storey County (near Reno)": Reno is in Washoe County), so such a row gets the county's centroid. |
| county | From the locality: the text in "(X County)" or "(X Parish)", or a locality that is itself a county ("Caddo Parish"; boroughs and census areas only in Alaska, since Pennsylvania boroughs are towns). With coordinates, the named county must contain the point (several: the one that does). When none does, the record keeps the point, names no county and gets a `county_mismatch` item (EdgeCore Louisa's point is in Goochland County). |
| `city` / `municipality` | The locality outside the parentheses when it is not a county: townships, boroughs and villages go to `municipality`, other places to `city` ("City of" dropped). A parenthesis that only says the site is near a place ("near Reno", "Granbury area", "north of …") may give the point, but never the city. |
| `parties` | `operator` and `owner` split on " / ", `tenant` on " / " and ", ", `filing_entities` from `filing_llc` (split on " / " and ";", prose dropped). Records hold organizations only (07 §5.3), so every name goes through one filter: a party AI GridWatch annotates with a personal role and no organization marker ("A Person (developer)") is dropped; a trailing parenthesis is a note, not part of the name, and is removed ("Amazon (AWS)", "(parcels)", "(proposed site)", and a person's name in "Example Ventures (A Person)"); a capacity typed into a party field ("67 MW") becomes a `unit_parse` item. Persons left out are counted in `persons_dropped`. |
| `aliases` | Each filing entity (after the same filter), `kind: "filing_llc"` |
| `capacity.it_mw` | `size_mw` (the schema: "Planned IT/critical load in MW"), when in (0, 10000]; otherwise a `unit_parse` item |
| `site.acreage` | `acres`, when in (0, 100000]; otherwise `unit_parse` |
| `evidence_level` | `rumor` for the Rumored stage, else `reported` |
| `status_reason` | from the crosswalk (`moratorium`, `local_denial`, `developer_withdrawal`) |
| `sources` | `s1` = the tracker ("AI GridWatch", `open_dataset`, `CC-BY-4.0`) supporting `/canonical_name`, `/aliases`, `/parties`, `/location`, `/capacity`, `/site`, `/status_history`. `s2` = the row's `source` URL: publisher = host; `government_record` for `.gov`/`.us` hosts and Legistar, Granicus, CivicPlus and Municode hosts, else `news`; empty `supports`. |
| `field_meta` | `/capacity/it_mw`, `/site/acreage`: 0.70 `imported`. `/location`: 0.70 `imported` (source coordinates) or 0.60 `derived` (geocoded). |

### Status history

Events come from the milestone dates (all `source_ids: ["s1"]`, day precision; a date after today
is a planned event):

| Milestone | Event | Status |
|---|---|---|
| `announced` | `announced` | announced |
| `rezoning_filed` | `application_filed` | proposed |
| `hearing_date` on or before today | `hearing_held` | proposed |
| `hearing_date` after today | `hearing_scheduled` (planned) | proposed |
| `decided_date` + outcome `approved` / `denied` / `withdrawn` / `moratorium` | `approved` / `denied` / `withdrawn` / `paused` | permitted / denied / cancelled / paused |

The stage then goes through `from_aigridwatch_stage(stage, has_filing)`, where `has_filing` is a
`rezoning_filed` date or an event of kind `filing` or `rezoning`; so "Proposed" means announced
without a filing and proposed with one. AI GridWatch derives the stage from data the milestones do
not always carry. When the milestones have no non-planned event, or roll up to a different status,
an `other` event with the stage's status is appended, noted "AI GridWatch stage '{stage}' as of
{as_of}". It is dated with the row's `as_of` (else `generated`), but never earlier than the latest
milestone, so the record's status always equals the stage's. On 2026-10-08, 117 of 202 records
needed one.

An `announced` date later than a filing, hearing or decision date would move the record backward
(announced after proposed, which 07 §2.3 flags). That milestone is left out and the row gets a
`conflict` item naming both dates (2 rows on 2026-10-08, announcements dated 5 days and almost 3
months after the rezoning filing).

## Geocoding (`atlas/geocode.py`)

`geocode(GeocodeRequest, census=..., gazetteer=..., counties=...)` stops at the first step that
works:

| Step | Precision | `geocode_method` | County | Confidence |
|---|---|---|---|---|
| 1. Source coordinates | as the source states | `source_coords` | point-in-polygon for address or better, else the stated county name | 0.70 |
| 2. Census Geocoder (only for a request with a state) | `address` when the match agrees and its house number equals the input's, `street` when it agrees with another number | `census_geocoder` | the response GEOID when the point lies in it, else point-in-polygon | 0.90 / 0.60 |
| 3. Gazetteer place | `locality`, unless a stated county does not contain the place | `gazetteer` | the stated county name, if any | 0.60 |
| 4. County by name | `county` (the polygon's point on surface) | `county_centroid` | that county | 0.60 |

The confidences follow 07 §3.4: derived in code, 0.90 from an address, otherwise 0.60.

A Census match counts only when it agrees with the request; otherwise the chain falls through to
the Gazetteer, one precision level down:

- its state is the request's;
- its ZIP is the input's, or its city is (when the parser found no city, the city's words appear in
  the address text);
- its street is the input's street, compared word by word after spelling-out and abbreviations
  are made one form (Road and RD, County Road and CO RD, Highway, Route and State Route all HWY;
  US Hwy stays apart). The input may leave out a directional or the street type ("500 8th St" is
  500 E 8TH ST), but a directional, type or qualifier the input states must be the match's own:
  "2950 S. Litchfield Road" is not 2950 LITCHFIELD RD BYP, "Co Rd 42" is not 42 COUNTY CT, and
  Larrison Blvd is not LARRISON DR. A numbered route may carry a trailing directional the Census
  leaves out ("1435 Hwy 54 W" is 1435 STATE RTE 54);
- when more than one match agrees and two lie more than 200 m apart, the address is ambiguous and
  none counts ("145th St" in Rosemount matches 145TH ST E and 145TH ST W, 4.6 km apart).

Every result is checked like `atlas validate` rule 5 (county-checked precisions inside the county
with the 0.003° tolerance, locality inside the state), and a locality point lies in the county the
result names, so a geocoded record always passes validation and never names a county its point is
outside of.

- **Census Geocoder:** `GET https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress`
  with `benchmark=Public_AR_Current&vintage=Current_Current&format=json&layers=Counties`, through
  `atlas.net.fetch` (`robots=False`, one request per second, retries on 429/5xx). Responses are
  cached by the SHA-256 of the address in `.cache/atlas/census/`, so re-runs and `--offline` runs
  with a warm cache send nothing. Addresses over 100 characters are not sent (the service answers
  400). Only a 400 answer counts as no match (it is not cached); a 429 after the retries, a 408,
  any other 4xx and a 5xx fail the run, so a rate-limited request never moves a record down to
  the Gazetteer for one run and back on the next.
- **Gazetteer:** `reference/census/2025_Gaz_place_national.zip` (32,350 places) and
  `2025_Gaz_counties_national.zip` (3,222 counties), checked against their SHA-256 on load. Place
  names match case- and accent-insensitively with the LSAD descriptor stripped ("Abbeville city",
  "Indianapolis city (balance)"), and St/Mt/Ft spelled out. A name shared by an incorporated place
  and a CDP means the incorporated place; any other duplicate is ambiguous and does not match.

## Overrides policy (`config/overrides/epoch.json`)

```json
{"Epoch Name": {"state_abbr": "LA", "county_fips": "22083", "city": null, "municipality": null,
                "lat": null, "lon": null, "precision": "county",
                "source_url": "https://…", "archive_url": "optional: the snapshot that was read",
                "quote": "the sentence that states the location, verbatim, at most 300 characters",
                "retrieved_at": "2026-10-08T05:15:06Z", "note": "why this source and precision",
                "publisher": "Meta", "source_type": "company_release"}}
```

- Each entry cites a `source_url`: one of Epoch's own Selected Sources for the site first, a
  primary source (company, government, regulator) before press. Never from memory.
- `quote` is copied verbatim from the page when it was read (`retrieved_at`), and states the
  place. When the live page refuses automated clients, the Wayback Machine snapshot that was read
  goes in `archive_url`. `tests/sources/test_epoch.py` checks that every entry has its citation
  fields and that the places it names appear in the quote.
- An entry places the site no more precisely than the quote states: the municipality (`city`,
  from the Gazetteer) or the county, unless the source gives a street address. A township is not
  a Census place, so it goes in `municipality` with the county's point. The quote itself names
  every place the entry gives (a reader of the record's source sees only the quote), and a
  dateline ("Kansas City, MO — March 20, 2024 —") does not count: it says where a release was
  issued. `test_every_committed_override_is_cited` checks both; Pryor Creek, the Census name of
  Pryor, is the one name the test maps.
- The entry becomes its own source supporting `/location`. `county` precision without coordinates
  uses the county's point on surface, `locality` uses the Gazetteer place for `city`, and
  coordinates (`manual`) are checked against the county or state. When an entry names a county,
  the point (the Gazetteer place's included) must lie in it at every precision, so a record never
  names a county its point is outside of. A bad entry fails the run.
- Committed (owner decision of 2026-10-08: the sites the seed cannot place get cited overrides),
  13 entries, each read on 2026-10-08 (Google Kansas City East, OpenAI Stargate Michigan and AWS
  New Albany re-read the same day for a quote that states the place):

| Site | Placed at | Source |
|---|---|---|
| Meta Hyperion | Richland Parish, LA (county) | Meta's data center page |
| Google Storey County | Storey County, NV (county) | Google's location page |
| Google Kansas City East | Kansas City, MO (locality) | DCD, through the Wayback Machine (Hunt Midwest's release has only a dateline) |
| Google Mesa | Mesa, AZ (locality) | DCD, through the Wayback Machine |
| Anthropic Barber Lake | Colorado City, TX (locality) | Cipher Mining's release |
| OpenAI Stargate Michigan | Washtenaw County, MI (county) | Michigan Public Service Commission, through the Wayback Machine (the township is in another sentence) |
| OpenAI Stargate Milam | Milam County, TX (county) | SB Energy's release |
| OpenAI Stargate New Mexico | Doña Ana County, NM (county) | Oracle's release |
| OpenAI Stargate Wisconsin | Port Washington, WI (locality) | Vantage's campus page |
| AWS New Albany | New Albany, OH (locality) | WOSU on the City Council vote (the City's project list does not name the city) |
| Google Pryor (North) | Pryor Creek, OK (locality; the Census name of Pryor) | Google's post |
| Meta Huntsville | Huntsville, AL (locality) | the site contractor's project page |
| Stream Phoenix | Goodyear, AZ (locality) | Stream's case study, through the Wayback Machine |

  Eight have no address in Epoch; Meta Hyperion, Google Pryor (North) and Meta Huntsville have an
  address the chain cannot place; AWS New Albany and Stream Phoenix have an address without a
  state.

## Review items

| Kind | Importer | When |
|---|---|---|
| `missing_location` | epoch | no address and no override |
| `geocode_failed` | both | the chain found no location; Epoch: also an address that names no state |
| `unknown_status` | both | Epoch: no timeline row dated today or earlier; AGW: a stage the crosswalk does not know |
| `unverified_upstream` | aigridwatch | `verified: false` |
| `possible_duplicate` | aigridwatch | the row is an Epoch site (`record_id` = the Epoch record, `data.matched_by`), or may be one (`nearby`, `several`: `record_id` empty, the candidates in `data.epoch_records`); no record. `data.stored_record` names an AI GridWatch record the store already holds for the row |
| `county_mismatch` | aigridwatch | the coordinates are not in the stated state (no record), or not in the named county (the record keeps the point and names no county) |
| `conflict` | aigridwatch | the announced date is later than a filing, hearing or decision date (the announcement is left out) |
| `unit_parse` | aigridwatch | `size_mw` or `acres` is not a number in range, or a party field holds a capacity (the record is kept without it) |
| `out_of_scope` | aigridwatch | a territory |
| `invalid` | both | a row that does not map to a valid record |

`apply_import` adds `conflict` (a candidate matches more than one stored record),
`held_human_reviewed`, `held_merged`, `removed_upstream` and the validation `invalid` items.

## Attribution

Every record cites its dataset in `sources[]` with the license; [ATTRIBUTION.md](../../ATTRIBUTION.md)
carries the full credits:

- Epoch AI, "AI data centers". Published online at epoch.ai. Retrieved from
  <https://epoch.ai/data/ai-data-centers>. CC BY 4.0.
- AI GridWatch (<https://aigridwatch.com>), AI GridWatch data center project tracker. CC BY 4.0.
- U.S. Census Bureau: Geocoder, 2025 Gazetteer Files, 2025 cartographic boundary files. Public
  domain.

The test fixtures (`tests/fixtures/epoch/`, `aigridwatch/`, `census/`) are small subsets with their
own attribution READMEs.

## Epoch AI's sites in AI GridWatch

AI GridWatch republishes most of Epoch's US sites: on 2026-10-08, 69 of its 284 rows carry an
Epoch site's exact name (56 of them say "Epoch AI estimates ~… MW"), and 68 of those set `source` to
the first of Epoch's Selected Sources for the site. Imported twice, these sites counted twice on the
map and in the summary (about 10 GW from Epoch plus 16 GW on the AI GridWatch twins, often with
conflicting statuses: 29 of 61 pairs on 2026-10-07). 07 §2.1 counts each facility once.

The AI GridWatch importer therefore makes no record for a row that is the same site as an Epoch
record already in the store, and files a `possible_duplicate` item with that record's id
(deterministic links only, 07 §6.6 step 1). Rules read from the row before it is mapped:

- `name`: the row's name is the Epoch record's `epoch_name` (case and spacing aside), in the same
  state;
- `id`: the row's id is the Epoch name in AI GridWatch's id form (accents dropped, lower case,
  other characters as "-"), alone or followed by "-{state}", in the same state
  (`microsoft-nebius-new-jersey` is Microsoft-Nebius New Jersey, `qts-cedar-rapids-ia` is QTS
  Cedar Rapids);
- `source`: the row's `source` is a link that exactly one Epoch record cites (one of Epoch's
  Selected Sources), and that record is in the same state. A report Epoch cites for several sites
  (a company's environmental report) links none;
- `epoch_source`: the row's own `source` is an Epoch AI page (Stargate Abilene cites Epoch's
  Stargate report); no record id.

AI GridWatch also has rows of its own, under other names, for some of the Epoch sites it copies,
often next to the copy. Rules read once the row is placed, all for a row that lies in the Epoch
record's county (the stated county, else the one containing the point):

- `link`: the row cites, as `source` or in its event log, a link specific to one Epoch site: the
  Epoch record cites it, or AI GridWatch's copy of the site (a row held by `name`, `id` or
  `source`) does, and no other Epoch record or AI GridWatch row cites it. Without the county
  condition this would tie Google's Haskell County row to Midlothian and Goodnight, 260 km away,
  through one statewide announcement;
- `street`: the row's name or id gives the Epoch record's street address, house number and street
  words alike ("216 Greenfield Road" is 216 Greenfield Rd).

Weaker evidence holds the row for review without merging it: no record, `record_id` empty, the
Epoch records it may be in `data.epoch_records`, and a reviewer decides.

- `several`: the rules tie the row to more than one Epoch site, or to one while the row shares an
  organization with another Epoch site in the same county (AI GridWatch's "AWS Madison County data
  center campuses" covers Epoch's Amazon Madison Mega Site and Amazon Ridgeland);
- `nearby`: no rule ties the row, but it shares an organization (owner, operator or tenant) with
  Epoch sites within 5 km, both points at locality precision or finer. Distance alone would merge
  neighbours (Compass's Red Oak campus is 1.1 km from Epoch's Google Red Oak), and an organization
  in the same county alone would merge Microsoft's withdrawn Caledonia site, 14 km from Fairwater.

The Epoch record is kept, since Epoch is the origin and has the dated timeline. The rule needs the
Epoch records, so `epoch` must run first, and the run refuses a store without them unless
`--without-epoch` is given (then only `epoch_source` applies; `metrics.epoch_records_seen` says
how many Epoch names were seen). When the store already holds an AI GridWatch record for a row
that is now held (one stored before its Epoch twin arrived), `merge_import` files the usual
`removed_upstream` item and keeps it, and the `possible_duplicate` item names it in
`data.stored_record`: a reviewer sets its `merged_into`. A row whose Epoch twin has no record (an
Epoch site in review) is imported, so the site still appears once. Merging the AI GridWatch
milestones into the Epoch record is entity resolution's job (M4), and so is releasing a held row
that a reviewer finds is another site: M1 has no switch for it.

On 2026-10-08, 79 rows were held: 69 by `name`, 4 by `link`, 2 by `id`, 1 by `street`, 1 by
`epoch_source`, and 2 for review (`several`, `nearby`). The nine rows the name rule missed, with
about 7.1 GW (7,060 MW) that had been counted twice:

| AI GridWatch row | Epoch site | Rule | Evidence |
|---|---|---|---|
| `microsoft-nebius-new-jersey` (300 MW) | Microsoft-Nebius New Jersey | `id` | the id; the Nebius release Epoch cites is also in the event log |
| `qts-cedar-rapids-ia` | QTS Cedar Rapids | `id` | the id |
| `openai-stargate-dona-ana-nm` | OpenAI Stargate New Mexico | `link` | the Oracle release Epoch cites, and a Food & Water Watch story the copy cites; Doña Ana County |
| `vantage-frontier-shackelford-tx` (1,400 MW) | OpenAI Stargate Shackelford | `link` | the groundbreaking story the copy cites; Shackelford County |
| `aws-new-carlisle-in` (2,400 MW) | Anthropic-Amazon New Carlisle | `link` | five stories the copy cites; St. Joseph County |
| `xai-colossus-memphis-tn` (2,000 MW) | Colossus 2 | `link` | three stories the Colossus 2 copy cites; Shelby County. The row covers the Colossus complex, Colossus 1 included, but names xAI where Epoch names SpaceXAI |
| `216-greenfield-road-lancaster-city-pa` | CoreWeave Lancaster Greenfield site | `street` | 216 Greenfield Road in the name; Lancaster County |
| `aws-salem-township-pa` (960 MW) | AWS Berwick (held for review) | `nearby` | Amazon, 4.3 km (Epoch's point is Berwick's Census point, in Columbia County; AI GridWatch places the campus in Salem Township, Luzerne County) |
| `aws-madison-county-ms` | Amazon Madison Mega Site and Amazon Ridgeland (held for review) | `several` | the 2024 announcement the Mega Site copy cites; Amazon owns both Madison County sites |

None of the new rules ties another row: `hexa-monroe-township-nj` shares a page with the Nebius
row, but lies in Gloucester County, not Cumberland.

## Limits (M1)

- **No other cross-source de-duplication until M4.** Entity resolution is 07 §6.6. Beyond the
  Epoch and AI GridWatch rules above, OSM covers several Epoch campuses, and those appear twice.
  A row the rules hold for review stays out of the store until M4 even if a reviewer finds it is
  another site.
- **AI GridWatch `events[]` is not imported** (2,287 events on 2026-10-07). It needs the extraction
  and verification steps of M2–M3. Only event kinds are read, for `has_filing`.
- **AI GridWatch stage dates.** The schema cannot say "status observed on date X, event date
  unknown", so the stage's `other` event is dated by `as_of`, the day AI GridWatch read its source.
  `derive_dates` skips `other` events for `first_reported`, `operating_since` and `cancelled`, so a
  row whose milestones do not reach the stage has none of those dates.
- **Epoch projections that do not change the mapped status are dropped.** A projected row whose
  mapped status equals the previous row's adds no event. The crosswalk reads "Buildings
  operational" and, when that cell is empty, "IT power (MW)": OpenAI Stargate Milam's 2028-12-31
  "site is fully operational" row (no count, 857 MW) is a planned `operating` event.
- **Census coverage.** Fewer than half of Epoch's street addresses give an agreeing match (new
  industrial roads often are not in the address ranges yet, and a match on another street is
  refused); the rest fall back to `locality`.

## Live run, 2026-10-08

Full runs (`uv run atlas import epoch`, then `aigridwatch`) into an empty store, then
`atlas validate` over the result: **279 records, 0 issues** (re-run offline on that day's inputs
and Census cache once the twin rules landed; 288 before them). A second run of each: every record
`unchanged`, no Census request (all answered from the cache), no record or review file changed.

| | Epoch AI | AI GridWatch |
|---|---|---|
| Upstream rows | 93 sites (77 US), 547 timeline rows | 284 projects (283 verified), 2,287 events |
| Records | 77 (every US site) | 202 |
| By location | 30 address, 1 street, 31 Gazetteer place, 2 county, 13 override (8 locality, 5 county) | 153 source coordinates, 35 Gazetteer place, 14 county centroid |
| Review items | none | 79 `possible_duplicate` (Epoch sites: 77 matched, 2 held for review), 3 `county_mismatch` (point outside the named county), 2 `conflict` (late announcements), 2 `geocode_failed` (Bloomfield CT, "Central Ohio"), 1 `unit_parse` ("67 MW" as the operator), 1 `unverified_upstream` |
| Status events | 17 planned (projections; OpenAI Stargate Milam's 2028-12-31 row is one since Epoch's IT power counts when the building count is blank) | 117 stage (`other`) events, 0 planned (no future hearing dates) |
| Other | 61 Census requests on the first run (2 duplicate addresses answered from the cache) | 2 personal names left out of the parties |

The store's capacity (IT MW, else facility MW) is 53,284 MW, 11,851 MW of it on Epoch records;
it was 60,344 MW before the twin rules, the 7,060 MW difference being the AI GridWatch twins.

Without the overrides the same run leaves 13 Epoch sites unplaced: the 8 without an address, the 3
addresses the chain cannot place (Meta Hyperion, Google Pryor (North), Meta Huntsville) and the 2
without a state (AWS New Albany, Stream Phoenix). Compared with 2026-10-07: Stream Phoenix's
address is no longer sent to the Census (its match, Litchfield Rd Byp, was 6.6 km off); the
matches on another street for Meta Montgomery (County Ct) and Anthropic-Amazon New Carlisle
(Larrison Dr) are refused; Meta Rosemount's two matches (145th St E and W) are ambiguous; and
OpenAI Stargate Shackelford is placed in Shackelford County instead of at Abilene's point in Taylor
County. Meta Montgomery, Anthropic-Amazon New Carlisle and Meta Rosemount fall back to their
Gazetteer place.
