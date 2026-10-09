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
and unused (an unused entry names a site Epoch no longer lists); for AI GridWatch also the rows
held for review (`held_for_review`), the first reports and unconfirmed hearings, the stages dated
with the file (`stage_events_undated`), the Epoch twins whose stage disagrees with the Epoch record
(`epoch_stage_conflicts`), the releases and the overrides used and unused.

Both need two Census files that are not committed: the place polygons (`cb_2025_us_place_500k.zip`,
which give a record's city) and the county-subdivision Gazetteer (`2025_Gaz_cousubs_national.zip`,
which places New England towns and townships). An import downloads them from census.gov on first
use into `{--cache-dir}/reference/census/`, checks them against their pinned SHA-256 and reuses
them; `--offline` without a copy fails with a message naming the file. Both are public domain
([reference/README.md](../../reference/README.md#downloaded-on-first-use)).

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
| `canonical_name` | `{Name} ({city or county}, {ST})`; the county is written in full ("Madison County", "Richland Parish"). A shared campus (below) takes the name of its first site in the CSV |
| `parties.tenant` | `Owner`, then `Users`, split on commas, each name once. Names tagged `#confident` or `#likely` (and untagged names) are kept, `#speculative` and `#unlikely` are dropped, tags are stripped. Epoch defines `Owner` as "Who owns the AI hardware in the data center. This is not necessarily the owner or operator of the facility" (Oracle at Stargate Abilene, where Crusoe builds and runs the campus; CoreWeave at sites that Core Scientific, Applied Digital and Galaxy own), so it is a company the facility houses, like the AI labs of `Users`: **Epoch's `Owner` is published as `tenant`, never as `owner` or `operator`** (owner decision of 2026-10-09). `owner` and `operator` stay empty, because Epoch names neither. (The M1 spec said `operator`; the seed of 2026-10-08 published `Owner` as `owner`.) |
| `aliases` | `Project`, same tag rule, `kind: "codename"`; a shared campus adds its other sites' names as `kind: "phase_name"` |
| `capacity.it_mw`, `capacity.facility_mw` | IT power (MW) and Power (MW) of the latest timeline row dated today or earlier, when > 0; a shared campus sums its sites. Left out when the timeline does not date the facility (below): the phase carries it |
| `cooling.water_use_mgd` | Water use (MGD) of the same row, when > 0 (Epoch writes 0.0 for "not estimated"); summed, and left out, like the capacity |
| `money` | Current total capital cost × 10⁹, `investment_basis: "estimate"`, `currency_year: 2025`; summed, and left out, like the capacity |
| `phases` | One per site, named `{Name} (buildings tracked by Epoch AI)`, with the site's capacity and `source_ids: ["s1"]`, for a shared campus or a timeline that does not date the facility; none otherwise |
| `purpose`, `evidence_level` | `unknown`, `reported` |
| `status_history` | the timeline (below) |
| `field_meta` | `/capacity/*`, `/cooling/water_use_mgd`, `/money/investment_usd`: confidence 0.70, `imported`, `["s1"]`. `/location`: `derived` with the geocoder's confidence, or `stated` for an override: 0.85 (07 §3.4, stated with a verbatim quote) plus the source adjustment (government record or regulator +0.10, utility, ISO or SEC filing +0.07, company release +0.05, press 0). |
| `sources` | `s1` = the dataset ("Epoch AI", "AI data centers", `open_dataset`, `CC-BY-4.0`) supporting `/canonical_name`, `/aliases`, `/parties`, `/location`, `/capacity`, `/cooling`, `/money`, `/status_history`. An override adds its source supporting `/location` (and `s1` then drops `/location`), with the override's `quote` (`quote_match: "human"`: copied by hand when the entry was written, not matched by the pipeline), `retrieved_at` and `archive_url`. Up to five `Selected Sources` links per site follow with empty `supports`, title = the link text, and `source_type` from `classify_source(url, link text)` (`atlas/sources/base.py`): `sec_filing` for sec.gov, an EDGAR accession number or an investor site's filing page; `government_record` for `.gov`, `.us` and `.mil` hosts and the agenda, code and permit platforms (Legistar, Granicus, CivicPlus, Municode, …); `utility_or_iso_filing` for ISO and RTO hosts; `company_release` for newswires and investor press pages; on an investor site or a document store (Google Drive, Scribd) the link text decides ("SEC 8k Filing", "Air Construction Permit"); else `news`. An override's source takes its `source_type`, else `classify_source` of its URL. |

### Status timeline

Rows are sorted by date and mapped with `from_epoch_row(Construction status, Buildings
operational)` on the status text with its markdown links reduced to their text, so a URL never
decides the status (the "Announces" in a link made Coreweave Helios's "land cleared" row an
announcement): operating when any building is operational, announced when the text is only about
plans, under construction otherwise. An event is kept only where the status changes, with
`seq` in date order, `as_of` at day precision, `source_ids: ["s1"]` and the construction-status text
as the note (markdown links reduced to their text, at most 200 characters, dropped if it looks like
it holds contact details).

An under-construction row keeps the crosswalk's `construction_start` only when its note says
construction starts ("Land clearing begins", "Construction start", "groundbreaking", "foundation
started", "First signs of construction"). Otherwise it observes work under way ("Land is cleared",
"Cooling install continues on the roof", a bond resolution for equipment): the first row is a
`first_reported` event and a later one an `other` event, and neither dates `construction_start`.
A first row that already counts operational buildings is an `other` event as well: Epoch began
tracking a site that was running, so the row dates neither the first report nor the start of
operation.

Rows dated after today are Epoch's projections (95 of 547 on 2026-10-07, up to 2030-01-01). They
become `planned: true` events, which never set the status or the derived dates (07 §2.3). A site
with no row dated today or earlier has no current status and becomes an `unknown_status` review
item.

### Timelines that do not date the facility

Epoch tracks the buildings it counts as AI compute ("the capacity of the AI-specialized chips we
estimate at a site, rather than all computing at the data center or campus"), so one Epoch site
can be buildings added to an older campus. The timeline does not date the facility when:

- a note on a row dated today or earlier says the tracked buildings were converted from or added
  to a facility that was already there, or that the site has a building Epoch does not count:
  "existing" before a data center, datacenter, Bitcoin or crypto (with only names, numbers and a
  parenthesis between), "former crypto", "data center was formerly", "first operational in
  {year}", "multi-tenant building", "Bitcoin mining" or "crypto mining", "non-AI building", "not
  for AI";
- the first row says "site expansion", "campus expansion", "of expansion", "begins expanding" or
  "new building" (on a later row these words are the tracked site's own growth);
- the first row dated today or earlier already counts operational buildings.

Its rows then become `other` events on a phase named `{Name} (buildings tracked by Epoch AI)`,
which carries the capacity, and the record has no construction or operating date and no capacity,
water use or cost of its own (07 §2.1: an expansion is a phase). When the tracked buildings are not
operating, the facility may be further along, and a `conflict` item says so: Epoch leaves Google
Fort Wayne's Building 1 out of its count as "not for AI compute", while Google said on 2025-12-11
that the data center is operational.

A site whose name or Selected Sources link title says "expansion" or "extension", and whose notes
decide nothing, is treated the same way and held for review with a `conflict` item quoting the
title. The title may name the tracked site's own growth ("Plan for 9-building and 6-building
expansions") or the reason the site exists (Meta Gallatin's "Campus extension announcement"), and
Epoch's fields do not say which; a reviewer restores the dates and capacity where the timeline
covers the whole facility. On 2026-10-08, with every Epoch site placed: 14 timelines that do not
date the facility, 10 held (12 and 3 on the integrated run, where 33 sites wait for an override).

### Sites that share a campus

Sites with the same street address (case, punctuation and spacing folded; it must start with a
house number) are one campus (07 §2.1): one record named after the first site in the CSV, with a
phase per site, each site's events on its phase, the capacity, water use and cost summed, the
tenants and codenames merged, the other sites' names as `phase_name` aliases, and every site in
`external_ids.epoch_name`. On 2026-10-08: Core42 Lake Mariner and Anthropic Lake Mariner (7725
Lake Rd, Barker, NY: TeraWulf's Lake Mariner campus, with two tenants), and OpenAI Stargate Abilene
and Crusoe Abilene Expansion (5502 Spinks Rd, Abilene, TX). A store that holds such sites as
separate records cannot take the shared record (its names match two records: a `conflict`), so
re-run the import on an empty store.

### Location

1. A cited entry in `config/overrides/epoch.json` (see the policy below).
2. Otherwise `parse_address(Address)` and `geocode()`: the Census Geocoder, then the county by name
   ("…, Mississippi Madison County"). The address's city is its postal city, which is only near
   the site and can lie in another county (AWS Berwick is mailed to Berwick, Columbia County, but
   sits in Salem Township, Luzerne County). It is sent as the city a Census match must agree with,
   and it gives its Gazetteer point (at `locality`, with no city) only inside a county the address
   names; it is never sent as the place the site is in. The record's city is the Census place that
   contains a Census match's point (the place polygons), else none, and the name gives the county
   (07 §6.5). An address that names no state ("13360 Miller Rd NW", "2950 S. Litchfield Road") is
   not geocoded at all: a Census match could come from any state. `--no-geocode` skips the Census
   step and needs no place polygons.
3. No address and no override: `missing_location`. An address the chain cannot place, or one
   without a state: `geocode_failed`, whose reason says why ("the address's city (Berwick) is only
   its postal city, which gives no point without a county a source states"), with the Census
   matches the chain refused in `data.census_matches` (Meta Jeffersonville: 500 E 8TH ST, a
   directional the address does not state). Neither becomes a record: a cited override places it
   (owner decision of 2026-10-09). On 2026-10-08, 33 sites wait for one.

## AI GridWatch (`atlas/sources/aigridwatch.py`)

| | |
|---|---|
| Input | <https://aigridwatch.com/data/projects.json> (robots.txt allows all). |
| License guard | The run fails unless `license == "CC BY 4.0"` and `projects` is a list. |
| Receipt | `InputSnapshot` license `CC-BY-4.0`, `upstream_version` = `generated`. |
| Rows | Rows with `verified: false` are leads, not facts (the dataset says so), and become `unverified_upstream` items. Matched on `external_ids.aigridwatch_id` = the row `id`. |
| Arguments | `--without-epoch`: run on a store that holds no Epoch record (only rows whose `source` is an Epoch AI page are held as Epoch's sites). Without it such a run fails. `--overrides PATH`: the reviewer releases (default `config/overrides/aigridwatch.json`; none when that file is absent), see [Releasing a held row](#releasing-a-held-row). |

### Mapping

| Record field | From |
|---|---|
| `record_type` | `project` |
| `canonical_name` | `{name} ({city, municipality or county}, {ST})` |
| `location` | `lat`/`lon` as given, `precision: "locality"`, `geocode_method: "source_coords"`. The point must lie in the state (`CountyIndex.in_state`), else `county_mismatch` and no record. Rows without coordinates (52 on 2026-10-07) go through `geocode()` without the Census step: a Gazetteer place from the locality (or from a non-county parenthesis, "Decatur Township (Indianapolis)"), or, when the Census has no place of that name, a county subdivision in the named county (a New England town or a township: "Bloomfield", CT is placed at the town's point, in the Capitol Planning Region, with `municipality`), else the named county's centroid; else `geocode_failed`. A Gazetteer place outside the named county is not used ("Storey County (near Reno)": Reno is in Washoe County), so such a row gets the county's centroid. |
| county | From the locality: the text in "(X County)" or "(X Parish)", or a locality that is itself a county ("Caddo Parish"; boroughs and census areas only in Alaska, since Pennsylvania boroughs are towns). Several counties are split on "/", "," and "and" ("(Chester / Montgomery County)"). With coordinates, the named county must contain the point; of two that do within the validate tolerance (a point near their border), the one the point lies in. When the point is outside every named county, the locality text is taken over the coordinates: the record is placed like a row without coordinates, at the Gazetteer place when it lies in the named county, else at the county's point (`county` precision), names that county, and gets a `county_mismatch` item; a point known to be in another county is never published (EdgeCore's Louisa County campus had its point in Goochland County, Dickerson's in Frederick County). With several named counties and no place, there is no point: `geocode_failed`. |
| `city` / `municipality` | The locality outside the parentheses when it is not a county: townships, boroughs and villages go to `municipality`, other places to `city` ("City of" dropped); a name the Gazetteer has only as a county subdivision (Bloomfield, CT) goes to `municipality`, and a row without such a name takes the subdivision the geocoder placed it in. A parenthesis that only says the site is near a place ("near Reno", "Granbury area", "north of …") may give the point, but never the city. |
| `parties` | `operator` and `owner` split on " / ", `tenant` on " / " and ", ", `filing_entities` from `filing_llc` (split on " / " and ";", prose dropped). Records hold organizations only (07 §5.3), so every name goes through one filter: a party AI GridWatch annotates with a personal role and no organization marker ("A Person (developer)") is dropped; a trailing parenthesis is a note, not part of the name, and is removed ("Amazon (AWS)", "(parcels)", "(proposed site)", and a person's name in "Example Ventures (A Person)"); a capacity typed into a party field ("67 MW") becomes a `unit_parse` item. Persons left out are counted in `persons_dropped`. |
| `aliases` | Each filing entity (after the same filter), `kind: "filing_llc"` |
| `capacity` | `size_mw` when in (0, 10000] (otherwise a `unit_parse` item) is always kept in `mw_as_stated` ("AI GridWatch size_mw: 120"). It becomes `it_mw` or `utility_request_mw` only when the row's note states that basis for the same figure (`size_basis`): critical IT load ("up to 300MW critical IT", "401 MW of critical IT load"), or a supply or interconnection ("Talen Energy agreed to supply 960 MW", "3.2 GW contracted from Georgia Power", "seeking 450 MW"). The schema calls the field "Planned IT/critical load", but its rows carry other bases (07 §2.4 never mixes them): with no stated basis the record has no MW basis at all ("120 MW / $3B campus", "24 MW data center"). A figure the note gives as a power source's ("835MW nuclear output", "450 MW gas-fired power plant") or one phase's ("phase 1 is 800MW") is kept only as stated, with a `unit_parse` item. |
| `site.acreage` | `acres`, when in (0, 100000]; otherwise `unit_parse` |
| `evidence_level` | `rumor` for the Rumored stage, else `reported` |
| `scope` | `out_of_scope` (kept, never published, 07 §2.2) when the row's name or note says it is a power supply deal or a generation facility rather than a data center site: "power purchase agreement", "PPA", "solar farm", "solar project", "power generation facility" or "not a data center" (Microsoft's Three Mile Island PPA, the Baconton power plant, two solar farms on 2026-10-08), with an `out_of_scope` item; else `in_scope` |
| `status_reason` | from the crosswalk (`moratorium`, `local_denial`); for Withdrawn, `litigation` when the note or a withdrawal entry cites a court ruling, `developer_withdrawal` when it names the developer, the applicant or a party of the row as the one who withdrew, else none ([status-crosswalk.md](../status-crosswalk.md)) |
| `sources` | `s1` = the tracker ("AI GridWatch", `open_dataset`, `CC-BY-4.0`) supporting `/canonical_name`, `/aliases`, `/parties`, `/location`, `/capacity`, `/site`, `/status_history`. `s2` = the row's `source` URL: publisher = host; `source_type` from `classify_source(url)` (`atlas/sources/base.py`, as for Epoch's links: `government_record` for `.gov`, `.us` and `.mil` hosts and agenda and permit platforms, `utility_or_iso_filing` for ISO and RTO hosts, `sec_filing`, `company_release` for newswires and investor press pages, else `news`); empty `supports`. |
| `field_meta` | `/capacity/it_mw` or `/capacity/utility_request_mw`, `/site/acreage`: 0.70 `imported`. `/location`: 0.70 `imported` (source coordinates) or 0.60 `derived` (geocoded, or placed by the locality text). |

### Status history

Events come from the milestone dates (all `source_ids: ["s1"]`; a date after today is a planned
event). AI GridWatch writes month-level dates as the 1st of the month (on 2026-10-08, 9 of 37
announced dates, 5 of 34 filing and 5 of 39 decision dates fall on the 1st, against 0 of 31 hearing
and 0 of 232 as_of dates), so every AI GridWatch date on the 1st is read at month precision
("2025-06-01" is June 2025: EdgeCore's release is dated June 25):

| Milestone | Event | Status |
|---|---|---|
| `announced` | `announced` | announced |
| `rezoning_filed` | `application_filed` | proposed |
| `hearing_date` after today | `hearing_scheduled` (planned) | proposed |
| `hearing_date` on or before today, when the row says it was held | `hearing_held` | proposed |
| `decided_date` + outcome `approved` / `denied` / `withdrawn` / `moratorium` | `approved` / `denied` / `withdrawn` / `paused` | permitted / denied / cancelled / paused |

`hearing_date` is "the next/decisive public hearing", and AI GridWatch warns that hearings get
moved: a date that has passed does not show the hearing took place (Dickerson's September hearings
were continued to November; 900 Conshohocken Road's August 17 session was "procedural matters only,
with no witness testimony"). A past hearing is `hearing_held` only when a decision is dated that
day, or an entry of the row's event log for that day, of kind `hearing`, `meeting` or `vote` and with
a source, says it was held or voted and says nothing of it being scheduled, moved or continued
(`hearing_held`). Otherwise there is no hearing event, and an `unknown_status` item gives the date
for a reviewer to check (24 records on 2026-10-08; 4 hearings counted as held).

The earliest dated entry of the event log that reports on the project dates a `first_reported`
event, when the row has no `announced` date and the entry is earlier than every milestone (38
records on 2026-10-08): before, a row with only a decision or a hearing date was first reported on
that date (Deep Green: the April 6 withdrawal, though its log starts with the March 4 Planning
Commission vote). Logs often start with the site's history or the place's rules (Louisa County
bought EdgeCore's business park in 2019; a township's data center ordinance; the applicant LLC's
registration), so an entry counts only when it names a data center or the row's name and is not
about an ordinance, a moratorium or other regulations, an annexation, an LLC's registration or an
interconnection request: a keyword test that errs toward a later first report. A row with an
`announced` date (AI GridWatch's "date first publicly proposed") is first reported then. The event's
status is `announced` (`proposed` when the entry is the row's own filing, rezoning or permit), so it
never sets the current status; there is none when the stage is built or being built (an early entry
may describe the site as it stands), when the entry is a construction or operation report, or when
it would move the record backward.

The stage then goes through `from_aigridwatch_stage(stage, has_filing)`, where `has_filing` is a
`rezoning_filed` date or an event of kind `filing` or `rezoning`; so "Proposed" means announced
without a filing and proposed with one. AI GridWatch derives the stage from data the milestones do
not always carry. When the events have no non-planned event, or roll up to a different status, an
`other` event with the stage's status is appended, noted "AI GridWatch stage '{stage}' as of
{as_of}". It is dated with the row's `as_of`, but never earlier than the latest milestone, so the
record's status always equals the stage's. A row without `as_of` (45 on 2026-10-08, most of them
the rows without coordinates) gets the same event at the file's `generated` date, noted "AI
GridWatch stage '{stage}', seen in its file on the file's date: the row has no as_of, so this is
not the date the stage began" (owner decision of 2026-10-09). Like an OSM tag's, the observation
sets the status and dates nothing: `derive_dates` skips `other` events, so it is never a first
report, an operating date or a cancellation. A later file that still shows the stage keeps the
date of the first one (the stored record's), so a weekly run rewrites nothing; a new stage is a
new observation. The file's date is not the day AI GridWatch read a source for the row, which is
why it dates nothing; a reviewer who confirms the stage can give that day in
`config/overrides/aigridwatch.json` (`as_of`). On the 2026-10-08 inputs, 145 records have a stage
event, 42 of them dated with the file.

A stage behind the row's own event log is not published either. When the log reports a later
milestone than an active stage, the row is held for review with a `conflict` item naming the entry
(date, kind, source), not imported: a denial of the project's land-use application with no filing
after it (Forsyth County "voted 5-2 to deny the rezoning request" while the stage read Awaiting
decision), an approval of its rezoning, conditional or special use, special exception, site plan,
development plan or PUD ("Person County commissioners voted to approve the rezoning" under In
review; Project Taurus's June approval, which AI GridWatch filed as `rezoning_filed`), or a
groundbreaking that is not the power supply's (Piketon's March 20 ceremony). The kinds alone do not
say it (a `vote` goes either way, a `construction` entry may be another site's), so only these
phrases count, and a word close before them that defers, conditions or denies them ("recommend",
"will", "scheduled", "not") voids them (`log_milestone`; 7 rows on 2026-10-08).

An `announced` date later than a filing, hearing or decision date would move the record backward
(announced after proposed, which 07 §2.3 flags). That milestone is left out and the row gets a
`conflict` item naming both dates (2 rows on 2026-10-08, announcements dated 5 days and almost 3
months after the rezoning filing).

### Ids

Records are matched on the row `id`. A row whose id starts with another row's name in the same
state, and not with its own (for a generic "Unnamed Data Center", the operator's name and the name),
has another project's id (`foreign_ids`): on 2026-10-08 a block of six PA DEP rows had each id on
the next row ("zediker-station-…" held PECO's Limerick project, "amazon-web-services-…-center-township-pa"
the Midland Borough row, while the real AWS Center Township row is "…-52"). Imported, such a record
would name another project and churn once AI GridWatch fixes the ids, so the row is held for review
with a `conflict` item (`data.id_of`). PA DEP's own project id would be a stable key; it is not in
AI GridWatch's file, and comes with the PA DEP importer (M4).

## Geocoding (`atlas/geocode.py`)

`geocode(GeocodeRequest, census=..., gazetteer=..., counties=...)` stops at the first step that
works:

| Step | Precision | `geocode_method` | County | Confidence |
|---|---|---|---|---|
| 1. Source coordinates | as the source states | `source_coords` | point-in-polygon for address or better, else the stated county name | 0.70 |
| 2. Census Geocoder (only for a request with a state) | `address` when the match agrees and its house number equals the input's, `street` when it agrees with another number or when the matched edge's address range spans 1,000 numbers or more (601 Kuna Mora Rd matched on the edge numbered 1-1229, 12 km from Meta's campus on that road) | `census_geocoder` | the response GEOID when the point lies in it, else point-in-polygon | 0.90 / 0.60 |
| 3. Gazetteer place | `locality`, unless a stated county does not contain the place. A name that is no Census place may be a county subdivision (a New England town, a township): its point, its county (a planning region in Connecticut) and `municipality`. A postal city (the city of an address) gives its point only inside a stated county, with no city | `gazetteer` | the stated county name, or the subdivision's county | 0.60 |
| 4. County by name | `county` (the polygon's point on surface) | `county_centroid` | that county | 0.60 |

The confidences follow 07 §3.4: derived in code, 0.90 from an address, otherwise 0.60.

A Census match counts only when it agrees with the request; otherwise the chain falls through to
the Gazetteer, one precision level down:

- its state is the request's;
- its ZIP is the input's, or its city is (when the parser found no city, the city's words appear in
  the address text);
- its street is the input's street, compared word by word after spelling-out and abbreviations
  are made one form (Road and RD, County Road and CO RD, Highway, Route and State Route all HWY;
  US Hwy stays apart). The input may leave out the street type, never a directional ("500 8th St"
  is not 500 E 8TH ST, 13 km from Meta Jeffersonville), and a type or qualifier the input states
  must be the match's own:
  "2950 S. Litchfield Road" is not 2950 LITCHFIELD RD BYP, "Co Rd 42" is not 42 COUNTY CT, and
  Larrison Blvd is not LARRISON DR. A numbered route may carry a trailing directional the Census
  leaves out ("1435 Hwy 54 W" is 1435 STATE RTE 54);
- when more than one match agrees and two lie more than 200 m apart, the address is ambiguous and
  none counts ("145th St" in Rosemount matches 145TH ST E and 145TH ST W, 4.6 km apart).

Every result is checked like `atlas validate` rule 5 (county-checked precisions inside the county
with the 0.003° tolerance, locality inside the state), and a locality point lies in the county the
result names, so a geocoded record always passes validation and never names a county its point is
outside of.

A Census match's city is the Census place that contains its point (`cb_2025_us_place_500k`, the
place polygons, `atlas/geo/places.py`), when the point lies more than 0.001° (about 100 m) inside
the place's line, else none; never the postal city of the address (QTS Cedar Rapids is mailed to
Fairfax, IA; OpenAI's Lordstown site to Warren). The margin is there because a Census point lies
on the street's centre line, which is often the city line. A record without a city names its
county.

- **Census Geocoder:** `GET https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress`
  with `benchmark=Public_AR_Current&vintage=Current_Current&format=json&layers=Counties`, through
  `atlas.net.fetch` (`robots=False`, one request per second, retries on 429/5xx). Responses are
  cached by the SHA-256 of the address in `.cache/atlas/census/`, so re-runs and `--offline` runs
  with a warm cache send nothing. Addresses over 100 characters are not sent (the service answers
  400). Only a 400 answer counts as no match (it is not cached); a 429 after the retries, a 408,
  any other 4xx and a 5xx fail the run, so a rate-limited request never moves a record down to
  the Gazetteer for one run and back on the next.
- **Gazetteer:** `reference/census/2025_Gaz_place_national.zip` (32,350 places) and
  `2025_Gaz_counties_national.zip` (3,222 counties), checked against their SHA-256 on load, and
  the county subdivisions (`2025_Gaz_cousubs_national.zip`, 36,427, downloaded on first use like
  the place polygons, see above), on for every import that geocodes a place name (AI GridWatch;
  Epoch states no town or township). Place
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
| `geocode_failed` | both | the chain found no location; Epoch: also an address that names no state, and an address whose only place is its postal city (no county a source states), with the refused Census matches in `data.census_matches`; a cited override places the site |
| `unknown_status` | both | Epoch: no timeline row dated today or earlier; AGW: a stage the crosswalk does not know (`data.stage`; no record); a past `hearing_date` nothing in the row says was held (`data.hearing_date`; the record has no hearing event) |
| `unverified_upstream` | aigridwatch | `verified: false` |
| `possible_duplicate` | aigridwatch | the row is an Epoch site (`record_id` = the Epoch record, `data.matched_by`), or may be one (`weak_link`, `nearby`, `several`, and `epoch_source` when a rule finds candidates: `record_id` empty, the candidates in `data.epoch_records`); no record. `data.stored_record` names an AI GridWatch record the store already holds for the row. With `data.released`, a reviewer released the row and it is imported |
| `county_mismatch` | aigridwatch | the coordinates are not in the stated state (no record), or not in the named county (the record is placed by the locality text instead; `data.lat`/`lon` are the coordinates not used) |
| `conflict` | both | AI GridWatch: the announced date is later than a filing, hearing or decision date (the announcement is left out); the event log reports a later milestone than the stage (`data.reported_status`, `event_date`, `event_kind`, `event_source`; held, no record); the id starts with another row's name (`data.id_of`; held, no record); a row held as an Epoch record's twin whose stage disagrees with that record's status (`record_id` = the Epoch record, `data.stage`, `as_of`, `epoch_status`, and `support`, the milestone or event-log entry that supports the stage, if any; the stage is not applied and nothing changes). Epoch: a name or cited source mentions an expansion, so the timeline may not date the facility (its dates and capacity are left out), or a timeline that does not date the facility has tracked buildings that are not operating (the facility may be further along); `data.evidence` quotes the note or title |
| `unit_parse` | aigridwatch | `size_mw` or `acres` is not a number in range, a party field holds a capacity, or the note gives `size_mw` as a power source's or one phase's (`data.basis`; the record is kept without it, `size_mw` only in `mw_as_stated`) |
| `out_of_scope` | aigridwatch | a territory (no record), or a power supply deal or generation facility (`data.phrase`; the record is kept with `scope: "out_of_scope"`) |
| `invalid` | both | a row that does not map to a valid record |

`apply_import` adds `conflict` (a candidate matches more than one stored record),
`held_human_reviewed`, `held_merged`, `removed_upstream` and the validation `invalid` items.

## Attribution

Every record cites its dataset in `sources[]` with the license; [ATTRIBUTION.md](../../ATTRIBUTION.md)
carries the full credits:

- Epoch AI, "AI data centers". Published online at epoch.ai. Retrieved from
  <https://epoch.ai/data/ai-data-centers>. CC BY 4.0.
- AI GridWatch (<https://aigridwatch.com>), AI GridWatch data center project tracker. CC BY 4.0.
- U.S. Census Bureau: Geocoder, 2025 Gazetteer Files (with the county subdivisions), 2025
  cartographic boundary files (counties, and the place polygons). Public domain.

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

- `link`: the row's own `source` is a link specific to one Epoch site (the Epoch record cites it,
  or AI GridWatch's copy of the site, a row held by `name`, `id` or `source`, does, and no other
  Epoch record or AI GridWatch row cites it), or the row cites such a link elsewhere while its own
  source is one the site or its copy cites; and the two MW agree within x2. Without the county
  condition this would tie Google's Haskell County row to Midlothian and Goodnight, 260 km away,
  through one statewide announcement;
- `street`: the row's name or id gives the Epoch record's street address, house number and street
  words alike ("216 Greenfield Road" is 216 Greenfield Rd).

Weaker evidence holds the row for review without merging it: no record, `record_id` empty, the
Epoch records it may be in `data.epoch_records`, and a reviewer decides.

- `weak_link`: a link specific to the site ties the row only through its event log, with its own
  source cited by neither the site nor its copy, or the row's `size_mw` and the site's MW (IT, else
  facility) differ by more than x2 (07 §6.6, Size). AI GridWatch copies a campus's history into rows
  about other projects: `aws-new-carlisle-in` is Amazon's $15B plan that "will expand our
  infrastructure to new sites across Indiana" and "add 2.4 gigawatts", tied to Epoch's
  Anthropic-Amazon New Carlisle (910 MW IT) only by the New Carlisle stories in its event log;
- `several`: the rules tie the row to more than one Epoch site, or to one while the row may share
  an organization with another Epoch site in the same county (AI GridWatch's "AWS Madison County
  data center campuses" covers Epoch's Amazon Madison Mega Site and Amazon Ridgeland);
- `nearby`: no rule ties the row, but it may share an organization (owner, operator or tenant) with
  Epoch sites within 5 km, both points at locality precision or finer (a row at its county's point
  is not near anything). Distance alone would merge neighbours (Compass's Red Oak campus is 1.1 km
  from Epoch's Google Red Oak), and an organization in the same county alone would merge
  Microsoft's withdrawn Caledonia site, 14 km from Fairwater;
- `epoch_source` lists, for the reviewer, the sites the location rules, an organization in common
  in the county, or one within 5 km point to (Stargate Abilene: Epoch's OpenAI Stargate Abilene).

"May share an organization" compares names without an alias table (`data/orgs.json` is empty in
M1): suffixes such as Inc, LLC and Corp and everything but letters and digits are dropped, and a name
of three or more characters inside another counts (`orgs_overlap`). So xAI and SpaceXAI, Amazon and
Amazon Web Services count as one; since these rules only hold a row, a near match costs a review,
never a merge. `xai-colossus-memphis-tn` names xAI and lies at Colossus 1's site, while a link ties
it to Colossus 2: it is held as `several`, naming both.

The item's reason says what happened to the row. A row held by `name`, `id` or `source` whose note
quotes "Epoch AI estimates" is AI GridWatch republishing the Epoch site. Every other merged row is
AI GridWatch's own row for that site: not imported, and nothing from it (its source, phase, approval
or events) is merged into the Epoch record, which M4's entity resolution does (07 §2.1 makes a newly
reported expansion a phase).

The Epoch record is kept, since Epoch is the origin and has the dated timeline. The rule needs the
Epoch records, so `epoch` must run first, and the run refuses a store without them unless
`--without-epoch` is given (then only `epoch_source` applies; `metrics.epoch_records_seen` says
how many Epoch names were seen). When the store already holds an AI GridWatch record for a row
that is now held (one stored before its Epoch twin arrived), `merge_import` files the usual
`removed_upstream` item and keeps it, and the `possible_duplicate` item names it in
`data.stored_record`: a reviewer sets its `merged_into`. A row whose Epoch twin has no record (an
Epoch site in review) is imported, so the site still appears once.

On 2026-10-08 (re-measured offline on the seed's saved inputs, with every Epoch site placed), 80
rows were held as Epoch's sites: 69 by `name`, 2 by `id`, 2 by `street`, and 7 for review (3
`weak_link`, 2 `several`, 1 `nearby`, 1 `epoch_source`). The eleven rows the name rule missed:

| AI GridWatch row | Epoch site | Rule | Evidence |
|---|---|---|---|
| `microsoft-nebius-new-jersey` (300 MW) | Microsoft-Nebius New Jersey | `id` | the id; the Nebius release Epoch cites is also in the event log |
| `qts-cedar-rapids-ia` | QTS Cedar Rapids | `id` | the id |
| `216-greenfield-road-lancaster-city-pa` | CoreWeave Lancaster Greenfield site | `street` | 216 Greenfield Road in the name; Lancaster County |
| `lancaster-ai-hub-east-formerly-referred-to-as-216-greenfield-road-chirisa-technology-parks-phase-2-city-of-lancaster-pa` | CoreWeave Lancaster Greenfield site | `street` | the same address; PA DEP's Phase 2 row (as_of 2026-10-08), not a separate Epoch phase |
| `openai-stargate-dona-ana-nm` | OpenAI Stargate New Mexico (held for review) | `weak_link` | the Oracle release Epoch cites and a Food & Water Watch story the copy cites, both in the event log; its own source is a list of data centers |
| `vantage-frontier-shackelford-tx` (1,400 MW) | OpenAI Stargate Shackelford (held for review) | `weak_link` | the groundbreaking story the copy cites, in the event log; its own source is Vantage's release |
| `aws-new-carlisle-in` (2,400 MW) | Anthropic-Amazon New Carlisle (held for review) | `weak_link` | five New Carlisle stories in the event log; the row's source and MW are Amazon's expansion to new Indiana sites, 2,400 MW against 910 MW |
| `xai-colossus-memphis-tn` (2,000 MW) | Colossus 1 and Colossus 2 (held for review) | `several` | three stories the Colossus 2 copy cites; xAI may be SpaceXAI, which owns both Shelby County sites |
| `aws-salem-township-pa` (960 MW) | AWS Berwick (held for review) | `nearby` | Amazon, 4.3 km (Epoch's point is Berwick's Census point, in Columbia County; AI GridWatch places the campus in Salem Township, Luzerne County) |
| `aws-madison-county-ms` | Amazon Madison Mega Site and Amazon Ridgeland (held for review) | `several` | the 2024 announcement the Mega Site copy cites; Amazon owns both Madison County sites |
| `stargate-abilene-tx` (1,200 MW) | OpenAI Stargate Abilene (held for review) | `epoch_source` | its source is Epoch's Stargate report |

Before the rules beyond `name`, ten of these rows (all but Abilene) were imported as records of
their own. Nine are Epoch sites listed a second time, about 4.7 GW (4,660 MW of `size_mw`) counted
twice. `aws-new-carlisle-in` is not: its 2,400 MW is Amazon's announced expansion to new sites,
which AI GridWatch files under New Carlisle; it is held for review rather than merged, and a
reviewer decides whether to release it as a project of its own. None of the rules ties another row:
`hexa-monroe-township-nj` shares a page with the Nebius row, but lies in Gloucester County, not
Cumberland.

Until the 33 Epoch sites that have only a postal city get their cited overrides (see
[Location](#location)), they have no record, so their AI GridWatch rows are imported: on the
integrated offline run of 2026-10-09, 47 rows are held as Epoch's sites (37 `name`, 2 `id`, 2
`street`, 5 `weak_link`, 1 `epoch_source`), and `aws-salem-township-pa` and `aws-new-carlisle-in`
are records of their own while AWS Berwick and Anthropic-Amazon New Carlisle wait for theirs.
The next import after the overrides holds them again; the stored AI GridWatch records are then
`removed_upstream` items with `data.stored_record` for a reviewer to merge.

A held row whose stage disagrees with its Epoch record's status also gets a `conflict` item
(`record_id` = the Epoch record): its stage is not applied, and nothing changes until a reviewer
decides. Google Fort Wayne is Operating in AI GridWatch (Google said so on 2025-12-11) and under
construction in Epoch's count of AI buildings; others are AI GridWatch rows whose stage lags the
Epoch timeline ("In review" for an operating campus, often a new phase's filing). 20 rows on the
integrated run.

## Releasing a held row

A reviewer who finds that a check holds a row wrongly releases it in
`config/overrides/aigridwatch.json`, one entry per row id, without a code change:

```json
{"example-row-id": {"release": ["epoch"],
                    "reason": "what the reviewer read, at most 300 characters",
                    "reviewed_at": "2026-10-09"}}
```

- `release` names the checks released: `epoch` (the Epoch AI site rules: the row is another site),
  `stage` (the event log's later milestone: the stage stands), `id` (the id that starts with another
  row's name) and `scope` (a power supply deal or a generation facility).
- `as_of`, optional: the day the reviewer confirmed the stage of a row that has no `as_of`; the
  stage's `other` event is then dated that day instead of the file's date, and its note says
  "confirmed by a reviewer on …". Like any `other` event it dates nothing.
- `reason` (at most 300 characters, no contact details) and `reviewed_at` are required; an unknown
  key, an empty entry or an `as_of` after today fails the run (exit 1, nothing written).

The row is then imported as any other, and while a released check would still hold it, each run
files an item with `data.released` naming the check and the reason, so the decision stays visible.
`metrics.overrides_used` and `overrides_unused` count the entries whose row is and is not in the
file; an unused entry names a row AI GridWatch no longer lists. Other corrections to an imported
record (its scope, a field) are edits to the record in the review PR (07 §6.7). The file holds no
entry on 2026-10-09.

## Limits (M1)

- **No other cross-source de-duplication until M4.** Entity resolution is 07 §6.6. Beyond the
  Epoch and AI GridWatch rules above, OSM covers several Epoch campuses, and those appear twice
  (owner decision: accepted until M4). A row the rules hold for review stays out of the store until
  a reviewer releases it ([Releasing a held row](#releasing-a-held-row)) or M4 resolves it.
- **AI GridWatch `events[]` is not imported** (2,290 events on 2026-10-08). It needs the extraction
  and verification steps of M2–M3. Only dates, kinds and a few phrases are read: for `has_filing`,
  for a hearing held, for the first report, and for a later milestone than the stage, which holds
  the row rather than setting a status. The phrases are keyword tests: they can miss a milestone
  (the stage is then published as before) or hold a row the reviewer releases.
- **AI GridWatch stage dates.** The stage's `other` event says "status observed on date X, event
  date unknown": it is dated by `as_of`, the day AI GridWatch read its source, or for a row without
  `as_of` by the file's date. `derive_dates` skips `other` events for `first_reported`,
  `operating_since` and `cancelled`, so a row whose milestones do not reach the stage has none of
  those dates.
- **AI GridWatch MW.** Most rows state no basis for `size_mw`, so most records carry the figure
  only in `mw_as_stated` and count as "without MW" on the map and in the totals until a source
  states the basis.
- **Epoch projections that do not change the mapped status are dropped.** A projected row whose
  mapped status equals the previous row's adds no event. The crosswalk reads "Buildings
  operational" and, when that cell is empty, "IT power (MW)": OpenAI Stargate Milam's 2028-12-31
  "site is fully operational" row (no count, 857 MW) is a planned `operating` event.
- **Census coverage.** Fewer than half of Epoch's street addresses give an agreeing match (new
  industrial roads often are not in the address ranges yet, and a match on another street is
  refused). The rest have only a postal city, which gives no point without a county a source
  states: those sites wait in review for a cited override (33 on 2026-10-08), and AI GridWatch's
  rows for them are imported meanwhile (no Epoch record holds them).

## Live run, 2026-10-08

The seed import (`uv run atlas import osm`, `epoch`, then `aigridwatch`, live, at 20:47–20:50 UTC)
saved its inputs: Epoch's `data_centers.zip`, AI GridWatch's `projects.json` (generated
2026-10-08, sha256 `16330d94…4ccab47`) and the 61 Census responses. The figures below are the
three importers' run on those inputs on 2026-10-09, after the third review round's fixes and the
owner decisions of that day, offline (`--input … --offline`, the Census cache from the saved
responses and the two downloaded Census files in the cache), into an empty store, then `atlas
validate` over the result: **1,368 records (1,103 OSM, 43 Epoch AI, 222 AI GridWatch; 1,282 in
scope), 0 issues**. A second run of each: every record `unchanged`, no Census request, no record or
review file changed. The seed itself, before the third review round's fixes, had 77 + 202 records
and 89 AI GridWatch review items.

| | Epoch AI | AI GridWatch |
|---|---|---|
| Upstream rows | 93 sites (77 US), 548 timeline rows | 285 projects (284 verified), 2,290 events |
| Records | 43 (44 of the 77 US sites; the other 33 wait for a cited override, because only their postal city places them; Core42 and Anthropic Lake Mariner share a campus) | 222 (4 out of scope: a power purchase agreement, a power plant, two solar farms); 14 rows held for review without a record, besides the 47 Epoch sites |
| By location | 25 address, 3 street, 2 county, 13 override (8 locality, 5 county) | 174 source coordinates, 35 Gazetteer place or county subdivision (Bloomfield, CT), 13 county point (EdgeCore, Dickerson and Meta Aiken among them, whose coordinates lie outside the county they name) |
| Review items | 37: 33 `geocode_failed` (32 with a postal city and no stated county, and Amazon Ridgeland, whose city the address parser cannot find; for 5 of them `data.census_matches` lists the refused matches: Jeffersonville's unstated directional, Montgomery's and New Carlisle's other streets, Rosemount's two, Ridgeland's 1626 E County Line Rd), 4 `conflict` (3 cited expansions held, 1 timeline whose tracked buildings are behind the facility) | 124: 47 `possible_duplicate` (Epoch sites: 41 matched, 6 held for review), 36 `conflict` (20 Epoch twins whose stage disagrees with the Epoch record, 8 event logs ahead of the stage, 6 ids of other rows, 2 late announcements), 25 `unknown_status` (past hearings nothing says were held), 7 `unit_parse` (`size_mw` a power source's or one phase's), 4 `out_of_scope`, 3 `county_mismatch`, 1 `geocode_failed` ("Central Ohio"), 1 `unverified_upstream` |
| Status events | 13 planned (projections; OpenAI Stargate Milam's 2028-12-31 row is one since Epoch's IT power counts when the building count is blank) | 145 stage (`other`) events, 42 of them dated with the file (rows without `as_of`), 60 `first_reported` from the event log, 4 `hearing_held`, 0 planned |
| Other | 61 Census requests on the live run (2 duplicate addresses answered from the cache); 21 records name the Census place that contains their point | 2 personal names left out of the parties |

The records' capacity (IT MW, else facility MW) is 7,587 MW: 4,052 MW on Epoch records (another
1,467 MW is on the phases of Epoch timelines that do not date their facility), 701 MW on AI
GridWatch records and 2,834 MW on OSM records. AI GridWatch adds 8,010 MW on a utility-request
basis. Before the basis rule, AI GridWatch's `size_mw` counted as IT MW on 60 records (41,433 MW).

Without the overrides the same run leaves 46 Epoch sites unplaced: the 8 without an address, the 3
addresses the chain cannot place (Meta Hyperion, Google Pryor (North), Meta Huntsville), the 2
without a state (AWS New Albany, Stream Phoenix), and the 33 above. Compared with 2026-10-07: Stream
Phoenix's address is no longer sent to the Census (its match, Litchfield Rd Byp, was 6.6 km off);
the matches on another street for Meta Montgomery (County Ct) and Anthropic-Amazon New Carlisle
(Larrison Dr) and on an unstated directional for Meta Jeffersonville (500 E 8th St) are refused;
Meta Rosemount's two matches (145th St E and W) are ambiguous; and OpenAI Stargate Shackelford is
placed in Shackelford County instead of at Abilene's point in Taylor County. Before the postal-city
rule, the 33 sites fell back to their postal city's Gazetteer point, some of them in another county
(AWS Berwick) or kilometres from the campus (Microsoft SAT40, 27 km from San Antonio's point).
