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

Two Census files they use are not committed: the place polygons (`cb_2025_us_place_500k.zip`, which
give the city of an Epoch record the Census Geocoder places; the OSM importer uses them too) and the
county-subdivision Gazetteer (`2025_Gaz_cousubs_national.zip`, which places the New England towns
and townships AI GridWatch names). An import downloads each from census.gov on first use into
`{--cache-dir}/reference/census/`, checks it against its pinned SHA-256 and reuses it; `--offline`
without a copy fails with a message naming the file. Both are public domain
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
| `capacity.it_mw`, `capacity.facility_mw` | IT power (MW) and Power (MW) of the latest timeline row dated today or earlier, when > 0; a shared campus sums its sites. Left out when the timeline does not date the facility (below): the phase carries it. These columns are Epoch's model (IT power from chip counts, power at its PUE), not a stated figure: a cited `capacity` entry replaces them with what an operator's page states (CoreWeave Ellendale ND: Applied Digital's "175 MW now live" against Epoch's 68 MW IT and 88 MW), keeping Epoch's figures in `field_meta` conflicts; and when the note of the row that set them says the counted buildings reached another figure ("bringing the first building to its expected 100 MW"), they are held back: only that phrase is kept, in `mw_as_stated`, and a `conflict` item asks for a cited capacity. A figure the note hedges (an estimate, "at least", a power plant's nameplate or turbines, a lease or contract, a forecast) does not count |
| `cooling.water_use_mgd` | Water use (MGD) of the same row, when > 0 (Epoch writes 0.0 for "not estimated"); summed, and left out, like the capacity |
| `money` | Current total capital cost × 10⁹, `investment_basis: "estimate"`, `currency_year: 2025`; summed, and left out, like the capacity |
| `phases` | One per site, named `{Name} (buildings tracked by Epoch AI)`, with the site's capacity and `source_ids: ["s1"]`, for a shared campus or a timeline that does not date the facility; none otherwise. A cited `facility_status` adds `{Name} (buildings Epoch AI does not track)`, with no capacity and its source |
| `purpose`, `evidence_level` | `unknown`, `reported` |
| `status_history` | the timeline (below) |
| `field_meta` | `/capacity/*`, `/cooling/water_use_mgd`, `/money/investment_usd`: confidence 0.70, `imported`, `["s1"]`; a cited capacity: `stated`, its source and the override confidence, with Epoch's figures in `conflicts` (`/capacity`, or `/phases/{i}/capacity` for a phase). `/location`: `derived` with the geocoder's confidence, or `stated` for an override: 0.85 (07 §3.4, stated with a verbatim quote) plus the source adjustment (government record or regulator +0.10, utility, ISO or SEC filing +0.07, company release +0.05, press 0), with the cited sources the entry rejected in `conflicts` (`value`, `source_ids`, `note`: Microsoft SAT40, TDLR's SAT11-14 record giving Medina County). |
| `sources` | `s1` = the dataset ("Epoch AI", "AI data centers", `open_dataset`, `CC-BY-4.0`) supporting `/canonical_name`, `/aliases`, `/parties`, `/location`, `/capacity`, `/cooling`, `/money`, `/status_history`. An override adds its source supporting `/location` (and `s1` then drops `/location`), with the override's `quote` (`quote_match: "human"`: copied by hand when the entry was written, not matched by the pipeline), `retrieved_at` and `archive_url`. Up to five `Selected Sources` links per site follow with empty `supports` (and any further link a timeline note links, or names by the publisher its link text leads with or its host's name: Microsoft-Nebius New Jersey's sixth, SemiAnalysis, whose estimate the note gives; Colossus 2's S-1 filing), title = the link text, and `source_type` from `classify_source(url, link text)` (`atlas/sources/base.py`): `sec_filing` for sec.gov, an EDGAR accession number or an investor site's filing page; `government_record` for `.gov`, `.us` and `.mil` hosts and the agenda, code and permit platforms (Legistar, Granicus, CivicPlus, Municode, …); `utility_or_iso_filing` for ISO and RTO hosts; `company_release` for newswires and investor press pages; on an investor site or a document store (Google Drive, Scribd) the link text decides ("SEC 8k Filing", "Air Construction Permit"); else `news`. An override's source takes its `source_type`, else `classify_source` of its URL. |

### Status timeline

Rows are sorted by date and mapped with `from_epoch_row(Construction status, Buildings
operational)` on the status text with its markdown links reduced to their text, so a URL never
decides the status (the "Announces" in a link made Coreweave Helios's "land cleared" row an
announcement): operating when any building is operational, announced when the text is only about
plans, under construction otherwise. An event is kept only where the status changes, with
`seq` in date order, `as_of` (below), `source_ids: ["s1"]` and the construction-status text as the
note (markdown links reduced to their text and bare URLs to their host, "Source:
datacenterdynamics.com", so a note shortened to 200 characters never ends in a cut URL; dropped
if it looks like it holds contact details).

`as_of` is the row's day, except where Epoch writes an estimate: a date on the 1st of a month is
read as the month (`2026-08`, precision `month`), as for AI GridWatch, and so is a date on the 15th
or the last day of a month whose note says it is estimated or assumed ("estimat…", "assum…", "best
guess", "we expect", "we think"). On the 2026-10-09 download 83 of the 452 US rows fall on the 1st,
about 12 on any other day, 29 on the 15th and 44 on a month's last day (38 of these with such a
note). Google Kansas City East's 2026-08-01 row reads "Building 1 operational. Estimated based on
present construction progress and typical timelines"; DCD dates Google Mesa's groundbreaking
2023-07-12, Epoch 2023-07-01. The derived dates take the event's precision (`operating_since:
2026-08`).

An under-construction row keeps the crosswalk's `construction_start` only when its note says
construction starts ("Land clearing begins", "Construction start", "groundbreaking", "foundation
started", "First signs of construction"). Otherwise it observes work under way ("Land is cleared",
"Cooling install continues on the roof", a bond resolution for equipment): the first row is a
`first_reported` event and a later one an `other` event, and neither dates `construction_start`.
A first row that already counts operational buildings is an `other` event as well: Epoch began
tracking a site that was running, so the row dates neither the first report nor the start of
operation.

Epoch's first row is what its imagery or a filing first shows, not the first public report: MPR
News reported the utility filing for Meta's Rosemount data center on 2023-09-02, Epoch's first row
("Land is cleared") is 2024-05-30. So `first_reported` is the earliest dated report among the
importer's inputs, when it is earlier than every event of a timeline that dates the facility and
its status is not ahead of the first event's (07 §2.3):

- a `first_report` in the site's override entry (below), which cites a dated report: the report's
  date as the page gives it (`published_at`: the page's date or time, the date it gives for an
  earlier public announcement, or a month, `2025-10`, when it gives only that) and the `status` it
  reports (`announced`, `proposed` or `permitted`). Its page is cited (a source the record already
  has for that URL, the location source or a Selected Source, also supports `/status_history` and
  takes the time as `published_at`). The entry's author dates the earliest report among Epoch's
  Selected Sources (their dates and titles, TDLR registrations' Registration Date), the location
  source and other pages; an entry that is not earlier records that check, and Epoch's first row
  stays first_reported (Vantage TX1: TDLR registered TX11 the day of Epoch's first row);
- an announcement a quote of the entry dates ("In December 2024, Meta announced that we are
  building our largest data center to date in Richland Parish": `2024-12`, citing the page);
- an announcement a note of Epoch's timeline dates ("their September 23, 2025 announcement",
  "announced on {date}", "In {month} {year}, ... announced"), on any row of the site, a
  projection's too, or on another site's row whose sentence names this site by the words of its
  name that the row's site does not share (Epoch's Lordstown row names "Lordstown and Milam
  County", so it dates OpenAI Stargate Milam too); it cites `s1`, with the sentence as the note.

The earliest of these (the more precise on a tie) becomes a record-level `first_reported` event on
its date, and no Epoch row is a first report any more. Otherwise `first_reported` stays Epoch's
first row (or a construction start, which derive_dates also counts), and that event's note begins
"Epoch AI's first observation, not a dated report:". A site without a `first_report` entry whose
Selected Sources may date an earlier report, a TDLR TABS registration (its Registration Date is
not in Epoch's CSV) or a date in a link's text or URL path earlier than the observation ("(Jan 23,
2024)", ".../2024/03/hello-rosemount/"), becomes an `unknown_status` item listing them, for a
reviewer's entry. A cited report whose status is ahead of the first event (CoreWeave Lancaster: a
PA DEP application, proposed, before CoreWeave's announcement) dates nothing and is a `conflict`
item. On 2026-10-09, 23 entries cite a first report.

The row that first shows a building operating dates `energized` with its own date, unless its note
says operation began earlier: "We estimate Building 1 became operational around early 2026 ...
SemiAnalysis estimated the full 50 MW was reached around January to February 2026" (Microsoft-Nebius
New Jersey, 2026-04-15) dates it `2026-Q1`, the most precise period the note states that lies
within the others ("January to February" inside "early 2026", which alone is the year), with those
clauses as the note. A period must start on or before the row's day and after the previous event;
a note that says so in words the importer cannot read, or whose periods disagree, makes the row an
`other` event (no `operating_since`) and an `unknown_status` item. Forecasts ("expected to be
operational by") do not count.

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
- the first row dated today or earlier already counts operational buildings;
- a note on a row dated today or earlier, or a Selected Sources link title, says the site's own
  infrastructure is modified or that Epoch counts only part of it: "site modifications", "of owned
  infrastructure" (CoreWeave Marble NC: "about 70 MW of HPC infrastructure from 100 MW of owned
  infrastructure", Core Scientific's bitcoin site since 2018), "only including" or "only counting"
  (AWS New Albany: "we're only including roughly 44% of all the New Albany campuses IT Power"),
  "believe is for AI" (Microsoft SAT40's TDLR link: "a set of three long buildings, which includes
  the first building we believe is for AI"). A retrofit or conversion alone ("retrofitting a former
  Electrolux building") says nothing of an older data center;
- a cited `timeline` entry with `coverage: "partial"` says so (below): Google Midlothian ("2019
  Google invests in our first Texas data center in Midlothian", Epoch's Building 1 from
  2023-11-28), Google Arcola (Bisnow, 2024-04-29: "The company operates a 400K SF data center in
  Arcola"), Meta Sarpy (Meta's fact sheet: "2017 Broke ground on the Sarpy Data Center").

Its rows then become `other` events on a phase named `{Name} (buildings tracked by Epoch AI)`,
which carries the capacity, and the record has no construction or operating date and no capacity,
water use or cost of its own (07 §2.1: an expansion is a phase).

When the tracked buildings are not operating, their status is not the facility's (the facility is
older, or has buildings Epoch does not count, and may be further along), so the site makes **no
record**: a `conflict` item says its status is unknown, until a cited `facility_status` entry
gives it. That entry puts the facility's status on a phase named `{Name} (buildings Epoch AI does
not track)`, as an `other` event (it dates nothing), so the record takes the more advanced of the
two: Epoch leaves Google Fort Wayne's Building 1 out of its count as "not for AI compute" and its
Buildings 2 on are under construction, while WPTA reported Google's announcement of 2025-12-11 that
the data center is operational; the committed entry cites it, and the record is operating and
`expanding`.

A site whose fields only suggest it is held for review with a `conflict` item that quotes the
evidence and says what held it, and is treated the same way (its status is kept):

- the name or a Selected Sources link title says "expansion" or "extension". The title may name the
  tracked site's own growth ("Plan for 9-building and 6-building expansions") or the reason the
  site exists (Meta Gallatin's "Campus extension announcement"), and Epoch's fields do not say
  which;
- the name or the first row names part of a site: a direction in parentheses at the end of the
  name ("Google Council Bluffs (East)", beside Google's older Bunge Avenue campus), a first row
  "for east buildings" (a direction, then "buildings", with no building number), or a first row
  whose lowest building number is above 1 ("for Buildings 5-9");
- a bitcoin miner owns or hosts the site: Core Scientific, TeraWulf, Galaxy Digital, Cipher Mining,
  Hut 8, Riot Platforms, Bitfarms, CleanSpark, Marathon Digital, MARA, Applied Digital, Bit Digital,
  Bitdeer, Greenidge, Soluna, Mawson, Stronghold Digital, Northern Data, HIVE Digital, IREN or Iris
  Energy in the site's name, Owner, Users, Project, a link title or a note (not a link's URL):
  CoreWeave Muskogee OK, Anthropic Barber Lake, CoreWeave Ellendale ND.

A reviewer who finds that the timeline covers the whole facility adds a cited `timeline` entry with
`coverage: "whole"`, which restores its dates and capacity. On the 2026-10-09 inputs, with every
Epoch site the overrides place: 20 timelines that do not date the facility (14 before these
rules), 14 held (10), and none whose facility status is unknown (Google Fort Wayne's is cited).

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

1. A cited location in `config/overrides/epoch.json` (see the policy below).
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
   (owner decision of 2026-10-09). On the 2026-10-08 inputs 33 sites waited for one; since the
   entries of 2026-10-09, only QTS Richmond 2 and QTS Richmond 3 do.

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
| `location` | `lat`/`lon` as given, `precision: "locality"`, `geocode_method: "source_coords"`. The point must lie in the state (`CountyIndex.in_state`), else `county_mismatch` and no record. Rows without coordinates (52 on 2026-10-07) go through `geocode()` without the Census step: a Gazetteer place from the locality (or from a non-county parenthesis, "Decatur Township (Indianapolis)"), or, when the Census has no place of that name, a county subdivision in the named county (a New England town or a township: "Bloomfield", CT is placed at the town's point, in the Capitol Planning Region, with `municipality`), else the named county's centroid; else `geocode_failed`. A Gazetteer place outside the named county is not used ("Storey County (near Reno)": Reno is in Washoe County), so such a row gets the county's centroid. A locality that names no county is placed in the county the row's note and event log place the site in ("on 82 acres in Stafford County near Fredericksburg"), when every county they place it in ("in X County", not in a clause about another site) is that one: Vantage VA4's "Fredericksburg" is a market name, and Fredericksburg city is not in Stafford County, so the record is placed at Stafford's point (`county` precision), names no city and gets a `county_mismatch` item (`data.stated_county`). A Virginia independent city is its own county-equivalent: a row placed at Fredericksburg's point names Fredericksburg city (51630) as its county. |
| county | From the locality: the text in "(X County)" or "(X Parish)", or a locality that is itself a county ("Caddo Parish"; boroughs and census areas only in Alaska, since Pennsylvania boroughs are towns). Several counties are split on "/", "," and "and" ("(Chester / Montgomery County)"), and a compound parenthesis is read part by part ("Berry Hill (Pittsylvania County / Danville)" names Pittsylvania County; Danville is the hint). With coordinates, the named county must contain the point; of two that do within the validate tolerance (a point near their border), the one the point lies in. When the point is outside every named county, the locality text is taken over the coordinates: the record is placed like a row without coordinates, at the Gazetteer place when it lies in the named county, else at the county's point (`county` precision), names that county, and gets a `county_mismatch` item; a point known to be in another county is never published (EdgeCore's Louisa County campus had its point in Goochland County, Dickerson's in Frederick County). With several named counties and no place, there is no point: `geocode_failed`. |
| `city` / `municipality` | The locality outside the parentheses when it is not a county: townships, boroughs and villages go to `municipality`, other places to `city` ("City of" dropped); a name the Gazetteer has only as a county subdivision (Bloomfield, CT) goes to `municipality`, and so does a New York or New England town even where a village shares its name (Lansing, NY). A compound text is split ("North Beaver & Mahoning townships", "Jessup / Olyphant / Throop Boroughs"). A row without coordinates takes the place or subdivision the geocoder placed it at; when it was placed by its parenthesis because the text's place is neither a Census place nor a subdivision ("Globeville-Elyria-Swansea (Denver)", a neighborhood), the record names the parenthesis's place. A row with coordinates names a place only when the point lies in it (`check_place`, with the place polygons, `cb_2025_us_place_500k.zip`): a city or a borough, village or city named as a municipality must be the Census place that contains the point (Ashburn, Lusby, Warrenton and Loxahatchee are not: their points lie in One Loudoun CDP, in no place, or in Wellington village), or, when the text's place is neither a Census place nor a subdivision, its parenthesis may name the one that does ("Martindale-Brightwood (Indianapolis)" is Indianapolis). The files the import uses have no township polygons, so a township or town is named unless the point lies in an incorporated place of Pennsylvania or New Jersey, which is a municipality of its own (Smithfield Township's point lies in East Stroudsburg borough), or more than 1.25 equal-area radii (land and water, from the county-subdivision Gazetteer) from the township's Gazetteer point (Hanover Township's lies 8.8 km from it, in Jefferson Township; a square's corner is 1.25 radii out). Of a compound text only the one place that passes is named. A name left out gets a `county_mismatch` item (`data.place`), and the record keeps the point and names its county. A parenthesis that only says the site is near a place ("near Reno", "Granbury area", "north of …") may give the point, but never the city. Nor is a place named that the row's note or an event-log entry puts the site outside (`outside_phrase`): "Mason Valley north of Yerington", "a site 11 miles north of Fort Stockton", "outside Socorro", "near Walnut Cove", "sought annexation into City of Burgin" (a capitalized word after the name makes it another name: "south of Tonganoxie Business Park", "outside the Sulphur Springs City Council meeting"; so does a building of the place: "outside Lansing town hall"). AI GridWatch's point is then the town's own, not the site's: the record names the county, keeps the point at `county` precision, and gets a `county_mismatch` item (`data.place`, `data.phrase`). |
| `parties` | AI GridWatch's `operator` ("Operator/developer, or blank if unknown") becomes `developer`, the role its definition guarantees: a real estate firm or a builder is no operator (Avison Young on PA DEP's unnamed Wilkes-Barre project), so no AI GridWatch record has an `operator` until another source or a reviewer says who runs the facility; an electric utility in that field (PSE&G, named for the approval a site needed) is no developer either and is left out with a `unit_parse` item, and so is a developer the row's note or log says is being replaced (Sulphur Springs: "a new developer will take over the Matrix Data Center Campus, with original developer MSB Global being phased out"), with a `conflict` item (`data.operator`). `operator` and `owner` split on " / ", `tenant` on " / " and ", ", `filing_entities` from `filing_llc` (split on " / " and ";", prose dropped). Records hold organizations only (07 §5.3), so every name goes through one filter: a party AI GridWatch annotates with a personal role and no organization marker ("A Person (developer)") is dropped; a trailing parenthesis is a note, not part of the name, and is removed ("Amazon (AWS)", "(parcels)", "(proposed site)", and a person's name in "Example Ventures (A Person)"); a capacity typed into a party field ("67 MW") becomes a `unit_parse` item. Persons left out are counted in `persons_dropped`. |
| `aliases` | Each filing entity (after the same filter), `kind: "filing_llc"` |
| `capacity` | `size_mw` when in (0, 10000] (otherwise a `unit_parse` item) is always kept in `mw_as_stated` ("AI GridWatch size_mw: 120"). It becomes `it_mw` or `utility_request_mw` only when the row's note states that basis for the same figure (`size_basis`) and no entry of its event log gives the campus another figure on that basis (DataBank Red Oak: the note's "up to 300MW critical IT" against its announcement's "240MW of critical IT power" of a 480 MW campus; the figure then stays only in `mw_as_stated`, with a `unit_parse` item, `data.others`): critical IT load ("up to 300MW critical IT", "401 MW of critical IT load"), or a supply or interconnection ("Talen Energy agreed to supply 960 MW", "3.2 GW contracted from Georgia Power", "seeking 450 MW"). The schema calls the field "Planned IT/critical load", but its rows carry other bases (07 §2.4 never mixes them): with no stated basis the record has no MW basis at all ("120 MW / $3B campus", "24 MW data center"). A figure the note gives as a power source's ("835MW nuclear output", "450 MW gas-fired power plant") or one phase's ("phase 1 is 800MW") is kept only as stated, with a `unit_parse` item. |
| `site.acreage` | `acres`, when in (0, 100000]; otherwise `unit_parse`. When the row's note or log gives its named campus an acreage of its own ("the 304-acre BCG Cedar Creek Campus portion of the 2,842-acre development"), that one, with a `unit_parse` item saying `acres` is the whole property's (`data.acres`, `campus_acres`) |
| `evidence_level` | `rumor` for the Rumored stage, else `reported` |
| `scope` | `out_of_scope` (kept, never published, 07 §2.2) when the row's name or note says it is a power supply deal or a generation facility rather than a data center site: "power purchase agreement", "PPA", "solar farm", "solar project", "power generation facility" or "not a data center" (Microsoft's Three Mile Island PPA, the Baconton power plant, two solar farms on 2026-10-08), with an `out_of_scope` item; else `in_scope` |
| `status_reason` | from the crosswalk (`moratorium`, `local_denial`); for Withdrawn, `litigation` when the note or a withdrawal entry cites a court ruling, `developer_withdrawal` when it names the developer, the applicant or a party of the row as the one who withdrew, else none ([status-crosswalk.md](../status-crosswalk.md)) |
| `sources` | `s1` = the tracker ("AI GridWatch", `open_dataset`, `CC-BY-4.0`) supporting `/canonical_name`, `/aliases`, `/parties`, `/location`, `/capacity`, `/site`, `/status_history`. `s2` = the row's `source` URL: publisher = host; `source_type` from `classify_source(url)` (`atlas/sources/base.py`, as for Epoch's links: `government_record` for `.gov`, `.us` and `.mil` hosts and agenda and permit platforms, `utility_or_iso_filing` for ISO and RTO hosts, `sec_filing`, `company_release` for newswires and investor press pages, else `news`); empty `supports`. |
| `field_meta` | `/capacity/it_mw` or `/capacity/utility_request_mw`, `/site/acreage`: 0.70 `imported`. `/location`: 0.70 `imported` (source coordinates) or 0.60 `derived` (geocoded, or placed by the locality text). |

### Status history

Events come from the milestone dates (all `source_ids: ["s1"]`; a date after today is a planned
event). AI GridWatch writes month-level dates as the 1st of the month (on 2026-10-08, 9 of 37
announced dates, 5 of 34 filing and 5 of 39 decision dates fall on the 1st, against 0 of 31 hearing
and 0 of 232 as_of dates), so a date on the 1st is read at month precision ("2025-06-01" is June
2025: EdgeCore's release is dated June 25). It writes a year-only date as January 1 too: Plaza
500's purchase "for $165 million in 2022" is dated 2022-01-01, Aligned's "(year reported as 2023)"
2023-01-01, and Metrobloks' announced and rezoning_filed 2025-01-01 stand for "In 2025, Metrobloks
filed a rezoning request" (the petition was filed in October). So a date on January 1 is read as
that year, unless the entry's summary names January (for a milestone, unless the note names
"January {year}"), and an entry whose summary gives only the year ("in 2022", "year reported as
2023") is that year whatever its month (`parse_agw_date`):

| Milestone | Event | Status |
|---|---|---|
| `announced` | `announced` | announced |
| `rezoning_filed` | `application_filed` | proposed |
| `hearing_date` after today | `hearing_scheduled` (planned) | proposed |
| `hearing_date` on or before today, when the row says it was held | `hearing_held` | proposed |
| `decided_date` + outcome `approved` / `denied` / `withdrawn` / `moratorium` | `approved` / `denied` / `withdrawn` / `paused` | permitted / denied / cancelled / paused |

An outcome `approved` is an approval to build (07 §2.3, permitted) only when the row names it one:
an event-log entry of the decision's day, or a clause of the note about that day (one that names
that date, or no date), with an approval of a rezoning, a conditional or special use, a special
exception, a site or development plan, a PUD, a variance, a plat or a permit (`land_use_approval`).
Otherwise there is no `approved` event, the stage stays an observation that dates nothing, and a
`conflict` item quotes the entry of that day for a reviewer to date the real approval: Botetourt's
Board of Supervisors approved Google's performance agreement on 2025-06-24 (an incentive; the
grading permit came in August 2026), and on 2026-10-08 the same holds for Nebius Independence (a
tax abatement), STACK Berry Hill (a performance agreement), Google Haskell County (no approval
named) and DC Blox (the commission "voted to approve the ... campus", no application named).

The milestone fields are read as the row's own note and event log explain them (fifth fix round,
`announcement`, `rezoning_filing`, `decision_day`):

- an `announced` date that the entries of that day (or, without any, the note's clauses that name
  it) all call an LLC's or a company's registration is no announcement ("National Land Developers
  registered Andover HPC Development in December 2025"): it is left out with a `conflict` item; one
  they all call the row's own filing is that filing ("filed a zoning permit application on April
  15, 2026": Muncy's April 15 is imported as `application_filed`, not `announced`);
- a `rezoning_filed` date is no filing when the note says only an inquiry was made (Abei Energy
  "emailed the Starke County Plan Commission asking about rezoning two parcels"; "before Abei's
  proposal advanced to a rezoning vote"), and is not the application's day when its entries of
  that day are the municipality's own procedure (Smithfield Township's curative amendment
  resolution, "180-day MPC review period begins") or only report the application ("Rezoning
  application revealed": Site Layer 4 had applied before a day the row does not give). Then no
  `application_filed` event is imported from it, with a `conflict` item (`data.rezoning_filed`,
  `application_filed`). When the row's own filing entries (naming the project or one of its
  organizations, and no electric utility), a filing day they or the note name, or an announced
  date the row calls a filing, are earlier, the earliest dates the application (Monticello Tech's
  2026-07-20 entry names its "July 6 land-use applications", against `rezoning_filed` 2026-07-07);
- a `decided_date` the day after the meeting the row says the vote ended around midnight
  ("approved the rezoning 4-1 around midnight following the May 11 meeting") is dated by that
  meeting, with a `conflict` item: Red Oak's approval is 2026-05-11, and its May 11 hearing was held.

An event-log entry whose link is a placeholder (Drox Rural Hall's nine
".../article_example.html" links, which return 404) dates nothing: it is no filing, no hearing
held, no approval named and no first report, and an `unverified_upstream` item lists them
(`data.entries`).

`hearing_date` is "the next/decisive public hearing", and AI GridWatch warns that hearings get
moved: a date that has passed does not show the hearing took place (Dickerson's September hearings
were continued to November; 900 Conshohocken Road's August 17 session was "procedural matters only,
with no witness testimony"). A past hearing is `hearing_held` only when a decision is dated that
day, or an entry of the row's event log for that day, of kind `hearing`, `meeting` or `vote` and with
a source, says it was held or voted and says nothing of it being scheduled, moved or continued
(`hearing_held`). Otherwise there is no hearing event, and an `unknown_status` item gives the date
for a reviewer to check (24 records on 2026-10-08; 4 hearings counted as held).

A row with an `announced` date (AI GridWatch's "date first publicly proposed") is first reported
then, unless an entry of its event log before that date reports the project: it then dates a
`first_reported` event (Meta Lebanon's "Meta proposes ~$800 million data center in Lebanon,
Indiana" of 2024-11-25, before the expanded campus announced on 2026-02-11; Digital Realty's Astra
campus, reported by Ingram's on 2026-06-09, the day before). Before the announced date an entry
counts only when it names the project, a site word, its acreage or one of its organizations with
its place, when its link does not date it on or after the announcement, and when it is no act out
of public view (an NDA, "negotiations conducted outside public view"). Otherwise the event log
dates a `first_reported` event (`first_report`), read in date order;
before, a row with only a decision or a hearing date was first reported on that date (Deep Green:
the April 6 withdrawal, though its log starts with the March 4 Planning Commission vote). An entry
is about the project when it names it (`names_project`): its name; a word of its name that names
the site ("Starpointe", "Boberg", "Powers Ferry") or its acreage (within 10%), with a data center,
a campus or a project named and no other use of the land (Hexa's warehouse proposal, a road
project), or an environmental review (AUAR, EAW, EIS, DRI) of an area of its acreage (Monticello's
Draft AUAR "for a 550-acre industrial area" of 2025-11, the row's 547 acres); its `size_mw` with a data center ("the 24 MW data center"); or one of its organizations
with its place or a data center ("TeraWulf ... the Lansing site", "the Prime Group data center
project"). Logs often start with the site's history or the place's rules, and the second seed check
found six of thirteen first reports dated wrongly by the keyword test that ran before, so these are
passed over: an entry that names neither the project nor a data center there; a property deal that
states no data center plan (Plaza 500's purchase "in 2022", Aligned's "Aligned Data Centers
purchased three parcels"); an LLC's registration; a construction or operation report (the site as it
stands); an entry about the rule itself, of a rule kind (rezoning, moratorium, ordinance, vote,
hearing, meeting, ruling) whose clauses that name the project all speak of an ordinance, a
moratorium or other rules (Hanover Township's ordinance, adopted "before Prime Data Centers LLC
submitted its Starpointe application"), while a report on the project that mentions one counts
(Posey County's residents meeting on 2026-06-03 to oppose the data center at Boberg and Hoenert
Roads and asking for a moratorium); and another site of the same company (an entry with another
acreage, or "existing", "first", "additional" acres: Compass's first Red Oak campus of 225 acres,
and its 375 more), after which an entry that names only the company and the place is taken as
that site's. The first entry about the project dates the report, at its date or at its source's
publication day when the link gives an earlier one ("/2026/06/26/": Wolcott's open house of June 29
was reported on June 26), never later. It must be earlier than every milestone the row states,
imported or not: `rezoning_filed`, a past `hearing_date` (held or not: a set hearing was public;
Wolcott's 2025-08-11 hearing comes ten months before its open house), `decided_date`, and a filing
month the note or an undated filing entry states ("filed a conditional-use application in May
2026"); when the earliest milestone is such a filing month, that month is the first report, as
`proposed` (Hanover: 2026-05). There is none, and an `unknown_status` item says why, when the
earliest report cannot be told: an entry that only mentions a data center there before any entry
names the project (policy news, a statewide protest day, a report that may be another project's),
or an entry that names the project but is dated by the event it announces ("scheduled", "will",
"open house", "set for") while its source gives no earlier date (Mason County's "scheduled public
hearings for March 25-26", dated March 25, from an article of March 23). The event's status is
`announced` (`proposed` when the entry is the row's own filing or a permit), so it never sets the
current status; there is none when the stage is built or being built, or when it would move the
record backward.

`dates.first_reported` is the earliest dated event (`rollup.derive_dates`), so a record without a
`first_reported` event is first reported at its earliest milestone. When the row shows the project
public before that milestone (an entry that names it, a stated filing month, an unconfirmed
hearing) and no first report can be dated, the import would publish a first report later than the
row shows: the row is held for review instead, with a `conflict` item (`data.earliest`,
`data.derived`; Plaza 500, whose substation hearing "serving the Plaza 500 data center" was set in
November 2024 against its 2026-07-16 filing; Mason County). So is a row whose earliest dated
milestone is a denial, a withdrawal, a cancellation or a pause and whose first report cannot be
dated: a project is public before it is denied or withdrawn, so that day is never its first report
(`conflict`, `data.earliest`; Project Riverjump, whose only milestone is the mayor's withdrawal
while rumors circulated from January, Douglas County's denial, and seven more on 2026-10-08). A
reviewer dates the first report from the source with `first_reported` in
`config/overrides/aigridwatch.json`, or releases the check (`first_report`) to accept the
milestone.

The stage then goes through `from_aigridwatch_stage(stage, has_filing, no_application)`, where
`has_filing` is a `rezoning_filed` date the row shows is a filing (not an inquiry or the
municipality's procedure, above), an announced date it explains as a filing, or an entry that is
one of the row's own applications (`own_filing`: of kind `filing` or `rezoning`, with a source that
is not a placeholder link, about an application, a petition, a request, a plan or a permit, and not
about the place's rules, a resolution or fees, a property deal or someone else's lawsuit, appeal or
motion: Posey County's only `rezoning` entry is its Area Plan Commission revising its own data
center ordinance; an entry whose first clause files the application and names no rule is one,
whatever a later clause says of an ordinance: Pronghorn "submitted a conditional use permit
application in November 2025 ..., months after the county amended its zoning ordinance"); so
"Proposed" means announced without a filing and proposed with one. `no_application` is the note,
or an event-log entry about the project, saying nothing has been formally proposed ("despite no
official proposal", "No formal application", "No formal site plan was ever filed", "no permit
applications before January 2027", "no formal application or site review had been submitted",
"will not submit permit applications"), or the note saying only an inquiry was made: then In
review, Hearing scheduled and Awaiting decision are announced when no filing is known, since
proposed needs a pending application (07 §2.3), with a `conflict` item (Posey County, Tonganoxie's
Project Bluestem, Andover Township and Project Zora on 2026-10-08). AI GridWatch derives the stage from data the milestones do
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

A stage behind the row's own event log or note is not published either. When the log or the note
reports a later milestone than an active stage, the row is held for review with a `conflict` item
naming the entry (date, kind, source; the note has no date), not imported: a denial of the project's
land-use application with no filing after it (Forsyth County "voted 5-2 to deny the rezoning
request" while the stage read Awaiting decision; the log only), an approval of its rezoning,
conditional or special use, special exception, site plan, development plan or PUD ("Person County
commissioners voted to approve the rezoning" under In review; Project Taurus's June approval, which
AI GridWatch filed as `rezoning_filed`), one a lawsuit seeks to overturn, vacate or void (Wolcott's
neighbors "sue ... to overturn the rezoning", under Awaiting decision; Stratos), or a groundbreaking
or construction under way that is not the power supply's, in a clause about the data center
(Piketon's March 20 ceremony; Meta Lebanon's note, "groundbreaking construction reported ongoing",
and its log, construction disruption "during Meta campus build-out", under Proposed; Google Haskell
County's Journey 2A building under Approved; in the note, "the company", "the developer" or "the
project" is the row's: Nebius Independence's "Council approved Chapter 100 abatement 5-2 and the
company has broken ground", under Approved). For a row at announced or proposed it is also held
when the log or note leaves no application under review: the application refused as incomplete
and the refusal upheld (Urbana: the BZA "unanimously denies Thor's appeal, upholding city
determination that site plan application was incomplete"), or, after the row's first filing, a
moratorium or ban on data centers adopted that the entry does not say exempts the project
("exempt", "does not affect", "does not block", "statewide": Urbana's council "passed a 12-month
moratorium on new data centers" three weeks after the filing; Cave City and Lyon Township too;
present-tense verbs count, "Township supervisors impose nine-month moratorium", and a demand or a
draft does not, "residents urged the board to impose"). For a row at announced with no filing, a
ban or moratorium adopted after the project was public (the earliest date the row shows, or any
when it shows none) holds it the same way, and the note's ban counts too (Andover's "the township
passed Ordinance 2026-13 banning data centers township-wide"; Project Zora's county moratorium of
2026-08-11). A moratorium a later entry says ended does not ("Commission votes 3-2 against
extending moratorium; moratorium expires": Leavenworth County's, for Project Bluestem). A row
Under construction is held when its log reports an injunction or a halt of its works that no later
entry lifts (Matrix: the judge "issues a temporary injunction freezing new construction/development
on the ~5,000-acre site pending trial") (`log_obstacle`). The kinds alone do not say it (a `vote` goes either way, a
`construction` entry may be another site's), so only these phrases count, and a word close before
them that defers, conditions or denies them ("recommend", "will", "could", "scheduled", "not")
voids them (`log_milestone`).

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

### Fifth fix round, 2026-10-09

AI GridWatch importer version 4, on the fourth seed's saved inputs (`projects.json` of 2026-10-08,
sha256 `16330d94…4ccab47`, the same Census cache and `--now`), offline, after the OSM and Epoch
imports into an empty store: 167 AI GridWatch records (163 in scope; 181 and 177 before) and 271
review items (220 before), `atlas validate` over the store 1,225 records, 0 issues, and a second run
`unchanged`. Fourteen rows the fourth seed published are held now: nine whose only dated milestone
is a denial, a withdrawal or a pause (Project Riverjump, Douglas County, Project Tango, 100 Jersey
Avenue, Dulles Cloud South, Mountain Road Technology Park, Pennhurst, Karis Plum Farms and A1
Millville), Andover and Project Zora (a ban or moratorium after the project was public), Muncy
(supervisors "impose" a moratorium after its filing), Matrix (an injunction freezing its
construction) and Nebius Independence (its note's groundbreaking). 22 `county_mismatch` items name a
place the row puts the site outside (19 on published records, among them Yerington, Fort Stockton,
Socorro, Wheatland, Tonganoxie, Burgin, Rural Hall and Walnut Cove); Vantage VA4 is placed in
Stafford County. Five `rezoning_filed` dates and three announced dates are read as the row explains
them (Smithfield, Site Layer 4 and Abei Energy publish no application date, Monticello Tech's is
July 6 and Muncy's April 15; Andover's registration, Muncy's and Amazon Warrenton's filings), Red
Oak's approval is dated by its May 11 meeting, DataBank Red Oak keeps 300 MW only as stated, BCG
Cedar Creek's acreage is 304, Drox Rural Hall's nine placeholder links date nothing, and 50
records carry an event-log first report (49 before), now also before an announced date (Digital
Realty's Astra campus, reported by Ingram's the day before AI GridWatch's date).

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
                "publisher": "Meta", "source_type": "company_release",
                "place_check": "only for a city that is also the address's postal city",
                "timeline": {"coverage": "partial", "source_url": "…", "quote": "…", "…": "…"},
                "first_report": {"published_at": "2024-03-14T15:22:17Z", "status": "announced",
                                 "source_url": "…", "quote": "…", "…": "…"},
                "facility_status": {"status": "operating", "as_of": "2025-12-11",
                                    "source_url": "…", "quote": "…", "…": "…"},
                "capacity": {"it_mw": 175, "facility_mw": null, "mw_as_stated": "175 MW now live",
                             "as_of": "2026-07-01", "source_url": "…", "quote": "…", "…": "…"},
                "milestones": [{"event": "energized", "as_of": "2023",
                                "source_url": "…", "quote": "…", "…": "…"}],
                "conflicts": [{"value": "Medina County, TX", "source_url": "…", "quote": "…",
                               "…": "…"}],
                "states_status": "optional: the status the location source gives as current"}}
```

An entry is keyed by Epoch's site name and has a location (the fields at its top level, with its
`conflicts`), any of the other cited parts, or both. Each part has its own citation
(`source_url`, `archive_url`, `quote`, `retrieved_at`, `note`, `publisher`, `source_type`, and
`states_status` when the page gives the facility's current status), so a site Epoch's address
places can still carry a `timeline` (Meta Sarpy), a `facility_status` (Google Fort Wayne), a
`first_report` (Microsoft SAT14), a `capacity` (CoreWeave Ellendale ND) or `milestones` (Google
Bristow).

- Each entry cites a `source_url`: one of Epoch's own Selected Sources for the site first, a
  primary source (company, government, regulator) before press. Never from memory.
- `quote` is copied verbatim from the page when it was read (`retrieved_at`), and states the
  place. When the live page refuses automated clients, the Wayback Machine snapshot that was read
  goes in `archive_url`. `tests/sources/test_epoch.py` checks that every entry has its citation
  fields and that the places it names appear in the quote.
- An entry places the site no more precisely than the quote states: the municipality (`city`,
  from the Gazetteer) or the county, unless the source gives a street address. A township or a New
  England town is a county subdivision, not a Census place: it goes in `municipality` with
  `county_fips`, at `locality`, and its point is that county subdivision's Gazetteer point in the
  county (the import then loads the county-subdivision Gazetteer, downloaded on first use). A
  `county` entry names no city or municipality, since the county's point need not lie in it (AWS
  Berwick's Luzerne County point is in Rice township, 17 km from its Salem Township campus): the
  run fails otherwise. The quote itself names every place the entry gives (a reader of the
  record's source sees only the quote), and a dateline ("Kansas City, MO — March 20, 2024 —") does
  not count: it says where a release was issued. `test_every_committed_override_is_cited` checks
  both.
- A town the quote names that is also the postal city of Epoch's address is only that, unless the
  site lies inside the town's Census place polygon (`cb_2025_us_place_500k`): "the expansion of our
  existing facility in Pryor" echoes the address of a campus that lies outside Pryor Creek, in
  Mayes County. Such an entry needs `place_check`, how the site was found inside the polygon (an
  OSM object of the site, or the street of its address); without it the run fails. Where the site
  lies outside, the entry gives the county a source states, or the site waits in review.
- The entry becomes its own source supporting `/location`. `county` precision without coordinates
  uses the county's point on surface, `locality` uses the Gazetteer place for `city` (or the county
  subdivision for `municipality`), and coordinates (`manual`) are checked against the county or
  state. When an entry names a county, the point (the Gazetteer place's included) must lie in it at
  every precision, so a record never names a county its point is outside of. A bad entry fails the
  run.
- `timeline`: `coverage: "partial"` where the cited page shows the facility older or larger than
  the buildings Epoch tracks, `"whole"` where a reviewer found that a held timeline is the whole
  facility. Its source supports `/phases` (`/status_history` for `whole`).
- `first_report`: the earliest dated report among the site's sources ([Status timeline](#status-timeline)),
  which dates `first_reported` when it is earlier than Epoch's first row. Its source supports
  `/status_history`. An entry for a site whose Selected Sources include a TDLR registration, or a
  link dated before Epoch's first row, is what clears that site's `unknown_status` item.
- `capacity`: the tracked buildings' capacity as the cited page states it on `as_of`: `it_mw` or
  `facility_mw` only for the basis the page states, `mw_as_stated` the page's phrase (it must be in
  the quote). It replaces Epoch's modelled columns for that site (the phase's, or the record's),
  which stay in `field_meta` conflicts; if Epoch's figures change after `as_of`, a `conflict` item
  asks a reviewer to check the entry.
- `milestones`: a `construction_start` or `energized` event a cited page dates earlier than
  Epoch's row for it, at the page's precision (`as_of`: YYYY, YYYY-Qn, YYYY-MM or YYYY-MM-DD). It
  becomes a record-level event citing the page, and Epoch's event an observation (`other`). For a
  timeline that dates the facility and is not shared; a milestone that is not earlier, has no
  Epoch event to replace or would precede an event of an earlier status is not applied and is a
  `conflict` item.
- `conflicts` (in the location): each cited source that places the site elsewhere and that the
  entry's author rejected, with the place it states as `value`. They go into
  `field_meta["/location"].conflicts` (the source cited, `supports` empty) and a `conflict` item.
- `states_status` (on any citation): the facility's status the page gives as current. When it is
  not the record's status the record gets a `conflict` item, so a source the record cites never
  contradicts its status unseen. Prefer a source that states none: Meta Montgomery's location
  source was Baxtel's campus page, which lists the campus as under construction; since 2026-10-09
  it is Baxtel's news of the announcement (2024-05-09), which names Montgomery County and states no
  current status.
- `facility_status`: the facility's status on `as_of`, for a timeline that does not date the
  facility ([Timelines that do not date the facility](#timelines-that-do-not-date-the-facility)).
  Its source supports `/status_history`.
- Committed (owner decisions of 2026-10-08 and 2026-10-09: the sites the seed cannot place, and
  those whose only place is their postal city, get cited overrides), 56 entries: 43 locations
  (13 read on 2026-10-08 and 30 on 2026-10-09; later on 2026-10-09 three moved to their county
  and one to its township, below, and Meta Montgomery's source changed), 1 location conflict, 3
  timelines, 23 first reports, 1 facility status, 2 capacities and 2 milestones (the fifth fix
  round's entries, read on 2026-10-09 at 21:44–21:59 UTC, are in the second table below). The first 13 were read on
  2026-10-08 (Google Kansas City East, OpenAI Stargate Michigan and AWS New Albany re-read the same
  day for a quote that states the place):

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
| Google Pryor (North) | Mayes County, OK (county; since 2026-10-09: the campus lies outside Pryor Creek, its postal town) | Google's Oklahoma location page (Google's post, cited until then, names only Pryor) |
| Meta Huntsville | Huntsville, AL (locality) | the site contractor's project page |
| Stream Phoenix | Goodyear, AZ (locality) | Stream's case study, through the Wayback Machine |

  Eight have no address in Epoch; Meta Hyperion, Google Pryor (North) and Meta Huntsville have an
  address the chain cannot place; AWS New Albany and Stream Phoenix have an address without a
  state.

  The other 30 were read on 2026-10-09, for sites whose address has only a postal city (Amazon
  Ridgeland: one whose city the parser cannot find):

| Site | Placed at | Source |
|---|---|---|
| AWS Berwick | Salem Township, Luzerne County, PA (locality: the township's Gazetteer point; county precision until 2026-10-09, at the county's point in Rice township) | the Susquehanna River Basin Commission's Federal Register notice |
| Amazon Ridgeland | Ridgeland, Madison County, MS (locality) | MDEQ's permit review summary |
| Anthropic-Amazon New Carlisle | St. Joseph County, IN (county) | Amazon's Project Rainier post |
| Colossus 1 | Memphis, TN (locality; the quote says South Memphis) | a Senate Environment and Public Works Committee letter to EPA |
| CoreWeave Denton TX | Denton, TX (locality) | Core Scientific's site page |
| CoreWeave Muskogee OK | Muskogee County, OK (county) | Oklahoma DEQ's draft construction permit |
| Goodnight | Armstrong County, TX (county) | Crusoe's TCEQ air permit application |
| Google Arcola | Arcola, Loudoun County, VA (locality; a CDP) | Bisnow |
| Google Columbus | Columbus, OH (locality) | Google's Ohio location page |
| Google Lincoln | Lincoln, NE (locality) | Google's Nebraska location page |
| Google Midlothian | Midlothian, Ellis County, TX (locality) | Google's Texas location page |
| Google New Albany | New Albany, OH (locality) | the City of New Albany's economic development news |
| Google Papillion | Papillion, Sarpy County, NE (locality) | Nebraska's air permit fact sheet for Fireball Group, LLC |
| Google The Dalles | The Dalles, OR (locality) | Columbia Community Connection |
| Meta Aiken | Aiken County, SC (county) | Meta's announcement |
| Meta Cheyenne | Cheyenne, WY (locality) | Meta's announcement |
| Meta Eagle Mountain | Eagle Mountain, Utah County, UT (locality) | Meta's post on the data center going online |
| Meta Gallatin | Gallatin, TN (locality) | Tennessee's economic development department |
| Meta Jeffersonville | Jeffersonville, IN (locality) | Turner Construction's release |
| Meta Los Lunas | Los Lunas, NM (locality) | a 2017 New Mexico House memorial |
| Meta Montgomery | Montgomery County, AL (county; locality at Montgomery, its postal city, until 2026-10-09) | Baxtel's news of 2024-05-09 (Meta's announcement names only Montgomery; Baxtel's campus page, cited until the fifth fix round, lists the campus as under construction) |
| Meta Prometheus | New Albany, OH (locality) | Meta's nuclear energy release |
| Meta Rosemount | Rosemount, MN (locality) | Meta's announcement |
| Meta Temple | Temple, TX (locality) | the City of Temple's release |
| Microsoft Goodyear | Goodyear, AZ (locality) | Microsoft's 2019 post on its Arizona campuses |
| Microsoft Project Osmium | West Des Moines, IA (locality) | Business Record |
| Microsoft SAT40 | Bexar County, TX (county) | TDLR's registration for SAT40 |
| OpenAI Stargate Abilene | Taylor County, TX (county; locality at Abilene until 2026-10-09: the Lancium Clean Campus lies outside Abilene's place polygon) | the campus's Title V application to TCEQ |
| QTS Eagle Mountain | Eagle Mountain, Utah County, UT (locality) | Utah DAQ's approval order |
| QTS Richmond 1 | Henrico County, VA (county) | the Henrico Economic Development Authority |

  OpenAI Stargate Abilene's entry also places Crusoe Abilene Expansion, which shares its campus:
  the importer places a shared campus by the entry of its first site in the CSV, so the campus
  has one entry. QTS Richmond 2 and QTS Richmond 3 have none: no source read states their county
  in a sentence (Epoch's Virginia DEQ permit refuses the project's client, and its Wayback
  snapshot could not be reached), so they stay `geocode_failed`.

  On 2026-10-09 the 21 locality entries whose city is also their address's postal city were
  checked against the place polygons, with the OSM objects of the sites (the Overpass snapshot of
  that day, and a few named ways read from Overpass) or the streets of their addresses. Eighteen
  lie inside and carry their `place_check` (Meta Jeffersonville's says what was found: International
  Drive, the street of the IDEM permit's address, lies inside Jeffersonville except its south end,
  91 m outside, and the site itself is not in OSM). Three lie outside, each about 750 m from the
  line, and now give their county: Google Pryor (North) (Google's Oklahoma page), OpenAI Stargate
  Abilene (its Title V application, which names Taylor County) and Meta Montgomery (Baxtel's campus
  page: Meta's own posts name only Montgomery).

  The cited parts, read on 2026-10-09:

| Site | Part | Source |
|---|---|---|
| Google Midlothian | timeline partial: "2019 Google invests in our first Texas data center in Midlothian." | Google's Texas location page |
| Google Arcola | timeline partial: "The company operates a 400K SF data center in Arcola" | Bisnow, 2024-04-29 |
| Meta Sarpy | timeline partial: "2017 Broke ground on the Sarpy Data Center" | Meta's Sarpy fact sheet (Epoch's Selected Source) |
| Meta Jeffersonville | first report 2024-01-25, announced | Turner's release (the location source) |
| Meta Temple | first report 2022-03-31, announced | the Temple Economic Development Corporation's release (the City's copy has no date) |
| Google Fort Wayne | facility status operating on 2025-12-11 | WPTA (21Alive), on Google's announcement |

  The fifth fix round's parts (2026-10-09), from the second seed check's findings: each is the
  earliest dated report the checkers and the entry's author found, read live (no Wayback snapshot:
  web.archive.org reset every connection that evening, so pages that refuse the project's client,
  DCD and KTAR, are not cited). Meta Rosemount, Microsoft Goodyear and Google The Dalles replace
  the first reports above.

| Site | Part | Source |
|---|---|---|
| Google Mesa | first report 2019-07-01, announced | the City of Mesa's release on the council's development agreement |
| Google Kansas City East | first report 2019-07-22, proposed | KSHB on Port KC's bond for Google's Northland data center |
| Google Red Oak | first report 2019-07-18, proposed | The Register (Alamo Mission LLC, State Highway 342) |
| Microsoft Goodyear | first report 2019-04-19, announced | Rose Law Group's repost of the Phoenix Business Journal, quoting Microsoft |
| Google Lincoln | first report 2019-09-09, permitted | the City Council minutes adopting Use Permit 19008 (Epoch's Selected Source) |
| Google The Dalles | first report 2021-10-19, proposed | Columbia Community Connection on the two-data-center proposal |
| Microsoft SAT14 | first report 2021-07-06, proposed | TDLR TABS2021019142's Registration Date (Epoch's Selected Source) |
| OpenAI Stargate Abilene | first report 2021-12-22, announced | Trade & Industry Development on the Taylor County and Abilene approval of Lancium's campus |
| Meta Kuna | first report 2022-02-16, announced | Idaho Power's PUC application, footnote 1 (Epoch's Selected Source) |
| Microsoft Fairwater Atlanta | first report 2022-08-12, announced | Urbanize Atlanta on QTS's Fayetteville campus |
| Meta Aiken | first report 2023-03-22, proposed | the Aiken Standard on Project Sabal's incentive ordinance |
| Meta Rosemount | first report 2023-09-02, proposed | MPR News on the utility filing |
| Meta Cheyenne | first report 2023-11-07, proposed | the Wyoming Tribune Eagle on Project Cosmo's development agreement |
| Vantage TX1 | first report 2023-12-15, proposed (not earlier: Epoch's row stays) | TDLR TABS2024007561 (Epoch's Selected Source) |
| Google Cedar Rapids | first report 2024-03-21, proposed | KCRG on the city's confirmation |
| Goodnight | first report 2024-11-12, announced | Armstrong County's Phase 1 tax abatement agreement (Epoch's Selected Source, a scan) |
| QTS Cedar Rapids | first report 2024-12-06, proposed | MISO's new load list, SNA LLC's 616 MW (Epoch's Selected Source) |
| CoreWeave Lancaster Greenfield site | first report 2025-05-14, proposed (status ahead of Epoch's announcement: a `conflict` item) | AI GridWatch's record of PA DEP's tracker (eFACTS fails TLS verification through the proxy) |
| OpenAI Stargate New Mexico | first report 2025-07, announced | El Paso Matters on BorderPlex's July presentation of Project Jupiter |
| OpenAI Stargate Michigan | first report 2025-10, proposed | the Project Mitten air permit narrative (Epoch's Selected Source) |
| OpenAI Stargate Wisconsin | first report 2025-10-22, announced; milestone construction start 2025-12-17 | Vantage's announcement; The Daily Reporter on the groundbreaking |
| Google Bristow | milestone energized 2023 | the Prince William Times ("which became operational in 2023") |
| CoreWeave Ellendale ND | capacity 175 MW IT ("175 MW now live", 2026-07-01) | Applied Digital's release (Epoch's Selected Source) |
| xAI QTS Atlanta | capacity 20 MW facility ("20 megawatts of total power", 2025-02-20) | Business Insider (Epoch's Selected Source) |
| Microsoft SAT40 | location conflict: Medina County, TX | TDLR EABPRJB8824650 (SAT11-14, Epoch's Selected Source) |

  Meta Hyperion, OpenAI Stargate Lordstown and OpenAI Stargate Milam need no entry: Meta's page,
  the location source, dates its announcement to December 2024, and Epoch's Lordstown note dates
  the two sites' announcement to 2025-09-23.

## Review items

| Kind | Importer | When |
|---|---|---|
| `missing_location` | epoch | no address and no override |
| `geocode_failed` | both | the chain found no location; Epoch: also an address that names no state, and an address whose only place is its postal city (no county a source states), with the refused Census matches in `data.census_matches`; a cited override places the site |
| `unknown_status` | both | Epoch: no timeline row dated today or earlier; first_reported is Epoch's first observation and a Selected Source may date an earlier report (a TDLR registration, a date in a link before the observation), with no `first_report` entry (`data.first_observation`, `sources`; the record is kept); a note dates the start of operation in words the importer cannot read (`data.row`, `note`; the row dates nothing); AGW: a stage the crosswalk does not know (`data.stage`; no record); a past `hearing_date` nothing in the row says was held (`data.hearing_date`; the record has no hearing event); the event log does not tell when the project was first reported (`data.event_date`, `event_kind`, `event_source`: the entry in doubt; the record has no `first_reported` event) |
| `unverified_upstream` | aigridwatch | `verified: false` |
| `possible_duplicate` | aigridwatch | the row is an Epoch site (`record_id` = the Epoch record, `data.matched_by`), or may be one (`weak_link`, `nearby`, `place`, `several`, and `epoch_source` when a rule finds candidates: `record_id` empty, the candidates in `data.epoch_records`), or is AI GridWatch's copy of an Epoch site that has no record (`epoch_copy`: no record id, no candidates); no record. `data.stored_record` names an AI GridWatch record the store already holds for the row. With `data.released`, a reviewer released the row and it is imported |
| `county_mismatch` | aigridwatch | the coordinates are not in the stated state (no record), or not in the named county (the record is placed by the locality text instead; `data.lat`/`lon` are the coordinates not used), or not shown to lie in the city or municipality the locality names (`data.place`; the record keeps the point and does not name it) |
| `conflict` | both | AI GridWatch: the announced date is later than a filing, hearing or decision date (the announcement is left out); the event log or note reports a later milestone than the stage, or leaves no application under review (`data.reported_status`, `event_date`, `event_kind`, `event_source`; held, no record); the row shows the project public before its earliest milestone and no first report can be dated (`data.earliest`, `derived`; held, no record); an outcome `approved` that nothing names a land-use approval or a permit (`data.decided_date`, `entry`; no `approved` event); a stage that implies an application while the note says none was made (`data.stage`, `note_says`; announced); the id starts with another row's name (`data.id_of`; held, no record); a row held as an Epoch record's twin whose stage disagrees with that record's status (`record_id` = the Epoch record, `data.stage`, `as_of`, `epoch_status`, and `support`, the milestone or event-log entry that supports the stage, if any; the stage is not applied and nothing changes). Epoch: Epoch's fields suggest the timeline may not date the facility (a name or cited source mentions an expansion, the name or first row names part of a site, a bitcoin miner owns or hosts the site; the reason says which; its dates and capacity are left out), or a timeline that does not date the facility has tracked buildings that are not operating and no cited facility status (its status is unknown: no record); `data.evidence` quotes the note, title or field, `data.status` is the tracked buildings' status. Also (the record is kept): Epoch's modelled capacity against the MW its note states (`data.epoch`, `stated`; only the phrase is kept), or a cited capacity older than Epoch's latest figures (`data.epoch`, `cited`); the location's cited sources disagree (`data.conflicts`); a cited source states another status than the record's (`data.source_url`, `states_status`, `status`); a cited first report whose status is ahead of the first event (`data.first_report`, `first_report_status`, `first_event`, `first_event_status`; it dates nothing); a cited milestone not applied (`data.problems`) |
| `unit_parse` | aigridwatch | `size_mw` or `acres` is not a number in range, a party field holds a capacity, the operator/developer field names an electric utility (`data.operator`; not imported), or the note gives `size_mw` as a power source's or one phase's (`data.basis`; the record is kept without it, `size_mw` only in `mw_as_stated`) |
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
- `place`: no rule above ties the row, but its locality names the city or municipality an Epoch
  record in its county states, and they may share an organization. Epoch's AWS Berwick is placed by
  its override in Salem Township, at Luzerne County's point (county precision), so no distance rule
  reaches AI GridWatch's `aws-salem-township-pa` ("Salem Township (Luzerne County)", Amazon); QTS
  Salem, in the same township, shares no organization with it;
- `epoch_source` lists, for the reviewer, the sites the location rules, an organization in common
  in the county, or one within 5 km point to (Stargate Abilene: Epoch's OpenAI Stargate Abilene).

A row that is AI GridWatch's copy of an Epoch site (its note quotes "Epoch AI estimates") whose name
is a numbered sibling of every record a rule ties it to is held as `epoch_copy`, naming no record:
its own site has no record yet, and the rule found the sibling. Epoch's QTS Richmond 2 and 3 wait
for an override, and AI GridWatch's copies cite QTS Richmond 1's pages, so `source` tied
`qts-richmond-2` and `weak_link` tied `qts-richmond-3` to QTS Richmond 1; neither is merged,
imported or compared with Richmond 1's status.

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

While the 33 Epoch sites that have only a postal city had no cited override (see
[Location](#location)), they had no record, so their AI GridWatch rows were imported: on the
integrated offline run of 2026-10-09, 47 rows were held as Epoch's sites (37 `name`, 2 `id`, 2
`street`, 5 `weak_link`, 1 `epoch_source`), and `aws-salem-township-pa` and `aws-new-carlisle-in`
were records of their own. With the overrides of 2026-10-09 the same run holds 79 (67 `name`, 2
`id`, 2 `street`, 1 `source`, 4 `weak_link`, 2 `several`, 1 `epoch_source`), `aws-new-carlisle-in`
among them (a `weak_link` to Anthropic-Amazon New Carlisle). `aws-salem-township-pa` stayed a
record beside AWS Berwick, since no rule tied them (other names, and the Epoch record has Luzerne
County's point); with the `place` rule and the `epoch_copy` hold of the fourth fix round, the second
seed's inputs give 80 (67 `name`, 2 `id`, 2 `street`, 3 `weak_link`, 2 `several`, 1 `place`, 2
`epoch_copy`, 1 `epoch_source`). A store that imported a row before its Epoch site was placed holds it on the
next import; the stored AI GridWatch record is then a `removed_upstream` item with
`data.stored_record` for a reviewer to merge.

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
  `stage` (the event log's or note's later milestone, or an obstacle such as a moratorium: the stage
  stands), `id` (the id that starts with another row's name), `scope` (a power supply deal or a
  generation facility) and `first_report` (the row shows the project public before its earliest
  milestone, or that milestone is a denial, a withdrawal or a pause: the reviewer accepts it as
  the first report).
- `first_reported`, optional: the day the reviewer found the project first reported (the source's
  publication day), for a row whose event log does not tell it; it dates a `first_reported` event
  noted "First report dated by a reviewer", and the row is no longer held for it (nor for a first
  report its denial, withdrawal or pause would otherwise date).
- `as_of`, optional: the day the reviewer confirmed the stage of a row that has no `as_of`; the
  stage's `other` event is then dated that day instead of the file's date, and its note says
  "confirmed by a reviewer on …". Like any `other` event it dates nothing.
- `reason` (at most 300 characters, no contact details) and `reviewed_at` are required; an unknown
  key, an empty entry or an `as_of` or `first_reported` after today fails the run (exit 1, nothing
  written).

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
  and verification steps of M2–M3. Only dates, kinds, source links and a few phrases are read: for
  `has_filing`, for a hearing held, for the first report, for whether a decision was a land-use
  approval, and for a later milestone than the stage or an obstacle, which holds the row rather
  than setting a status. The phrases are keyword tests: they can miss a milestone (the stage is then
  published as before) or hold a row the reviewer releases; a first report they cannot date is left
  out, or holds the row when the earliest milestone would date it later than the row shows.
- **No township polygons.** The place polygons say whether a point lies in a city, borough or
  village; for a township or town the import has only the Gazetteer's point and area, so it names
  one unless the point lies in another Pennsylvania or New Jersey municipality or more than 1.25
  equal-area radii from that point. An irregular township can pass with a point just outside it.
- **AI GridWatch stage dates.** The stage's `other` event says "status observed on date X, event
  date unknown": it is dated by `as_of`, the day AI GridWatch read its source, or for a row without
  `as_of` by the file's date. `derive_dates` skips `other` events for `first_reported`,
  `operating_since` and `cancelled`, so a row whose milestones do not reach the stage has none of
  those dates.
- **AI GridWatch MW.** Most rows state no basis for `size_mw`, so most records carry the figure
  only in `mw_as_stated` and count as "without MW" on the map and in the totals until a source
  states the basis.
- **Epoch's `first_reported` can only be as early as the importer's inputs.** Epoch's first row is
  what its imagery or a filing first shows; the importer reads no Selected Source, so beyond the
  dates Epoch's own notes and an entry's quotes give, a reviewer's `first_report` entry dates the
  first report (23 on 2026-10-09), and a site without one keeps Epoch's first observation, which
  its note says. Only the sites whose sources hint at an earlier date (a TDLR registration, a dated
  link) are held for review; for the others a reviewer's check is the safeguard.
- **Epoch projections that do not change the mapped status are dropped.** A projected row whose
  mapped status equals the previous row's adds no event. The crosswalk reads "Buildings
  operational" and, when that cell is empty, "IT power (MW)": OpenAI Stargate Milam's 2028-12-31
  "site is fully operational" row (no count, 857 MW) is a planned `operating` event.
- **Census coverage.** Fewer than half of Epoch's street addresses give an agreeing match (new
  industrial roads often are not in the address ranges yet, and a match on another street is
  refused). The rest have only a postal city, which gives no point without a county a source
  states: those sites wait in review for a cited override (33 on 2026-10-08, 2 since the
  overrides of 2026-10-09), and AI GridWatch's rows for them are imported meanwhile (no Epoch
  record holds them).

## Fifth fix round, 2026-10-09 (Epoch AI)

Epoch importer version 5, from the second check of the round-4 seed (71 of its 73 Epoch records
checked; 29 had an error under the cited or the today standard, 23 of them a first report dated by
Epoch's first observation). On the same saved inputs (`data_centers.zip` of 2026-10-09, sha256
`0ce2b5e4…4ccab47`, the same Census cache), offline, into an empty store: 73 records (73
before), `atlas validate` 0 issues, a second run `unchanged`.

- **First reports:** 24 records date `first_reported` from a report (5 before): 22 cite one (21
  committed `first_report` entries and Meta Hyperion's location quote) and 2 an announcement
  Epoch's own note dates (OpenAI Stargate Lordstown and Milam, 2025-09-23). 15 keep Epoch's first
  observation, which their note now says; Vantage TX1's entry records that no cited source is
  earlier.
- **Capacity:** 2 cited capacities replace Epoch's model (CoreWeave Ellendale ND 175 MW IT, xAI QTS
  Atlanta 20 MW facility), with Epoch's figures in `field_meta` conflicts.
- **Dates:** Microsoft-Nebius New Jersey's operation dates from its note (`2026-Q1`, not
  `2026-04`); 2 cited milestones (Google Bristow operating since 2023; OpenAI Stargate Wisconsin's
  groundbreaking 2025-12-17).
- **Sources and notes:** 3 Selected Sources past the five-link cap are cited because a note links
  or names them (Colossus 2's S-1 filing, Meta Kuna's third PPA notice, SemiAnalysis for
  Microsoft-Nebius New Jersey); no note holds a URL (CoreWeave Chester VA and Dalton 1 & 2 ended in
  a cut one); Meta Montgomery's location source no longer states a contrary status; Microsoft
  SAT40's location conflict is in `field_meta`.
- **Review items:** 18 (16 before): a `conflict` for SAT40's location conflict and one for CoreWeave
  Lancaster Greenfield site, whose cited PA DEP application (proposed, 2025-05-14) is ahead of
  Epoch's announcement and dates nothing until a reviewer decides.

## Fourth fix round, 2026-10-09

The fourth fix round's importers, integrated (OSM version 3, Epoch version 4, AI GridWatch version
3), on the second seed's saved inputs (Epoch's `data_centers.zip` and AI GridWatch's `projects.json`
of 2026-10-08, sha256 `16330d94…4ccab47`, the same Census cache), offline, into an empty store after
the OSM import (985 records): `atlas validate` over the store 1,239 records, 0 issues, and a second
run of each importer `unchanged`.

Epoch AI: 73 records (73 before), 43 placed by an override, 20 timelines that do not date the
facility and 14 held (14 and 10 before), 5 cited first reports, 1 cited facility status (Google Fort
Wayne) and 16 review items (14 `conflict`, 2 `geocode_failed`; 13 before). No Census request, and
no record at county precision names a city or municipality (AWS Berwick did).

AI GridWatch: 181 records (191 before) and 220 review items (170 before). Its Google Fort Wayne
row is still held as the Epoch record's twin, but its stage no longer disagrees with that record,
which is now operating, so the stage conflict is gone. Ten rows the second seed published are held
now: Plaza 500 and Mason County (no first report can be dated, `conflict`), Meta Lebanon, Wolcott,
Google Haskell County and Stratos (the log or note reports construction, or an approval a lawsuit
seeks to undo), Urbana, Cave City and Lyon Township's Project Flex (a moratorium after the filing;
Urbana's application also refused as incomplete) and `aws-salem-township-pa` (`place`, AWS
Berwick); QTS Richmond 2 and 3 stay held, as `epoch_copy` instead of Richmond 1's twins. 49 records carry
an event-log first report (58 before; 11 `unknown_status` items say where none could be told), 23
`county_mismatch` items (21 a place the point is not shown to lie in), 5 approvals no longer
imported, 3 stages read as announced for want of an application, and no AI GridWatch record has an
`operator` (PSE&G is left out).

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
and 89 AI GridWatch review items. The table predates the 30 overrides of 2026-10-09: with them,
the same Epoch run gives 73 records (43 placed by an override) and 13 review items (11
`conflict`; `geocode_failed` only for QTS Richmond 2 and 3), and AI GridWatch on top of it 191
records, 79 rows held as Epoch's sites and 170 review items (264 records, 0 issues). The figures
after the fourth fix round are in [Fourth fix round, 2026-10-09](#fourth-fix-round-2026-10-09).

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
