# Gigawatt Atlas release 20000101-0000

The Gigawatt Atlas is an open dataset of US data center campuses and projects:
operating, under construction and proposed. Map and methodology:
<https://moonspells.dev/atlas>. Pipeline and records:
<https://github.com/moonspells/gigawatt-atlas>.

**This is the fixture release.** Its records are test data for contract tests and
site CI, mostly invented examples. Do not use or cite them as facts about real
facilities.

## Files

| File | What it holds |
|---|---|
| `ATTRIBUTION.md` | every source, its license and citation |
| `CHANGELOG.md` | the data changelog |
| `LICENSE-ODbL-1.0.txt` | the Open Database License 1.0 |
| `README.md` | this file |
| `facilities-map.json` | compact GeoJSON with short keys, used by the /atlas map and table |
| `facilities.csv` | flat table, UTF-8, one row per facility |
| `facilities.geojson` | GeoJSON FeatureCollection, the flat columns as properties |
| `facilities.parquet` | GeoParquet 1.1: one row per facility, nested parts as JSON columns |
| `facilities.pmtiles` | vector tiles (layer `facilities`, zoom 0-12) for third-party maps |
| `feed.json` | changes merged in the last 90 days |
| `manifest.json` | every file with bytes and SHA-256, record counts, git commit, layers |
| `records.jsonl.gz` | every public record in full (merged records too), one JSON per line |
| `schema/facility.v1.json` | JSON Schema (draft 2020-12) of one record |
| `summary.json` | counts and GW by status, group, state, ISO/RTO and evidence level |

Each record also has its own object at `rec/{id}/{hash}.json` next to `v/`; the hash is
the `h` property in `facilities-map.json`. `manifest.json` lists the bytes and SHA-256 of
every other file above.

## License in plain words

The database is published under the Open Database License 1.0 (`LICENSE-ODbL-1.0.txt`,
which is the binding text). You may use, copy and build on it, commercially or not, if you:

1. credit the Gigawatt Atlas and its sources (the line below, and `ATTRIBUTION.md`);
2. share alike: offer any database you derive from it and use or distribute publicly
   under the ODbL too;
3. keep it open: do not add technical restrictions without also offering an
   unrestricted copy.

Maps, charts and screenshots made from the data are Produced Works: made from this
release, they need the attribution only; made from a changed version of the database,
ODbL 4.6 asks you to offer that changed database (or a file of the changes) too.
`facilities.pmtiles` holds the records' values, so it is part of the database.

## Attribution

> Gigawatt Atlas, moonspells.dev/atlas, release 20000101-0000, ODbL 1.0 (https://opendatacommons.org/licenses/odbl/1-0/); contains information from OpenStreetMap contributors (ODbL), PNNL IM3 (ODbL), and data adapted from Epoch AI and AI GridWatch (CC BY 4.0, https://creativecommons.org/licenses/by/4.0/)
