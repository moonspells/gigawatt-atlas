# Corrections fixture

`seed-records.json`, used by `tests/sources/test_corrections.py`, holds 19 records the seed
imports built, keyed by `{match key}:{value}` (`osm:way/818167571`,
`epoch_name:Amazon Ridgeland`, `aigridwatch_id:king-of-prussia-mlp-pa`). They are the records of
the owner's corrections that the shared correction mechanism (`atlas/sources/corrections.py`,
`docs/corrections.md`) was designed on, so each test applies a correction to the record the
importer really gives.

## What was taken and what was changed

- 15 records come from an offline seed run with fix round 6's importer rules (OSM and AI
  GridWatch importer version 5, Epoch AI version 5) on an empty store, on the saved seed inputs
  and with the seed's `--now` (2026-10-09): the Overpass response of 2026-10-09T09:51:31Z with
  the PNNL IM3 centroids, Epoch AI's download of 2026-10-09 and AI GridWatch's file generated
  2026-10-08.
- 4 records come from the fifth seed (the importers before fix round 6), because the round's
  importer rules now hold or rename them, and the tests need them as the corrections found them:
  `osm:way/793622740` (then "Aligned IAD-02"), `aigridwatch_id:amazon-bridgefield-ashburn-va`
  (its announcement not yet read as the first report), `aigridwatch_id:microsoft-cheyenne-expansion`
  and `aigridwatch_id:sarasota-county-moratorium-fl` (both now held by the importer).
- Each record is copied whole, field for field, as `atlas` wrote it (its random id included). The
  text was checked: it names organizations only, no private individual.

## Licenses

The records are derived from the sources they cite:

- OpenStreetMap (<https://www.openstreetmap.org/copyright>), Open Database License 1.0 (ODbL), and
  PNNL's IM3 Open Source Data Center Atlas, ODbL 1.0;
- Epoch AI, 'AI data centers' (<https://epoch.ai/data/ai-data-centers>), Creative Commons
  Attribution 4.0 International (CC BY 4.0);
- AI GridWatch (<https://aigridwatch.com>), AI GridWatch data center project tracker, CC BY 4.0.
