# Publishing a release

How the records in `data/records/` become a release on `tiles.moonspells.dev`, and how the
basemap and the fixture release get there. Source of truth: the site plan, chapters 07 (§6.8,
§11) and 10 (§3.6, §11.8). The R2 settings the owner makes once are in
[r2-setup.md](r2-setup.md).

Nothing reaches `v/` except through the `upload` job of `publish.yml`, which runs only after a
merge to `main` (or a dispatch from `main`) and is the only job that sees the R2 token.

## 1. Release layout (contract_version 1)

A release id is the UTC build time, `YYYYMMDD-HHMM`. `atlas publish build` writes a directory
whose paths are the R2 keys of the `atlas-tiles` bucket:

| Key | Content-Type | What it holds |
|---|---|---|
| `v/{release}/manifest.json` | `application/json` | release, generated_at, contract_version 1, schema_version, license, attribution line, `fixture`, `base_url`, `rec_base_url`, git commit and PR number, counts, every other file with bytes, SHA-256 and content type, and the `layers` map copied from `overlays/out/layers.json` |
| `v/{release}/facilities.parquet` | `application/octet-stream` | GeoParquet 1.1 (WKB points, `bbox` covering column, zstd), one row per facility; the flat columns below plus JSON-string columns `aliases`, `parties`, `status_history`, `phases`, `buildings`, `sources`, `incentives` |
| `v/{release}/facilities.geojson` | `application/geo+json` | FeatureCollection, the flat columns as properties |
| `v/{release}/facilities.csv` | `text/csv; charset=utf-8` | the flat columns, header row, LF line ends |
| `v/{release}/facilities.pmtiles` | `application/octet-stream` | tippecanoe 2.79.0, layer `facilities`, zoom 0 to 12, mappable facilities only |
| `v/{release}/facilities-map.json` | `application/json` | compact GeoJSON for the map and table: properties `id n a s g m b p o ot st c e t h x` (07 §11.1), at most 400,000 bytes gzipped |
| `v/{release}/records.jsonl.gz` | `application/gzip` | every public record, merged ones included (the site builds redirects from them), sorted by id; gzip with mtime 0 and no file name. Never `Content-Encoding` |
| `v/{release}/summary.json` | `application/json` | counts and GW by status, group, state, ISO/RTO and evidence level |
| `v/{release}/feed.json` | `application/json` | merged changes of the last 90 days; `items` stays empty until M2 |
| `v/{release}/schema/facility.v1.json` | `application/json` | the JSON Schema |
| `v/{release}/LICENSE-ODbL-1.0.txt` | `text/plain; charset=utf-8` | the license |
| `v/{release}/ATTRIBUTION.md`, `CHANGELOG.md` | `text/markdown; charset=utf-8` | copies of the repo files |
| `v/{release}/README.md` | `text/markdown; charset=utf-8` | generated: release id, file list, the share-alike terms in plain words, the attribution line |
| `rec/{id}/{h}.json` | `application/json` | one public record, compact JSON; `h` is the first 12 hex digits of the SHA-256 of these bytes (the `h` map property). At most 65,536 bytes |
| `atlas/latest.json` | `application/json` | `{"release": ..., "manifest": "https://tiles.moonspells.dev/v/{release}/manifest.json"}`, uploaded last; never for the fixture |

Cache-Control is `public, max-age=31536000, immutable` for `v/`, `rec/`, `basemap/`,
`overlays/` and `archive/`, and `public, max-age=60` for `atlas/latest.json`,
`atlas/pending.json` and `runs/latest.json`. Any other key, and any extension outside `.json`,
`.jsonl.gz`, `.geojson`, `.csv`, `.parquet`, `.pmtiles`, `.md`, `.txt` (and `.wasm` inside a
`vendor/` directory), fails the job. `Content-Encoding` is never set: Cloudflare ignores `Range`
when it has to decompress, and it compresses JSON at the edge by itself.

Flat columns (CSV, GeoJSON properties, Parquet): `id, name, record_type, status, status_group,
evidence_level, operator, owner, developer, tenant, purpose, state, county_fips, county_name,
city, lat, lon, precision, it_mw, facility_mw, utility_request_mw, mw_display, mw_basis,
mw_as_stated, investment_usd, investment_basis, acreage, building_sqft, iso_rto, utility_name,
announced, application_filed, approved, construction_start, expected_in_service,
operating_since, latest_event, latest_event_date, expanding, n_sources, confidence_status,
review_state, updated_at, url`. Several parties of one role are joined with ` | `; dates are the
fuzzy-date values (`2026`, `2026-Q3`, `2026-07`, `2026-07-14`); `confidence_status` is
`field_meta["/status"].confidence` when present.

Public records are the records with `scope: "in_scope"`, with zero-width and bidirectional
control characters removed from every string. Merged records appear only in
`records.jsonl.gz`; out-of-scope records appear nowhere. `analytics/*.parquet` and `mcp/*.json`
are contract-1 files that arrive with Phase 5 (additive).

## 2. Release checks

`atlas publish build` refuses to write a release unless all of these pass:

1. `atlas validate` (07 §3.6) over the records and orgs.
2. The facility count is within ±10% of the previous release's `counts.facilities`, unless
   `--allow-count-change` is given. The previous release comes from
   `https://tiles.moonspells.dev/atlas/latest.json`; a 404 there means this is the first release,
   and any other error fails the build.
3. Every point lies in lat [18, 72] and lon [-180, -64]; DuckDB's `ST_IsValid` passes on every
   geometry in the Parquet file, which has one row per facility.
4. Budgets: `facilities-map.json` at most 400,000 bytes gzipped, every `rec/` object at most
   65,536 bytes, every file under 500,000,000 bytes (the cacheable limit is 512 MB).
5. Every file passes the upload allow-list and has a Cache-Control rule.
6. At least one facility (except for the fixture).

`atlas publish verify DIR` re-checks a built directory: every file is in the manifest with the
right bytes, SHA-256 and content type, the allow-list and key prefixes, the `rec/` names against
their bytes, the map's `h` values against `rec/`, and `atlas/latest.json`.

## 3. Commands

```sh
uv run atlas publish build                       # data/records -> build/release, previous from tiles
uv run atlas publish build --previous none --skip-pmtiles --out build/release   # local, offline
uv run atlas publish verify build/release
uv run atlas publish upload build/release --release 20261020-0613 --dry-run      # the plan only
uv run atlas publish upload build/release --release 20261020-0613 --local-target /tmp/bucket
uv run atlas publish put conus-z10-20261006.pmtiles --key basemap/conus-z10-20261006.pmtiles --immutable --no-overwrite
uv run atlas publish fixture [--check]
```

`build` options: `--records`, `--orgs`, `--layers`, `--out`, `--release` (default: now, UTC),
`--previous URL|PATH|none`, `--allow-count-change`, `--skip-pmtiles`, `--deterministic`
(`generated_at` from the release id; no clock, git commit or PR number), `--fixture`,
`--tiles-base` (or `ATLAS_TILES_BASE`), `--github-output PATH` (appends `release=` and
`latest=`). `GITHUB_SHA` (else `git rev-parse HEAD`) and `ATLAS_PR_NUMBER` go into
`manifest.git`.

`upload` sends, in this order: every `v/{release}/` file except the manifest, the manifest,
the `rec/` objects, then `atlas/latest.json` (skipped with `--no-latest`). An immutable key is
checked with HEAD first: the same `x-amz-meta-sha256` is skipped, a different one fails the job.
A reader that follows `latest.json` therefore never meets a manifest whose files are missing, and
a failed upload leaves `latest.json` on the previous release. Credentials come only from
`CF_ACCOUNT_ID`, `R2_TILES_ACCESS_KEY_ID`, `R2_TILES_SECRET_ACCESS_KEY` and `R2_TILES_BUCKET`
(default `atlas-tiles`); errors name a missing variable and never print a value.

tippecanoe for local builds (CI builds the same commit):

```sh
git init -q /tmp/tippecanoe && cd /tmp/tippecanoe
git fetch -q --depth 1 https://github.com/felt/tippecanoe 68ab8dcc229f95b8b25877697d5e8d66783af503   # tag 2.79.0
git checkout -q FETCH_HEAD && make -j"$(nproc)" tippecanoe     # needs libsqlite3-dev and zlib1g-dev
export PATH="/tmp/tippecanoe:$PATH"
```

## 4. `publish.yml`

Runs on every push to `main` that touches `data/**` or `overlays/out/**`, and on dispatch.

| Job | Environment | Secrets | Steps |
|---|---|---|---|
| `build` | none | none (`GH_TOKEN` is the read-only job token, used to look up the merged PR) | `uv sync --locked`; build tippecanoe 2.79.0 from its pinned commit; `atlas validate`; find the merged PR's number and whether it has the `bulk` label; `atlas publish build` (or `atlas publish fixture --check` and stage `fixtures/release`); upload the directory as the artifact `release` (7 days) |
| `upload` | `production` (deployment branch `main` only) | `R2_TILES_ACCESS_KEY_ID`, `R2_TILES_SECRET_ACCESS_KEY` in the step env only; variable `CF_ACCOUNT_ID` | download the artifact; `atlas publish upload` (verifies again first) |

Dispatch inputs:

- `allow_count_change`: skip the ±10% check for this run.
- `fixture`: upload the committed fixture release instead of building one (section 6).

**A bulk import** (the seed PR, a new source) changes the count by more than 10%. Label the PR
`bulk` before merging it and the push run skips the check. If it was merged without the label,
the `build` job fails with "more than ±10%"; then run the workflow from the Actions tab on `main`
with `allow_count_change` ticked. The release id is the time of that run, so nothing collides
with the failed attempt (which uploaded nothing).

The first release needs the tiles domain (r2-setup.md). Before it exists,
`tiles.moonspells.dev` does not resolve and the build fails at the previous-release check, which
is the intended order: nothing can be uploaded before step 15 either. Once the domain answers,
`atlas/latest.json` returns 404 and the build treats the release as the first one.

The site data PR (07 §6.8 step 4, the `site-pr` job) comes in Phase 2: it will read the
`release` and `latest` outputs of the `build` job.

## 5. `basemap.yml`: the CONUS z10 basemap

Dispatch only, with the Protomaps build id (default `20261006`, the key the site's styles pin:
`basemap/conus-z10-20261006.pmtiles`).

1. `extract` (no secrets, no environment): installs go-pmtiles 1.31.2 after checking the
   tarball's SHA-256, runs `pmtiles extract https://build.protomaps.com/{build}.pmtiles
   conus-z10-{build}.pmtiles --bbox=-125.0,24.4,-66.9,49.4 --maxzoom=10`, `pmtiles verify` and
   `pmtiles show`, fails above 500,000,000 bytes, writes a `.sha256` file and keeps both as the
   artifact `basemap` for 30 days.
2. `upload` (`production`): checks the SHA-256 again, `atlas publish put … --key
   basemap/conus-z10-{build}.pmtiles --immutable --no-overwrite`, then requests the first 16 KiB
   from `tiles.moonspells.dev` and fails unless the answer is `206` without `content-encoding`.

Protomaps seems to keep daily builds for about a week (`builds.json` listed 2026-10-01 to
2026-10-07 on 2026-10-07), so run the workflow as soon as it is on `main`, even before the R2
step: `extract` succeeds and keeps the file, `upload` fails for lack of credentials. After the
owner's R2 step, open that run and choose **Re-run failed jobs** (within 30 days); the upload
uses the kept artifact. If the build is already gone, dispatch with the newest build listed in
`https://build-metadata.protomaps.dev/builds.json` and change the style URL in the site
(`scripts/build-atlas-styles.mjs`) and `overlays/out/layers.json` here to match.

Never hotlink `build.protomaps.com`; the extract is re-hosted. Refresh quarterly under a new key.

## 6. The fixture release `20000101-0000`

Contract tests and site CI read a pinned release until the first real one exists (07 §11.3).
It is built from `tests/fixtures/records` (9 records: 7 facilities, 1 merged, 1 out of scope)
and `tests/fixtures/orgs.json`, and committed under `fixtures/release/v/20000101-0000/` and
`fixtures/release/rec/`. Its manifest has `"fixture": true`, `generated_at`
`2000-01-01T00:00:00Z` and `git.commit` `"fixture"`, and it never has an `atlas/latest.json`.

```sh
uv run atlas publish fixture           # rebuild after a fixture record or a format changes (needs tippecanoe)
uv run atlas publish fixture --check   # CI (tests/publish/test_fixture.py and publish.yml)
```

`atlas publish fixture` is the same as `atlas publish build --records tests/fixtures/records
--orgs tests/fixtures/orgs.json --release 20000101-0000 --fixture --deterministic --previous
none --out fixtures/release`. `--check` rebuilds it in a temp directory and compares: text files
and `records.jsonl.gz` byte for byte, `facilities.parquet` by its rows and `geo` metadata,
`facilities.pmtiles` by tile counts and metadata (only when tippecanoe is on PATH; without it the
committed archive is reused), and `manifest.json` with the bytes and SHA-256 of those two binary
files left out, since a DuckDB or tippecanoe upgrade may re-encode the same content.

To publish it, run `publish.yml` from the Actions tab with `fixture` ticked. The build job checks
the committed fixture and stages it; the upload job sends `v/20000101-0000/**` and `rec/**` with
`--no-latest`. It then answers at `https://tiles.moonspells.dev/v/20000101-0000/manifest.json`.
Uploading it again is a no-op (same SHA-256). A changed fixture under the same id fails the
upload, by design, because immutable keys are never overwritten: to replace it, the owner
deletes `v/20000101-0000/` (and the stale `rec/` objects, if any) in the R2 dashboard, purges
the tiles hostname, and dispatches again. Site CI pins this id, so prefer additive fixture
changes.
