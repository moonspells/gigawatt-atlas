# Epoch AI fixture

`data_centers.zip` is a small, re-packed subset of Epoch AI's AI data centers download, used by
`tests/sources/test_epoch.py`.

> Epoch AI, 'AI data centers'. Published online at epoch.ai. Retrieved from
> 'https://epoch.ai/data/ai-data-centers' [online resource].

License: Creative Commons Attribution 4.0 International (CC BY 4.0),
<https://creativecommons.org/licenses/by/4.0/>. Credit: Epoch AI.

## What was taken and what was changed

- Source: <https://epoch.ai/data/data_centers/data_centers.zip>, retrieved 2026-10-07 (114,002
  bytes, ETag `"45b3157f40c5c87c9bdd1ef706415bf2"`, SHA-256
  `199f8b66698dd43e125040db64da5fd44c3b7f23f1613914337a5a6fed09925e`).
- `README.md` is copied unchanged, so the license text stays with the data.
- `data_centers.csv` and `data_center_timelines.csv` keep the original header and only the rows of
  five sites, with every timeline row of each: Colossus 2 (a full street address and a projected
  row), Meta Hyperion (a partial address, "Holly Ridge, LA 71269"), OpenAI Stargate Milam (no
  address), Amazon Madison Mega Site (an address the parser cannot split) and CoreWeave Norway
  (outside the US). Cell values are unchanged; the CSVs were re-written with Python's `csv`
  module (LF line ends).
- The ZIP's other members (chip quantities, chillers, cooling towers, chip types) are left out,
  because the importer does not read them.

## `cases.zip`

The same license and credit apply. `cases.zip` holds the sites behind the timeline, party and
campus rules of `tests/sources/test_epoch_cases.py`, taken from the download of 2026-10-08
(114,113 bytes, ETag `7728e0cff3ce3c556d6c637bff480a5a`, SHA-256
`5421dbb31f07ae0fad48c390d59ef5f62e84a9121ec2dd46ced65e4b5968a25b`), built the same way: the
original `README.md`, and the header and only these sites' rows of the two CSVs, with every
timeline row of each, cell values unchanged.

| Site | Why it is here |
|---|---|
| Coreweave Helios | a status note whose link URL contains "Announces"; a former crypto mining site |
| QTS Richmond 1 | the first row is land "cleared for site expansion" of an older campus |
| Google Storey County | the first row already counts a building "first operational in 2021" |
| Google New Albany | the first row already counts an operational building |
| Google Fort Wayne | a building Epoch leaves out of its count ("not for AI compute") |
| CoreWeave Dalton 1 & 2 | "rebuilding their existing Dalton 1 (and we're assuming 2) datacenter" |
| xAI QTS Atlanta | a bond resolution for equipment in a "multi-tenant building" |
| CoreWeave Lancaster Greenfield site | an announcement, then "Cooling install continues on the roof" |
| Meta Huntsville | a cited "Announcement of expansion completing in 2026" |
| Core42 Lake Mariner, Anthropic Lake Mariner | two sites at 7725 Lake Rd, Barker, NY (one campus) |
| OpenAI Stargate Abilene, Crusoe Abilene Expansion | two sites at 5502 Spinks Rd, Abilene, TX |
