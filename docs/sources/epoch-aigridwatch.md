# Epoch AI and AI GridWatch: the pipeline seeds

Two CC BY 4.0 datasets seed the pipeline (07 §4.1, §4.3): Epoch AI's frontier data centers, with
dated construction timelines, and AI GridWatch's tracker of contested proposals, with hearing and
decision dates. Both importers are thin mappings onto the shared framework
(`atlas/sources/base.py`) and the status crosswalk (`atlas/crosswalk.py`,
[status-crosswalk.md](../status-crosswalk.md)). Locations go through `atlas/geocode.py` (07 §6.5).

```sh
uv run atlas import epoch                       # fetch, geocode (Census Geocoder) and merge
uv run atlas import epoch --input data_centers.zip --no-geocode --offline
uv run atlas import aigridwatch                 # fetch and merge
uv run atlas import aigridwatch --input projects.json --offline
```

Both write `data/records/{id}.json`, `review/queue/{epoch,aigridwatch}.jsonl` and
`data/imports/{epoch,aigridwatch}.json`. A second run with the same inputs rewrites no record. The
receipt's metrics count the rows read, the records by location method, the planned and stage
events, the review items, and for Epoch the Census requests and cache hits and the overrides used
and unused (an unused entry names a site Epoch no longer lists).

## Epoch AI (`atlas/sources/epoch.py`)

| | |
|---|---|
| Input | <https://epoch.ai/data/data_centers/data_centers.zip> (robots.txt allows `/data/`), opened with `atlas.safezip`. Only `README.md`, `data_centers.csv` and `data_center_timelines.csv` are read. |
| License guard | The run fails if `README.md` no longer contains "Creative Commons Attribution", or a required file or column is missing. |
| Receipt | `InputSnapshot` license `CC-BY-4.0`, `upstream_version` = the ETag (none with `--input`). |
| Rows | `Country == "United States"`. Matched on `external_ids.epoch_name` = the Epoch `Name` (whitespace-normalized). |
| Arguments | `--overrides PATH` (default `config/overrides/epoch.json`), `--no-geocode` (no Census requests: overrides, Gazetteer places and county names only). |

### Mapping

| Record field | From |
|---|---|
| `record_type` | `campus` once a timeline row dated today or earlier has Buildings operational > 0, else `project` |
| `canonical_name` | `{Name} ({city or county}, {ST})`; the county is written in full ("Madison County", "Richland Parish") |
| `parties.operator` / `parties.tenant` | `Owner` / `Users`, split on commas. Names tagged `#confident` or `#likely` (and untagged names) are kept, `#speculative` and `#unlikely` are dropped, tags are stripped. |
| `aliases` | `Project`, same tag rule, `kind: "codename"` |
| `capacity.it_mw`, `capacity.facility_mw` | IT power (MW) and Power (MW) of the latest timeline row dated today or earlier, when > 0 |
| `cooling.water_use_mgd` | Water use (MGD) of the same row, when > 0 (Epoch writes 0.0 for "not estimated") |
| `money` | Current total capital cost × 10⁹, `investment_basis: "estimate"`, `currency_year: 2025` |
| `purpose`, `evidence_level` | `unknown`, `reported` |
| `status_history` | the timeline (below) |
| `field_meta` | `/capacity/*`, `/cooling/water_use_mgd`, `/money/investment_usd`: confidence 0.70, `imported`, `["s1"]`. `/location`: `derived` with the geocoder's confidence, or `stated` (0.85) for an override. |
| `sources` | `s1` = the dataset ("Epoch AI", "AI data centers", `open_dataset`, `CC-BY-4.0`) supporting `/canonical_name`, `/aliases`, `/parties`, `/location`, `/capacity`, `/cooling`, `/money`, `/status_history`. An override adds its source supporting `/location` (and `s1` then drops `/location`). Up to five `Selected Sources` links follow with empty `supports`: `sec_filing` for sec.gov, `government_record` for other `.gov` and `.us` hosts, else `news`; title = the link text. |

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
   from a "City, ST" part, then the county by name ("…, Mississippi Madison County").
3. No address and no override: `missing_location`. An address the chain cannot place:
   `geocode_failed`. Neither becomes a record.

## AI GridWatch (`atlas/sources/aigridwatch.py`)

| | |
|---|---|
| Input | <https://aigridwatch.com/data/projects.json> (robots.txt allows all). |
| License guard | The run fails unless `license == "CC BY 4.0"` and `projects` is a list. |
| Receipt | `InputSnapshot` license `CC-BY-4.0`, `upstream_version` = `generated`. |
| Rows | Rows with `verified: false` are leads, not facts (the dataset says so), and become `unverified_upstream` items. Matched on `external_ids.aigridwatch_id` = the row `id`. |

### Mapping

| Record field | From |
|---|---|
| `record_type` | `project` |
| `canonical_name` | `{name} ({city, municipality or county}, {ST})` |
| `location` | `lat`/`lon` as given, `precision: "locality"`, `geocode_method: "source_coords"`. The point must lie in the state (`CountyIndex.in_state`), else `county_mismatch` and no record. Rows without coordinates (52 on 2026-10-07) go through `geocode()` without the Census step: a Gazetteer place from the locality (or from a non-county parenthesis, "Decatur Township (Indianapolis)"), else the named county's centroid; else `geocode_failed`. |
| county | From the locality: the text in "(X County)" or "(X Parish)", or a locality that is itself a county ("Caddo Parish"; boroughs and census areas only in Alaska, since Pennsylvania boroughs are towns). Several counties ("Chester / Montgomery County") resolve to the one that contains the point, else none. |
| `city` / `municipality` | The locality outside the parentheses when it is not a county: townships, boroughs and villages go to `municipality`, other places to `city` ("City of" dropped). |
| `parties` | `operator` and `owner` split on " / ", `tenant` on " / " and ", ", `filing_entities` from `filing_llc` (split on " / " and ";", prose dropped). A party AI GridWatch annotates with a personal role and no organization marker ("A Person (developer)") is dropped: records hold organizations only (07 §5.3). |
| `aliases` | Each filing entity, `kind: "filing_llc"` |
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
milestone, so the record's status always equals the stage's. On 2026-10-07, 193 of 281 records
needed one.

## Geocoding (`atlas/geocode.py`)

`geocode(GeocodeRequest, census=..., gazetteer=..., counties=...)` stops at the first step that
works:

| Step | Precision | `geocode_method` | County | Confidence |
|---|---|---|---|---|
| 1. Source coordinates | as the source states | `source_coords` | point-in-polygon for address or better, else the stated county name | 0.70 |
| 2. Census Geocoder | `address` when the matched house number equals the input's, else `street` | `census_geocoder` | the response GEOID when the point lies in it, else point-in-polygon | 0.90 / 0.80 |
| 3. Gazetteer place | `locality` | `gazetteer` | the stated county name, if any | 0.60 |
| 4. County by name | `county` (the polygon's point on surface) | `county_centroid` | that county | 0.60 |

Every result is checked like `atlas validate` rule 5 (county-checked precisions inside the county
with the 0.003° tolerance, locality inside the state), and a Census match in another state than the
request's is refused, so a geocoded record always passes validation.

- **Census Geocoder:** `GET https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress`
  with `benchmark=Public_AR_Current&vintage=Current_Current&format=json&layers=Counties`, through
  `atlas.net.fetch` (`robots=False`, one request per second, retries on 429/5xx). Responses are
  cached by the SHA-256 of the address in `.cache/atlas/census/`, so re-runs and `--offline` runs
  with a warm cache send nothing. Addresses over 100 characters are not sent (the service answers
  400), and a 4xx answer counts as no match and is not cached.
- **Gazetteer:** `reference/census/2025_Gaz_place_national.zip` (32,350 places) and
  `2025_Gaz_counties_national.zip` (3,222 counties), checked against their SHA-256 on load. Place
  names match case- and accent-insensitively with the LSAD descriptor stripped ("Abbeville city",
  "Indianapolis city (balance)"), and St/Mt/Ft spelled out. A name shared by an incorporated place
  and a CDP means the incorporated place; any other duplicate is ambiguous and does not match.

## Overrides policy (`config/overrides/epoch.json`)

```json
{"Epoch Name": {"state_abbr": "LA", "county_fips": "22083", "city": null, "lat": null, "lon": null,
                "precision": "county", "source_url": "https://…", "note": "what the source says",
                "publisher": "optional", "source_type": "optional, else by host"}}
```

- Each entry must cite a `source_url`: Epoch's own Selected Sources links or a company or
  government page that states the location. Never from memory.
- The entry becomes its own source supporting `/location`. `county` precision without coordinates
  uses the county's point on surface, `locality` uses the Gazetteer place for `city`, and
  coordinates (`manual`) are checked against the county or state. A bad entry fails the run.
- Committed now: **Meta Hyperion**, whose Epoch address ("Holly Ridge, LA 71269") matches no
  Census place. Meta's Richland Parish Data Center page, Epoch's first Selected Source for the site,
  says it is in Richland Parish, Louisiana.
- Not committed: the eight US sites without an address (Google Mesa, Google Kansas City East,
  Google Storey County, Anthropic Barber Lake, OpenAI Stargate Michigan, Milam, New Mexico and
  Wisconsin). Whether to add cited overrides for them is an open owner decision; until then they
  stay `missing_location` items.

## Review items

| Kind | Importer | When |
|---|---|---|
| `missing_location` | epoch | no address and no override |
| `geocode_failed` | both | the chain found no location |
| `unknown_status` | both | Epoch: no timeline row dated today or earlier; AGW: a stage the crosswalk does not know |
| `unverified_upstream` | aigridwatch | `verified: false` |
| `county_mismatch` | aigridwatch | the coordinates are not in the stated state |
| `unit_parse` | aigridwatch | `size_mw` or `acres` is not a number in range (the record is kept without it) |
| `out_of_scope` | aigridwatch | a territory |
| `invalid` | both | a row that does not map to a valid record |

`apply_import` adds `conflict`, `held_human_reviewed`, `held_merged`, `removed_upstream` and the
validation `invalid` items.

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

## Limits (M1)

- **No cross-source de-duplication until M4.** Entity resolution is 07 §6.6, so the seed shows
  some sites more than once: Colossus 2 and Meta Hyperion, for example, are in both Epoch and AI
  GridWatch, and OSM covers several Epoch campuses.
- **AI GridWatch `events[]` is not imported** (2,287 events on 2026-10-07). It needs the extraction
  and verification steps of M2–M3. Only event kinds are read, for `has_filing`.
- **AI GridWatch stage dates.** The schema cannot say "status observed on date X, event date
  unknown", so the stage's `other` event is dated by `as_of`; for rows without milestones that date
  also becomes `first_reported`.
- **Epoch projections that do not change the mapped status are dropped.** The crosswalk reads
  "Buildings operational"; a projected row with that cell empty (OpenAI Stargate Milam's
  2028-12-31 "site is fully operational") maps to under construction, so no planned `operating`
  event appears for it.
- **Census coverage.** About half of Epoch's street addresses match (new industrial roads often
  are not in the address ranges yet); the rest fall back to `locality`.

## Live run, 2026-10-07

Full runs (`uv run atlas import epoch`, then `aigridwatch`) into an empty store, then
`atlas validate` over the result: **347 records, 0 issues**. A second run of each: every record
`unchanged`, no Census request (all answered from the cache).

| | Epoch AI | AI GridWatch |
|---|---|---|
| Upstream rows | 93 sites (77 US), 547 timeline rows | 284 projects (283 verified), 2,287 events |
| Records | 66 | 281 |
| By location | 32 address, 3 street, 29 locality, 1 county, 1 override (county) | 231 source coordinates, 36 Gazetteer place, 14 county centroid |
| Review items | 8 `missing_location`, 3 `geocode_failed` (AWS New Albany: no city or state; Google Pryor (North): the Census place is "Pryor Creek"; Meta Huntsville: Toney is not a Census place) | 1 `unverified_upstream`, 2 `geocode_failed` (Bloomfield CT, "Central Ohio") |
| Status events | 12 planned (projections) | 193 stage (`other`) events, 0 planned (no future hearing dates) |
| Other | 65 Census requests (2 duplicate addresses answered from the cache), 69 s | 1 personal name dropped from `owner` |
