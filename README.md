# Gigawatt Atlas

An open dataset and pipeline of US data center campuses and projects: operating, under
construction and proposed. Every displayed value links to a source and, for primary records, a
short verbatim quote. A person reviews every change in a pull request before it is published.
The map lives at [moonspells.dev/atlas](https://moonspells.dev/atlas); this repository holds the
pipeline, the records and the release builder.

**Status: milestone M1 (seed map) in progress.** The schema, validation, status crosswalk, the
importers (OpenStreetMap with the PNNL cross-check, Epoch AI, AI GridWatch), geocoding, the release
builder and the publish workflow are in place. `data/records/` stays empty until the seed pull
request; until then publish.yml has nothing to publish, and the committed fixture release
(`fixtures/release`, test data) stands in for one.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) 0.11.32 or newer (CI pins 0.11.32) and Python 3.13 (uv
installs it if needed). `tippecanoe` 2.79.0 on `PATH` is needed only to build `facilities.pmtiles`
(docs/publishing.md); without it those tests are skipped.

```sh
uv sync --locked                               # exact versions and hashes from uv.lock
uv run python -m atlas.geo.duck --install      # DuckDB spatial extension, once per machine
# `atlas import` downloads two Census files on first use (reference/README.md)
uv run atlas validate                          # 07 §3.6 over data/records and data/orgs.json
uv run pytest                                  # offline test suite
```

Lint and types: `uv run ruff check .`, `uv run ruff format --check .` and `uv run mypy`.

## Commands

| Command | What it does |
|---|---|
| `atlas validate` | Checks every record in `data/records/` (schema, rollup, dates, sources and support, quotes, geography against the Census county polygons, ranges, personal data, orgs, phases, scope). Exit 1 on any issue. `--records`, `--orgs`, `--today`, `--format json`. |
| `atlas schema export [--check]` | Writes `schema/facility.v1.json` (JSON Schema 2020-12) from `atlas/schema/record.py`; `--check` fails when it is out of date. |
| `atlas import <source>` | Runs a seed importer (`osm`, `epoch`, `aigridwatch`; run `epoch` before `aigridwatch`, which refuses a store without Epoch records unless given `--without-epoch`), merges its candidates into `data/records/`, and writes `review/queue/{source}.jsonl` and the receipt `data/imports/{source}.json`. `--input FILE`, `--dry-run`, `--offline`, `--places PATH`, plus per-source options (`atlas import osm --help`). The Census place polygons and county subdivisions are downloaded into `--cache-dir` on first use and checked against their pinned SHA-256 ([reference/README.md](reference/README.md#downloaded-on-first-use)). |
| `atlas publish build\|verify\|upload\|put\|fixture\|takedown` | Builds a release directory (`build`), re-checks one against its manifest (`verify`), uploads it to R2 (`upload`, or one file with `put`), rebuilds or checks the committed fixture release (`fixture [--check]`), and removes taken-down records from R2 (`takedown`). See [docs/publishing.md](docs/publishing.md). |

`python -m atlas` is the same entry point.

## Repository layout

```text
atlas/                 the pipeline (Python package)
  schema/              record and org models, status rollup, claim pointers, JSON Schema export
  sources/             importers (osm, epoch, aigridwatch) and the PNNL join; base.py is the
                       framework every importer plugs into
  geo/                 state FIPS codes, DuckDB spatial, the Census county index
  commands/            one module per CLI command
  validate.py          the 07 §3.6 rules
  crosswalk.py         upstream status values to Atlas statuses (docs/status-crosswalk.md)
  dissolve.py          OSM objects into campuses
  geocode.py           Census Geocoder, Gazetteer and county fallbacks
  publish.py r2.py     the release builder and the R2 uploader
  net.py safezip.py    guarded HTTP and ZIP handling for untrusted upstream files
config/overrides/      cited location overrides (epoch.json) and reviewer releases of held AI
                       GridWatch rows (aigridwatch.json)
data/records/          one canonical JSON file per record ({id}.json)
data/orgs.json         organizations (gwo- ids)
data/imports/          import receipts
review/queue/          review items per source (JSON Lines)
overlays/out/          overlay layer index (layers.json) for the release manifest
fixtures/release/      the committed fixture release 20000101-0000 (test data, frozen inputs
                       in fixtures/release-inputs/)
reference/census/      Census boundary and Gazetteer files (see reference/README.md; the place
                       polygons and county subdivisions are downloaded on first use instead)
schema/                the generated JSON Schema
r2/                    the R2 CORS rules (docs/r2-setup.md)
docs/                  sources, crosswalk, publishing and R2 runbooks
tests/                 pytest suite and fixtures
```

## Documentation

- [docs/status-crosswalk.md](docs/status-crosswalk.md): how upstream statuses map to the eight Atlas statuses
- [docs/sources/osm-pnnl.md](docs/sources/osm-pnnl.md): the OpenStreetMap and PNNL seed
- [docs/sources/epoch-aigridwatch.md](docs/sources/epoch-aigridwatch.md): the Epoch AI and AI GridWatch importers
- [docs/publishing.md](docs/publishing.md): release layout and the publish workflow
- [docs/r2-setup.md](docs/r2-setup.md): the R2 buckets and the tiles domain

## Licenses

- Code: [MIT](LICENSE).
- The database (`data/` and every published release): [Open Database License 1.0](LICENSE-ODbL-1.0.txt).
  [DATA-LICENSE.md](DATA-LICENSE.md) says what that means in plain words.
- Sources and how to credit them: [ATTRIBUTION.md](ATTRIBUTION.md).

## Corrections and security

- A wrong value: open an issue in this repository, with no personal data in it.
- A takedown (personal data, legal notices): never a public issue. Email **hello@moonspells.dev**
  with "Atlas takedown" in the subject; [DATA-LICENSE.md](DATA-LICENSE.md#corrections-and-takedowns)
  has the details. A private moonspells.dev/contact form takes over once it ships.
- Security problems: see [SECURITY.md](SECURITY.md); please do not open a public issue.
