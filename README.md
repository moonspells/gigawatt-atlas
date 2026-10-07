# Gigawatt Atlas

An open dataset and pipeline of US data center campuses and projects: operating, under
construction and proposed. Every displayed value links to a source and, for primary records, a
short verbatim quote. A person reviews every change in a pull request before it is published.
The map lives at [moonspells.dev/atlas](https://moonspells.dev/atlas); this repository holds the
pipeline, the records and the release builder.

**Status: milestone M1 (seed map) in progress.** The schema, validation, status crosswalk and the
importer framework are in place. The OSM and PNNL seed, the Epoch AI and AI GridWatch importers,
geocoding and the publish workflow are landing next; `data/records/` stays empty until the seed
pull request.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) 0.11 and Python 3.13 (uv installs it if needed).

```sh
uv sync --locked                               # exact versions and hashes from uv.lock
uv run python -m atlas.geo.duck --install      # DuckDB spatial extension, once per machine
uv run atlas validate                          # 07 §3.6 over data/records and data/orgs.json
uv run pytest                                  # offline test suite
```

Lint and types: `uv run ruff check .`, `uv run ruff format --check .` and `uv run mypy`.

## Commands

| Command | What it does |
|---|---|
| `atlas validate` | Checks every record in `data/records/` (schema, rollup, dates, sources and support, quotes, geography against the Census county polygons, ranges, personal data, orgs, phases, scope). Exit 1 on any issue. `--records`, `--orgs`, `--today`, `--format json`. |
| `atlas schema export [--check]` | Writes `schema/facility.v1.json` (JSON Schema 2020-12) from `atlas/schema/record.py`; `--check` fails when it is out of date. |
| `atlas import <source>` | Runs a seed importer (`osm`, `epoch`, `aigridwatch` as they land), merges its candidates into `data/records/`, and writes `review/queue/{source}.jsonl` and the receipt `data/imports/{source}.json`. `--input FILE`, `--dry-run`, `--offline`. |
| `atlas publish ...` | Builds, verifies and uploads a release (lands with the publish workflow). |

`python -m atlas` is the same entry point.

## Repository layout

```text
atlas/                 the pipeline (Python package)
  schema/              record and org models, status rollup, claim pointers, JSON Schema export
  sources/             importers; base.py is the framework every importer plugs into
  geo/                 state FIPS codes, DuckDB spatial, the Census county index
  commands/            one module per CLI command
  validate.py          the 07 §3.6 rules
  crosswalk.py         upstream status values to Atlas statuses (docs/status-crosswalk.md)
  net.py safezip.py    guarded HTTP and ZIP handling for untrusted upstream files
data/records/          one canonical JSON file per record ({id}.json)
data/orgs.json         organizations (gwo- ids)
data/imports/          import receipts
review/queue/          review items per source (JSON Lines)
reference/census/      Census boundary and Gazetteer files (see reference/README.md)
schema/                the generated JSON Schema
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

Data errors and takedown requests: open an issue in this repository (the
[moonspells.dev/contact](https://moonspells.dev/contact) form is the private route once it ships).
Security problems: see [SECURITY.md](SECURITY.md); please do not open a public issue.
