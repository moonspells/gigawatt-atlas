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
| `announced` | Proposed with no known filing; In review, Hearing scheduled or Awaiting decision with no known filing when the note says nothing was formally proposed; Rumored (with `evidence_level: "rumor"`) | pre-construction text only | `proposed:telecom=data_center`, `proposed:building=*`, `proposed=*` (no OSM tag is a filing) | press or company announcement |
| `proposed` | Proposed with a filing, In review, Hearing scheduled, Awaiting decision | n/a | n/a | application filed, TABS registration, DRI submitted |
| `permitted` | Approved | n/a | n/a | rezoning approved, permit issued |
| `under_construction` | Under construction | construction text, or anything not clearly pre-construction, with 0 buildings operational | `construction:telecom=data_center`, `construction=data_center`, `construction:building=*`, `building=construction`, `landuse=construction` | building permits plus construction reports |
| `operating` | Operating | buildings operational > 0 (IT power > 0 when the count is empty) | `telecom=data_center`, `building=data_center`, or `industrial=data_centre` on an object with a building tag, with no lifecycle tag (assumed) | n/a |
| `paused` | Blocked by ban | n/a | n/a | moratorium, regulatory pause |
| `denied` | Denied | n/a | n/a | application denied |
| `cancelled` | Withdrawn | n/a | n/a | withdrawal |

PA DEP and EPA ECHO status values are mapped at M4, when those importers land.

## OpenStreetMap (`from_osm_tags`)

Confidence 0.60. Every OSM status is an `other` event: the OSM importer (`atlas/sources/osm.py`)
writes it at the snapshot date, and it sets the status and dates nothing, because OSM says what a
feature is, not when its status began (07 §2.3; `derive_dates` skips `other` events). A
`start_date` becomes an `unverified_upstream` review item, not an `energized` date. `opening_date`
on a non-operating group adds a planned `energized` event unless its period has passed, in which
case it is a `conflict` item. The proposed rules 1 to 3 below read as `announced`, because no OSM
tag is a filing (07 §2.3): `proposed=yes` on an anonymous lot in Manchester Township, NJ (way
1549253250) was published as `proposed` before the third review round.

An object is a data center when `telecom`, `building`, `construction:telecom`, `proposed:telecom`,
`construction`, `construction:building` or `proposed:building` is `data_center`, or when it has a
building tag (`building`, `proposed:building` or `construction:building`, other than `no`) and
`industrial=data_centre` (or `data_center`): "IBM Quantum Data Center", way 195803647, is tagged
only `building=industrial` and `industrial=data_center`. Anything else has no mapping (`None`); a
site polygon tagged `industrial=data_centre` without a building tag is a campus container OSM does
not say is built, so it has no status and the importer holds it for review. Lifecycle tags are read before `telecom=data_center`, so a site OSM tags as
planned or being built is never shown as operating. The first matching rule wins; `*` is any value
except `no`:

| Order | Tags | Status | Label |
|---|---|---|---|
| 1 | `proposed:telecom=data_center` | `announced` | the tag, for example `proposed:telecom=data_center` |
| 2 | `proposed:building=*` | `announced` | `proposed:building=industrial` |
| 3 | `proposed=*` | `announced` | `proposed=yes` |
| 4 | `construction:telecom=data_center` | `under_construction` | `construction:telecom=data_center` |
| 5 | `construction=data_center` (usually with `building=construction`) | `under_construction` | `construction=data_center` |
| 6 | `construction:building=*` | `under_construction` | `construction:building=yes` |
| 7 | `building=construction` | `under_construction` | `building=construction` |
| 8 | `landuse=construction` | `under_construction` | `landuse=construction` |
| 9 | `telecom=data_center` | `operating` (assumed) | `telecom=data_center` |
| 10 | `building=data_center` | `operating` (assumed) | `building=data_center` |
| 11 | `industrial=data_centre` or `industrial=data_center`, with a building tag | `operating` (assumed) | `industrial=data_centre` |

When an object carries both a proposed and a construction tag, it is announced: the less advanced
status, so the map never overstates progress. Before review round 1 only rules 1, 4, 5, 9 and 10
existed, and 24 records of a full OSM import on 2026-10-07 were operating although OSM tagged every
member `building=construction` (14), `landuse=construction` (1), `proposed:building=*` (7) or
`proposed=yes` (2); `tests/test_crosswalk.py` checks one of each.

## AI GridWatch (`from_aigridwatch_stage`)

Confidence 0.70. Stages are matched ignoring case and extra spaces. `has_filing` is true when the
project has a `rezoning_filed` date the row shows is a filing, an `announced` date the row explains
as a filing, or an event-log entry that is one of its own applications: of kind `filing` or
`rezoning`, with a source that is not a placeholder link, about an application, a petition, a
request, a plan or a permit, and not about the place's rules (an ordinance, a moratorium, a text
amendment, a resolution, fees), a property deal or someone else's lawsuit, appeal or motion
(`aigridwatch.own_filing`; Posey County's only `rezoning` entry is its Area Plan Commission revising
its own data center ordinance). The rule may come in as context: an entry whose first clause files
an application and names no rule is one ("Pronghorn Development LLC submitted a conditional use
permit application in November 2025 ..., months after the county amended its zoning ordinance").
A `rezoning_filed` date is no filing when the note says only an inquiry was made (Abei Energy
"emailed the Starke County Plan Commission asking about rezoning two parcels"), or when its entry
of that day is the municipality's own procedure (Smithfield Township's curative amendment
resolution, "180-day MPC review period begins"; `aigridwatch.rezoning_filing`). `no_application`
is true when the row's note, or an event-log entry about its project, says nothing has been
formally proposed ("despite no official proposal", "No formal application", "no permit applications
before January 2027", "no formal application or site review had been submitted", "will not submit
permit applications"), or the note says only an inquiry was made (`aigridwatch.no_application`).

| Stage | Status | Event | Evidence | Reason |
|---|---|---|---|---|
| Rumored | `announced` | `announced` | `rumor` | |
| Proposed, no filing | `announced` | `announced` | | |
| Proposed, with a filing | `proposed` | `application_filed` | | |
| In review, Hearing scheduled or Awaiting decision, with no filing and `no_application` | `announced` | `announced` | | |
| In review | `proposed` | `application_filed` | | |
| Hearing scheduled | `proposed` | `application_filed` | | |
| Awaiting decision | `proposed` | `application_filed` | | |
| Approved | `permitted` | `approved` | | |
| Under construction | `under_construction` | `construction_start` | | |
| Operating | `operating` | `energized` | | |
| Blocked by ban | `paused` | `paused` | | `moratorium` |
| Denied | `denied` | `denied` | | `local_denial` |
| Withdrawn | `cancelled` | `withdrawn` | | none (see below) |

Any other stage raises `UnknownStatus`; the importer turns it into an `unknown_status` review item.
07 §2.3 makes `proposed` a pending formal application, so a stage that implies one (In review,
Hearing scheduled, Awaiting decision) is `announced` when the row states no filing and its note or
its log says nothing has been formally proposed, and the importer files a `conflict` item for a
reviewer (Posey County's commissioners signed an NDA "despite no official proposal" while the stage
read Awaiting decision; Project Zora, In review, "with no permit applications before January
2027").

The event above is what the stage means when the row's milestone dates reach it. A `decided_date`
with outcome `approved` is an `approved` event only when the row names it a land-use approval or a
permit (a performance agreement or a tax abatement is no approval to build;
[epoch-aigridwatch.md](sources/epoch-aigridwatch.md#status-history)). When the milestones do not
reach the stage (no milestone, or one with another status), the importer records the stage as an
`other` event with the stage's status instead, dated with the row's `as_of`, or, for a row without `as_of`, with
the file's `generated` date (owner decision of 2026-10-09). Like an OSM tag, it is an observation:
it sets the status and dates nothing (`derive_dates` skips `other` events). A row whose own event
log or note reports a milestone the stage has not reached, or leaves no application under review (an
application refused as incomplete, a moratorium adopted after the filing), stays held for review
with a `conflict` item, and so does an Under construction row whose log reports an injunction or a
halt of its works that no later entry lifts, and a row read as `announced` with no filing that a
ban or moratorium adopted after it was public (and not ended) blocks
([epoch-aigridwatch.md](sources/epoch-aigridwatch.md#status-history)).

"Withdrawn" does not say who withdrew. On 2026-10-08, 13 rows read Withdrawn: in some the developer
withdrew ("Deep Green withdrew its rezoning request"), in others a mayor dropped his support, a host
agreement lapsed or a court voided the rezoning. The crosswalk gives no `status_reason`, and the AI
GridWatch importer sets one from the row's note and its `withdrawal` entries
(`aigridwatch.withdrawal_reason`): `litigation` when they cite a court ruling ("judicially voided"),
`developer_withdrawal` when they name the developer, the applicant or one of the row's parties as the
one who withdrew ("Karis notified the village … that it would withdraw"), else none.

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
EPOCH_CONSTRUCTION     \b(land clearing|land cleared|clearing|cleared|grading|civil works|
                       construction|foundation|groundbreaking|ground broken|site work|
                       excavat\w*|steel|roof|built|conversion|retrofit\w*|installed|
                       complete[d]?)\b
EPOCH_PRE_CONSTRUCTION \b(announc\w*|planned|proposed|permit application|rezoning|acquired|
                       purchased)\b
```

So "Announced in January; grading has begun" is under construction (construction wins), and text
that matches neither pattern also defaults to under construction: an Epoch row with no operational
building describes a site where work is visible.

The Epoch importer passes the text with its markdown links reduced to their text, so a URL never
decides the status, and keeps `construction_start` only for a row whose note says construction
starts; other rows observe work under way (`first_reported` or `other`, see
[epoch-aigridwatch.md](sources/epoch-aigridwatch.md#status-timeline)). On the 548 rows of
2026-10-08 the regex change alters no record.

## How events become the current status

Importers add one event per upstream status change; `atlas.schema.rollup.apply_rollup` then sets
`status` and the derived `dates` (07 §2.3): the latest non-planned event wins for a record without
phases; with phases, the most advanced active phase wins, and a stopped status applies only when
every phase is stopped. `atlas validate` fails a record whose `status` or `dates` disagree with its
events.
