# AI GridWatch fixtures

Two small subsets of the AI GridWatch data center project tracker:

- `projects.json`, used by `tests/sources/test_aigridwatch.py`: one row per stage;
- `seed-2026-10-08.json`, used by `tests/sources/test_aigridwatch_seed.py`: rows of the
  2026-10-08 seed import that the record checks found mapped wrongly.

Attribution: AI GridWatch (<https://aigridwatch.com>), AI GridWatch data center project tracker.
License: Creative Commons Attribution 4.0 International (CC BY 4.0),
<https://creativecommons.org/licenses/by/4.0/>.

## projects.json: what was taken and what was changed

- Source: <https://aigridwatch.com/data/projects.json>, generated 2026-10-07, retrieved 2026-10-07
  (1,300,431 bytes, 284 projects).
- The top-level metadata (`name`, `generated`, `license`, `license_url`, `attribution`,
  `source_page`, `caveat`, `schema`) is copied unchanged. `count` and `verified_count` are set to
  the subset's numbers (12 and 11).
- 12 projects, copied field for field: one for each of the ten stages (two for Approved, one of
  them without coordinates), plus the one row marked `verified: false`.
- Each project's `events` list is cut to at most three entries. The entries kept were chosen so
  that the fixture names no private individuals.

## seed-2026-10-08.json: what was taken and what was changed

- Source: <https://aigridwatch.com/data/projects.json>, generated 2026-10-08, retrieved
  2026-10-08T20:50:12Z by the seed import (1,302,329 bytes, 285 projects, sha256
  `16330d941c519db1db0747666e3ce319344571b2be0079535f0e5284a4ccab47`).
- The top-level metadata is copied unchanged; `count` and `verified_count` are the subset's (22
  and 22).
- 22 projects, field for field except as listed: Dickerson, 900 Conshohocken Road, EdgeCore
  Louisa, King of Prussia, Microsoft Person County, PORTS (Piketon), Microsoft Three Mile Island,
  Midtown St. Louis, Deep Green Lansing, Project Riverjump, Project Taurus, Karis Plum Farms, DC
  Blox Warren Township, Antelope Data Campus, Project Iron Spur, the six PA DEP rows from
  `amazon-web-services-unnamed-data-center-center-township-pa` to
  `zediker-station-data-center-south-strabane-township-pa` (each under another row's id in the
  file), and the real AWS Center Township row (`…-52`).
- Each `events` list keeps only the entries the tests read (by date); rows not listed keep all.
- Personal names are taken out, so the fixture names no individual: the developer's name in 900
  Conshohocken Road's `owner` (emptied) and `note`, and in King of Prussia's `operator` (now "MLP
  Ventures"); officials' names in Project Riverjump's `note` and withdrawal entry ("the mayor"),
  Karis Plum Farms' `note` ("The governor"), PORTS' groundbreaking entry ("SoftBank's chairman")
  and Project Iron Spur's vote entry (the two commissioners who voted in support).
