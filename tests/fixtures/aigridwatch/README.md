# AI GridWatch fixture

`projects.json` is a small subset of the AI GridWatch data center project tracker, used by
`tests/sources/test_aigridwatch.py`.

Attribution: AI GridWatch (<https://aigridwatch.com>), AI GridWatch data center project tracker.
License: Creative Commons Attribution 4.0 International (CC BY 4.0),
<https://creativecommons.org/licenses/by/4.0/>.

## What was taken and what was changed

- Source: <https://aigridwatch.com/data/projects.json>, generated 2026-10-07, retrieved 2026-10-07
  (1,300,431 bytes, 284 projects).
- The top-level metadata (`name`, `generated`, `license`, `license_url`, `attribution`,
  `source_page`, `caveat`, `schema`) is copied unchanged. `count` and `verified_count` are set to
  the subset's numbers (12 and 11).
- 12 projects, copied field for field: one for each of the ten stages (two for Approved, one of
  them without coordinates), plus the one row marked `verified: false`.
- Each project's `events` list is cut to at most three entries. The entries kept were chosen so
  that the fixture names no private individuals.
