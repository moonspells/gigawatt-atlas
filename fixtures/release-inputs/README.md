# Frozen inputs of the fixture release

`atlas publish fixture` builds `fixtures/release` (release `20000101-0000`) from
`tests/fixtures/records`, `tests/fixtures/orgs.json` and the three files here:

- `layers.json`: the manifest's `layers` map (a copy of `overlays/out/layers.json` on 2026-10-08);
- `ATTRIBUTION.md` and `CHANGELOG.md`: the copies shipped in the release (copies of the repo files
  on 2026-10-08).

They are frozen on purpose. The living repo files change with every data release, new source and
weekly overlays PR, and `atlas publish fixture --check` is part of the required `test` check, so
the fixture must not read them. Change a file here only as a deliberate fixture change, together
with `uv run atlas publish fixture` (docs/publishing.md §6).
