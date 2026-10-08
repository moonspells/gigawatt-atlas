# Status crosswalk

How upstream status values become Atlas statuses and `status_history` events (07 §4.7). This page
describes `atlas/crosswalk.py`; `tests/test_crosswalk.py` checks every row.

Each mapping returns a `Crosswalked` value: `status`, `event`, `evidence_level` (set only when the
upstream value implies one), `status_reason`, `confidence` and `label` (the upstream value as
given). Importers put the label in the event's `note` or the review item, so a reviewer can see what
upstream said.

## The eight statuses

| Atlas status | AI GridWatch stage | Epoch timeline row | OSM tag | Agenda or permit outcome (M2+) |
|---|---|---|---|---|
| `announced` | Proposed with no known filing; Rumored (with `evidence_level: "rumor"`) | pre-construction text only | n/a | press or company announcement |
| `proposed` | Proposed with a filing, In review, Hearing scheduled, Awaiting decision | n/a | `proposed:telecom=data_center`, `proposed:building=*`, `proposed=*` | application filed, TABS registration, DRI submitted |
| `permitted` | Approved | n/a | n/a | rezoning approved, permit issued |
| `under_construction` | Under construction | construction text, or anything not clearly pre-construction, with 0 buildings operational | `construction:telecom=data_center`, `construction=data_center`, `construction:building=*`, `building=construction`, `landuse=construction` | building permits plus construction reports |
| `operating` | Operating | buildings operational > 0 (IT power > 0 when the count is empty) | `telecom=data_center` or `building=data_center` with no lifecycle tag (assumed) | n/a |
| `paused` | Blocked by ban | n/a | n/a | moratorium, regulatory pause |
| `denied` | Denied | n/a | n/a | application denied |
| `cancelled` | Withdrawn | n/a | n/a | withdrawal |

PA DEP and EPA ECHO status values are mapped at M4, when those importers land.

## OpenStreetMap (`from_osm_tags`)

Confidence 0.60. OSM events are `first_reported` at the snapshot date, except `energized` at a
valid `start_date` (1990 or later) for operating objects; `opening_date` on a non-operating group
adds a planned `energized` event (`atlas/sources/osm.py`).

An object is a data center when `telecom`, `building`, `construction:telecom`, `proposed:telecom`,
`construction`, `construction:building` or `proposed:building` is `data_center`; anything else has
no mapping (`None`). Lifecycle tags are read before `telecom=data_center`, so a site OSM tags as
planned or being built is never shown as operating. The first matching rule wins; `*` is any value
except `no`:

| Order | Tags | Status | Label |
|---|---|---|---|
| 1 | `proposed:telecom=data_center` | `proposed` | the tag, for example `proposed:telecom=data_center` |
| 2 | `proposed:building=*` | `proposed` | `proposed:building=industrial` |
| 3 | `proposed=*` | `proposed` | `proposed=yes` |
| 4 | `construction:telecom=data_center` | `under_construction` | `construction:telecom=data_center` |
| 5 | `construction=data_center` (usually with `building=construction`) | `under_construction` | `construction=data_center` |
| 6 | `construction:building=*` | `under_construction` | `construction:building=yes` |
| 7 | `building=construction` | `under_construction` | `building=construction` |
| 8 | `landuse=construction` | `under_construction` | `landuse=construction` |
| 9 | `telecom=data_center` | `operating` (assumed) | `telecom=data_center` |
| 10 | `building=data_center` | `operating` (assumed) | `building=data_center` |

When an object carries both a proposed and a construction tag, it is proposed: the less advanced
status, so the map never overstates progress. Before review round 1 only rules 1, 4, 5, 9 and 10
existed, and 24 records of a full OSM import on 2026-10-07 were operating although OSM tagged every
member `building=construction` (14), `landuse=construction` (1), `proposed:building=*` (7) or
`proposed=yes` (2); `tests/test_crosswalk.py` checks one of each.

## AI GridWatch (`from_aigridwatch_stage`)

Confidence 0.70. Stages are matched ignoring case and extra spaces. `has_filing` is true when the
project has a `rezoning_filed` date or an event of kind `filing` or `rezoning`.

| Stage | Status | Event | Evidence | Reason |
|---|---|---|---|---|
| Rumored | `announced` | `announced` | `rumor` | |
| Proposed, no filing | `announced` | `announced` | | |
| Proposed, with a filing | `proposed` | `application_filed` | | |
| In review | `proposed` | `application_filed` | | |
| Hearing scheduled | `proposed` | `application_filed` | | |
| Awaiting decision | `proposed` | `application_filed` | | |
| Approved | `permitted` | `approved` | | |
| Under construction | `under_construction` | `construction_start` | | |
| Operating | `operating` | `energized` | | |
| Blocked by ban | `paused` | `paused` | | `moratorium` |
| Denied | `denied` | `denied` | | `local_denial` |
| Withdrawn | `cancelled` | `withdrawn` | | `developer_withdrawal` |

Any other stage raises `UnknownStatus`; the importer turns it into an `unknown_status` review item.

## Epoch AI (`from_epoch_row`)

Confidence 0.70. Input: a timeline row's construction-status text, its "buildings operational"
count and its "IT power (MW)".

1. Buildings operational > 0, or, when that cell is empty, IT power > 0: `operating`, event
   `energized`. IT power in the timelines is the power online at that date: in the 547 rows of
   2026-10-07 every row with buildings operational > 0 has IT power > 0 and every row with 0 has 0.
   The one operational row with an empty count (OpenAI Stargate Milam, 2028-12-31, "site is fully
   operational", 857 MW) used to fall through to under construction.
2. Otherwise, text that matches `EPOCH_PRE_CONSTRUCTION` and does not match `EPOCH_CONSTRUCTION`:
   `announced`, event `announced`.
3. Otherwise: `under_construction`, event `construction_start`.

Both patterns are case-insensitive and match whole words:

```text
EPOCH_CONSTRUCTION     \b(land clearing|clearing|grading|civil works|construction|foundation|
                       groundbreaking|ground broken|site work|excavat\w*|steel|roof|built|
                       conversion|retrofit\w*|installed|complete[d]?)\b
EPOCH_PRE_CONSTRUCTION \b(announc\w*|planned|proposed|permit application|rezoning|acquired|
                       purchased)\b
```

So "Announced in January; grading has begun" is under construction (construction wins), and text
that matches neither pattern also defaults to under construction: an Epoch row with no operational
building describes a site where work is visible.

## How events become the current status

Importers add one event per upstream status change; `atlas.schema.rollup.apply_rollup` then sets
`status` and the derived `dates` (07 §2.3): the latest non-planned event wins for a record without
phases; with phases, the most advanced active phase wins, and a stopped status applies only when
every phase is stopped. `atlas validate` fails a record whose `status` or `dates` disagree with its
events.
