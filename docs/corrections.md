# Cited record corrections

How a reviewer's decision about a record is written down, applied on every import and cited. The
shared code is `atlas/sources/corrections.py` (the `Fix` model, the record ops, `apply_fixes`,
`check_entries`) and `merge_import` in `atlas/sources/base.py`; `tests/sources/test_corrections.py`
tests both. The decisions live in `config/overrides/{source}.json`, one file per importer
(`osm.json`, `epoch.json`, `aigridwatch.json`), and go through a reviewed PR like any other change.

Records are rebuilt from their sources on every run, and a seed run on an empty store gives them
new ids, so an entry is keyed by the source's own key: an OSM ref (`way/818167571`), an Epoch AI
site name (`Amazon Ridgeland`) or an AI GridWatch row id (`king-of-prussia-mlp-pa`). Editing a
record file by hand does not last: the next import overwrites it.

## Two layers

An entry has two layers, with the same rule in all three files:

- **Top-level keys** change what the source says *before* the importer builds. They are the
  source's own: OSM's element decisions (`drop`, `name`, `operator`; `docs/sources/osm-pnnl.md`
  section 9), Epoch AI's cited location, timeline, first report, facility status, capacity and
  milestones (`docs/sources/epoch-aigridwatch.md`, "Overrides policy"), and AI GridWatch's
  releases of held rows ("Releasing a held row" there). A file without the new keys is read as
  before, byte for byte.
- **`corrections`**, a list of `Fix`es, changes the record *built* from the source, with the shared
  ops below, applied after the build. Each Fix carries its own citation, so one record can be
  corrected from several pages: Crown Castle Baltimore's operator from Crown Castle's own page, its
  name from PeeringDB.

## A Fix

| Field | Meaning |
|---|---|
| `source_url` | the page that states the correction |
| `archive_url` | the Wayback snapshot read when the page refuses automated clients (optional) |
| `publisher` | who published the page (1 to 200 characters) |
| `source_type` | its 07 §3.4 type; when absent, `classify_source(source_url, title)` gives it |
| `title` | the page's title (optional, at most 300 characters) |
| `published_at` | the page's own date, when it states one (optional) |
| `quote` | the verbatim span of the page (or of `archive_url`) that states the correction, 1 to 300 characters |
| `retrieved_at` | when the page was read; never after the run's now, and its day never after `reviewed_at` |
| `states_status` | the status the page gives as current, when it gives one (optional) |
| `basis` | where the error came from (below) |
| `reason` | why, in at most 300 characters, with no contact details |
| `reviewed_at` | the day of the decision, never after today |
| `issue_url` | the review PR or issue, copied to `record.corrections[]` (optional) |
| `ops` | at least one record op |

`basis` tells override work from importer work, which the 07 §6.7 sample gate needs:

- `out_of_date`: the source was right when it was written, and the facts changed since (an
  operator that has since sold the site);
- `upstream_error`: the source itself is wrong (an OSM tag, an AI GridWatch date);
- `importer_gap`: the source supports the right value, but the importer cannot read it yet. Such a
  Fix must come with an importer fix; the receipt counts them (`corrections_importer_gap`) and the
  count should fall to 0.

A Fix does not load, and the import stops, when a key or an op is unknown, a value has the wrong
type, a citation field is missing, the quote is empty or longer than 300 characters, the quote,
reason, publisher, title or an op's text has a control or invisible character or looks like an
email address or phone number (records hold organizations only, 07 §5.3), or a paused, denied or
cancelled `add_event` or `observe` cites a source that is not a government record, regulator,
utility or ISO filing, SEC filing, company release, trade press or news (07 §6.4 gate 6).
`check_fix_dates` stops it when `reviewed_at`, `published_at` or a non-planned event date is after
today, or `retrieved_at` after now.

## The ops

Every op that replaces a value names the value it replaces (`from`), and an event op finds its
event by what it says (`match`), never by its place in the list. The record is checked as it
stands just before the op, so a second op on a value the first changed names the new value. A
guard that no longer matches is *stale*: the entry must be reviewed, and the run stops. Values are
compared as the record holds them, except that a list of organizations compares as its names and a
fuzzy date as its value string (`"2026-08"`).

| op | Fields | Stale when | Effect |
|---|---|---|---|
| `hold` | `kind`: `conflict` (default), `unknown_status`, `possible_duplicate`, `unverified_upstream` | never | `scope` becomes `out_of_scope` (never published; a stored record is unpublished), and a review item of `kind` gives the reason, URL and quote. On a record already out of scope, only the item. |
| `out_of_scope` | none | never | The same with an `out_of_scope` item: a decision (07 §2.2), not a doubt. For a whole site of many elements; OSM's element `drop` takes one element out instead. |
| `set` | `path`, `phase` (optional), `from`, `value` (null removes) | the value is not `from` | Sets one value. The paths are `/location/street`, `/location/postcode`, `/capacity/{it_mw, facility_mw, utility_request_mw, backup_generation_mw, mw_as_stated}`, `/site/{acreage, building_sqft, building_count}`, `/money/{investment_usd, investment_basis, currency_year}`, `/purpose` and `/dates/expected_in_service` (a fuzzy date string). With `phase`, `path` is a `/capacity/...` path of that phase. City, county and coordinates change only through `set_location`; ids, the status, the event dates, sources, external ids and review never. |
| `rename` | `from` (the base name now), `base`, `old_as_alias` (an alias kind, optional) | the base name is not `from` | The base name: the place suffix stays derived. With `old_as_alias`, the old base becomes an alias citing the sources that supported the name. An alias that equals the new base is dropped. |
| `add_alias` / `remove_alias` | `name`, `kind` / `name` | the alias exists / is absent | Adds it, citing the Fix / removes it. |
| `set_party` | `role`, `from` (the names now), `names` | the names are not `from` | Replaces the role's organizations, each citing the Fix; `[]` removes them all. |
| `add_party` / `remove_party` | `role`, `name` | present / absent | Adds one, citing the Fix / removes one. |
| `rename_party` | `role`, `from`, `to` | no organization is named `from` | Renames it; it cites its old sources (they support the role) and the Fix (the name). |
| `set_location` | `from` (some location fields as they are now), `state_abbr`, `county_fips`, `city`, `municipality`, `lat`, `lon`, `precision`, `place_check`, `street`, `postcode` | a `from` field differs | Places the record by Epoch's override rules (`place_location`, 07 §6.5): a county at its centroid, a city at its Gazetteer place, a municipality at its county subdivision, a point checked against the county or state. A county names no city. Street and postcode only at street precision or better; otherwise they, `geometry_ref` and `parcel_apns` are cleared. |
| `add_event` | `event`, `status`, `as_of`, `phase`, `planned` (default false), `note`, `status_reason` | an event with the same event, date and phase exists | Adds the event, citing the Fix. `status_reason` only with a paused, denied or cancelled status, and only when the record ends with that status. |
| `replace_event` | `match`, `set` {`event`, `status`, `as_of`, `planned`, `note`} | not exactly one event matches | Changes it in place. A new date or status makes the Fix its only source, and the old event goes to `field_meta["/status_history"]` conflicts; a retype (an announcement that is a first report, an event that is only an observation, `other`) keeps its sources and adds the Fix. |
| `remove_event` | `match` | not exactly one event matches | Removes it; it is kept in `field_meta["/status_history"]` conflicts with its sources. |
| `observe` | `status`, `as_of` (required: the day the page shows the status), `phase` | the same observation exists | Adds an `other` event: it sets the status and dates nothing. |
| `first_report` | `from` (`dates.first_reported` now, or null), `as_of`, `status` (`announced`, `proposed` or `permitted`) | `from` differs, or `as_of` is not earlier than every dated event | The record's first-report events become `other`, and a `first_reported` event citing the Fix is added. A wrong first-report date is corrected with `replace_event`, not with this op. |
| `support` | `path` and `from`, or `event` (a match) | the value is not `from`, or not exactly one event matches, or the Fix already cites it | Cites the Fix for a value the record has, without changing it. |

An event match (`match`, `event`) has `event`, `status`, `as_of`, `phase` and `planned`, and names
at least the event or the status. OSM observations are dated with each snapshot, so match them by
`{"event": "other", "status": ...}`, never by `as_of`, or every weekly run is stale. Match AI
GridWatch's observations by status and date, and Epoch AI's events by event and date.

The entries of one record are applied in the source's key order (OSM refs in ref order, Epoch
site names, AI GridWatch ids), each entry's Fixes in file order and each Fix's ops in order. Two
entries that rename one record therefore fail on the second `from`: one decision per record.

## Provenance

For each Fix, `apply_fixes` works on the record's JSON:

1. **The source.** A source of the record with the same URL (as `HttpUrl` normalizes it) is reused
   when it has no quote or the same quote: it takes the Fix's quote with `quote_match: "human"`,
   its `retrieved_at`, `publisher` and `archive_url`, its `source_type` when the Fix gives one, and
   `published_at` when it had none. Citing the OSM way's own URL reuses the element's source;
   Epoch's SemiAnalysis link (s7 of Microsoft-Nebius New Jersey) takes the Fix's quote. Otherwise
   a new source `s{n+1}` is added. The same URL with another quote is another source.
2. **What it supports.** `set`, `rename` (`/canonical_name`), `set_location` (`/location`) and
   `support` add their pointer to the source's `supports`. Events, aliases and organizations cite
   it in their own `source_ids` (07 §3.3 rule b). A phase's capacity is `/phases/{i}/capacity/...`;
   phases are never reordered.
3. **Narrowing.** Every other source whose `supports` entry covers a corrected value has that entry
   rewritten into the values beside it, level by level (`narrow_supports`), and an entry inside the
   corrected value is dropped. AI GridWatch's `/status_history` becomes `/status_history/0`,
   `/status_history/1` when a third event is added; Epoch's `/parties` becomes `/parties/tenant/1`
   when tenant 0 is renamed; OSM's s1 loses `/canonical_name` on a rename. So no importer source
   claims a value it does not state. A removed list item shifts the later pointers down in
   `supports` and `field_meta` (`remap_list_pointers`).
4. **`field_meta`.** `set`, `set_party`, `set_location` and `rename` write `field_meta[pointer]`:
   `method: "human_entered"`, the Fix's source, the confidence below, and in `conflicts` the old
   value with the sources that supported it. `set_location` replaces every key under `/location`.
   A narrower key beats a broader one: AI GridWatch's `field_meta["/location"]` stays when
   `/location/postcode` gets its own entry (`is_supported` accepts either).
5. **`record.corrections[]`**, one entry per op: `{on: reviewed_at, pointer, old, new, reason,
   issue_url}`. The pointers carry no list index: `/location/postcode`, `/canonical_name`,
   `/aliases`, `/parties/{role}`, `/location`, `/status_history` (old and new are event summaries),
   `/scope`, `/phases/{phase_id}/capacity/...`. Each run writes the same entries, and
   `merge_import` keeps the stored ones and adds the new ones, so the history is append-only (07
   §5.4). Withdrawing a wrong correction leaves its entry: record the revert in the review PR.

### Confidence (07 §3.4)

`human_confidence`: 0.95 for a value a reviewer entered, plus the source adjustment (government
record or regulator +0.10, utility or ISO filing and SEC filing +0.07, company release +0.05,
trade press, news and open dataset 0, commercial aggregator -0.10, community report -0.20), minus
0.20 when a value it replaces came from a source type higher in 07 §3.4's order (government
record > utility or regulator filing > SEC filing > company release > trade press > news > open
dataset > commercial aggregator > community report), clamped to 0.05 to 0.99. Ops that add
rather than replace (`add_*`, `observe`, `first_report`, `support`) have no conflict term.

Read literally, 07 §3.4 gives a commercial aggregator's correction of an OpenStreetMap value
0.95 - 0.10 - 0.20 = 0.65 (medium): Baxtel naming Aligned's IAD campus. Whether a reviewer's
decision should be exempt from the conflict term is an owner question; the code follows 07 §3.4
as written. Epoch AI's legacy entry parts keep their own "stated" base of 0.85.

## Derived fields

After the ops, the derived fields are recomputed, never patched:

1. `canonical_name` is the base name plus `name_suffix` of the final location: ` ({city, else
   municipality, else the county's full name}, {ST})`, or ` ({ST})`. The base is the built name
   without the suffix of the location it was built with; a built name that does not end with that
   suffix stops the run. So a `set_location` to Laramie County renames "Microsoft Cheyenne
   Expansion (WY)" to "Microsoft Cheyenne Expansion (Laramie County, WY)" with no `rename`.
2. `apply_rollup` gives the status and the event dates; `other` events date nothing, so
   observations and retyped events drop out of `first_reported`, `operating_since` and
   `cancelled`. `expected_in_service` is kept.
3. `status_reason` is the op's when the final status is that op's stopped status, none when the
   status is active, and otherwise the importer's.
4. `record_type`, through the importer's hook: Epoch AI's record is a `campus` once a building
   operates (Amazon Ridgeland returns to `project`), OSM's is always a campus and AI GridWatch's a
   project.
5. `field_meta["/status"]`: when the event that decides the status (the latest one, or the
   deciding phase event when the record has phases) cites a Fix's source through an event op, it
   becomes `human_entered` with that event's sources; otherwise it stays as built.
6. The record is checked against the schema and `validate_record`; any issue stops the run and
   names the entry.

`evidence_level` is not recomputed, and `review.state` stays `"machine"`: `merge_import` holds
every record whose state is not `machine`, which would freeze a corrected record against its own
importer. The reviewer's decision is recorded by the source's `quote_match: "human"`, the
`human_entered` field_meta and `corrections[]`.

## Review items

`hold` and `out_of_scope` file one item each, with `data: {source_url, quote, reviewed_at, basis,
file}`. The other ops file none: the record's `corrections[]` and the PR diff show them. The items
the importer files for a key whose corrections were applied get `data.corrected_by`,
`"config/overrides/{source}.json (reviewed {date}): {reason}"` (`mark_corrected`), so the review
page shows the decision; items are annotated, never dropped. No review kind is new.

## When an entry stops the run

Every check runs inside the importer, before `merge_import`, so `atlas import` exits 1 and writes
nothing (`--dry-run` behaves the same):

| When | What stops it |
|---|---|
| Loading | unreadable or invalid JSON; an unknown key or op; a bad type or key; a missing citation field; a quote, reason or value as above; a `set` path outside the list; `status_reason` with an active status; a stopped status from a source type gate 6 does not allow; a date after today or now |
| Against the input (`check_entries`) | a key the input does not have ("names X, which this input does not have"), in all three files |
| After the build | a key with record ops other than `hold` and `out_of_scope` for which no record was built ("no record was built for X (held: ...)": the review items that name the key say why; release the check or remove the correction); a guard that does not match; an unknown phase; no event that has happened left; a name without its place suffix; a `status_reason` the final status does not have; a record that fails validation |

An entry that only holds or puts out of scope a key the importer already held is fine. Failing
closed means a weekly refresh stops until a reviewer updates an entry (a row that vanished, an
Epoch site renamed upstream, a new importer hold): that is the intent.

## Duplicates: joins, splits and `merged_into`

Two records of one source for one facility are joined *before* the build, by the source's own
element decision (OSM's `join` and `alone`): the importer builds one record. A seed run on an
empty store therefore gives one record with no `merged_into`, which is right, since no id or URL
existed before.

On a populated store, `merge_import` turns a reviewed join into a 07 §6.6 merge. A candidate whose
`Candidate.joined` names the match values a join brought in, and that matches several stored
records each holding only values of the candidate's, updates the record created first (then the
smallest id); every other one is rewritten with `merged_into` the survivor and a `/merged_into`
correction, and counted as `merged` in the receipt. A value held by a merged record and by a
record that is not merged belongs to the latter, so the next run matches the survivor alone. A
record that is reviewed (or already merged) blocks the merge, which stays a `conflict` item, and
so does a candidate without `joined` that matches several records.

A split (`Candidate.split_from`, OSM's `alone`) takes back the merged record that holds the value,
which is unmerged (`merged_into` null, with a `/merged_into` correction); without one, the
candidate matches only records whose values all lie within its own, so it is new while the record
that still lists the value drops it on its own update.

Duplicates across sources (an OSM campus and an Epoch AI site) wait for M4's entity resolution;
no op expresses them.

## The receipt

`correction_metrics` gives the receipt's counts: `corrections_entries`, `corrections_ops`,
`records_corrected`, `corrections_held`, `corrections_out_of_scope` and
`corrections_importer_gap`. `overrides_used` and `overrides_unused` stay; an unused entry now stops
the run, so `overrides_unused` is always 0.

## Guidance

- Fix the importer first (07 §6.7). The sample error rate is measured on the importer's output,
  not on corrected output: an override hides an importer error, so a Fix with `basis:
  importer_gap` is a debt.
- No entry without a verbatim quote. A composed name ("Aligned IAD-01 and IAD-02") needs one page
  that states both names; a place needs a quote that names it.
- Write the corrections after the importer rules they depend on: every importer change can move
  a name, an event or a source id, and the guards then stop the run until the entry is updated.
- One decision per record, and match OSM observations by event and status.
- A held row (AI GridWatch's stage, Epoch twin, id and first-report checks) builds no record:
  corrections on it stop the run unless the entry also releases the check, and a release with
  wrong corrections can publish a row the importer rightly doubted.

## Worked examples

A postcode OSM gives wrong (`config/overrides/osm.json`):

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

The record's postcode becomes 68102; the new source s3 supports `/location/postcode`, OSM's s1 no
longer does, `field_meta["/location/postcode"]` is `human_entered` at 0.99 with 68106 and s1 in
its conflicts, and `corrections[]` gains `{pointer: "/location/postcode", old: "68106", new:
"68102"}`.

A status OSM's tag implies, corrected by a local report: `{"op": "remove_event", "match":
{"event": "other", "status": "operating"}}` and `{"op": "add_event", "event": "approved",
"status": "permitted", "as_of": "2026-08"}` make the Wichita Falls building `permitted`, approved
in 2026-08, with `field_meta["/status"]` citing the report instead of OSM's 0.60 "imported"; the
removed observation stays in `field_meta["/status_history"]` conflicts.

An Epoch AI site with only corrections (`config/overrides/epoch.json`), Meta Aiken: `{"op":
"support", "event": {"event": "first_reported"}}` ties the Aiken Standard to the first report,
and `{"op": "add_alias", "name": "Project Sabal", "kind": "codename"}` adds the codename it
gives, citing the article.

A held AI GridWatch row needs its release and its correction together (`config/overrides/
aigridwatch.json`): `{"release": ["stage"], "reason": ..., "reviewed_at": ..., "corrections":
[...]}`.
