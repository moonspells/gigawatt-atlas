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
