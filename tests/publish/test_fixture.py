"""The pinned fixture release fixtures/release (20000101-0000, 07 §11.3).

Site CI and the consumer contract tests read it until the first real release, so it must always
match what `atlas publish fixture` builds from tests/fixtures/records and tests/fixtures/orgs.json.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from atlas import publish
from atlas.cli import main
from atlas.geo.counties import CountyIndex
from atlas.geo.duck import connect
from atlas.jsonio import dumps_pretty
from atlas.publish import FIXTURE_RELEASE, check_fixture, compare_releases, verify_release

FIXTURE = publish.REPO_ROOT / "fixtures" / "release"
VDIR = FIXTURE / "v" / FIXTURE_RELEASE


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def test_fixture_check_passes(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["publish", "fixture", "--check"]) == 0
    assert "matches its records" in capsys.readouterr().out


def test_committed_fixture_is_a_valid_release() -> None:
    assert verify_release(FIXTURE) == (FIXTURE_RELEASE, [])
    assert sorted(p.name for p in FIXTURE.iterdir()) == ["rec", "v"]  # never atlas/latest.json


def test_fixture_manifest_is_pinned() -> None:
    manifest = read(VDIR / "manifest.json")
    assert manifest["release"] == "20000101-0000"
    assert manifest["fixture"] is True
    assert manifest["generated_at"] == "2000-01-01T00:00:00Z"
    assert manifest["git"] == {"commit": "fixture", "pr_number": None}
    assert manifest["contract_version"] == 1
    assert manifest["base_url"] == "https://tiles.moonspells.dev/v/20000101-0000/"
    assert manifest["counts"]["facilities"] == 7
    assert manifest["counts"]["records_public"] == 8
    assert manifest["counts"]["merged"] == 1
    paths = [f["path"] for f in manifest["files"]]
    assert "facilities.pmtiles" in paths
    assert "atlas" not in {p.split("/")[0] for p in paths}


def test_fixture_map_matches_rec_objects() -> None:
    doc = read(VDIR / "facilities-map.json")
    assert doc["release"] == FIXTURE_RELEASE
    keys = {f"{f['properties']['id']}/{f['properties']['h']}.json" for f in doc["features"]}
    rec = {p.relative_to(FIXTURE / "rec").as_posix() for p in (FIXTURE / "rec").rglob("*.json")}
    assert keys == rec


def test_check_reports_a_stale_fixture(tmp_path: Path, counties: CountyIndex) -> None:
    stale = tmp_path / "release"
    shutil.copytree(FIXTURE, stale)
    summary = stale / "v" / FIXTURE_RELEASE / "summary.json"
    data = read(summary)
    data["facilities"] = 8
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    summary.write_text(text, encoding="utf-8")
    diffs, _ = check_fixture(stale, counties=counties)
    assert any("SHA-256 does not match" in d for d in diffs)
    # Fix the manifest too, so the rebuild comparison is what catches it.
    manifest_path = stale / "v" / FIXTURE_RELEASE / "manifest.json"
    manifest = read(manifest_path)
    for entry in manifest["files"]:
        if entry["path"] == "summary.json":
            entry["bytes"] = len(text.encode("utf-8"))
            entry["sha256"] = publish.sha256_bytes(text.encode("utf-8"))
    manifest_path.write_text(dumps_pretty(manifest), encoding="utf-8")
    diffs, _ = check_fixture(stale, counties=counties)
    assert "v/20000101-0000/summary.json: differs" in diffs
    assert "v/20000101-0000/manifest.json: differs" in diffs


def test_check_reports_a_rec_object_without_a_facility(
    tmp_path: Path, counties: CountyIndex
) -> None:
    stale = tmp_path / "release"
    shutil.copytree(FIXTURE, stale)
    merged = stale / "rec" / "gwa-01m47854070vw56jt2y440t7p8"  # merged records get no rec/ object
    merged.mkdir()
    data = b'{"id":"gwa-01m47854070vw56jt2y440t7p8"}'
    name = f"{publish.sha256_bytes(data)[:12]}.json"
    (merged / name).write_bytes(data)
    diffs, _ = check_fixture(stale, counties=counties)
    assert diffs == [
        f"committed fixture: rec/gwa-01m47854070vw56jt2y440t7p8/{name} is not a facility in "
        "facilities-map.json"
    ]


def test_check_reports_a_missing_file(tmp_path: Path, counties: CountyIndex) -> None:
    stale = tmp_path / "release"
    shutil.copytree(FIXTURE, stale)
    (stale / "v" / FIXTURE_RELEASE / "feed.json").unlink()
    diffs, _ = check_fixture(stale, counties=counties)
    assert (
        "committed fixture: v/20000101-0000/feed.json is listed in the manifest but missing"
        in diffs
    )


def living_repo(tmp_path: Path) -> Path:
    """A stand-in repo root: the real tree, but its own CHANGELOG, ATTRIBUTION and layers.json."""
    root = tmp_path / "repo"
    root.mkdir()
    for entry in publish.REPO_ROOT.iterdir():
        if entry.name not in {".git", ".venv", "CHANGELOG.md", "ATTRIBUTION.md", "overlays"}:
            (root / entry.name).symlink_to(entry)
    changelog = (publish.REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    (root / "CHANGELOG.md").write_text(
        changelog + "\n## 20261020-0613\n\n- Seed import.\n", "utf-8"
    )
    attribution = (publish.REPO_ROOT / "ATTRIBUTION.md").read_text(encoding="utf-8")
    (root / "ATTRIBUTION.md").write_text(attribution + "\n### US Drought Monitor\n", "utf-8")
    layers = read(publish.REPO_ROOT / "overlays" / "out" / "layers.json")
    layers["drought"] = dict(layers["basemap"], label="Drought", asOf="2026-10-06")
    (root / "overlays" / "out").mkdir(parents=True)
    (root / "overlays" / "out" / "layers.json").write_text(dumps_pretty(layers), "utf-8")
    return root


def test_fixture_does_not_read_the_living_repo_docs(
    tmp_path: Path, counties: CountyIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A changelog entry, a new source or the weekly overlays PR must not fail the required
    # `test` check: the fixture reads frozen copies under fixtures/release-inputs.
    monkeypatch.setattr(publish, "REPO_ROOT", living_repo(tmp_path))
    diffs, _ = check_fixture(FIXTURE, counties=counties)
    assert diffs == []
    inputs = publish.REPO_ROOT / "fixtures" / "release-inputs"
    opts = publish.fixture_options(tmp_path / "out")
    assert (opts.layers_path, opts.attribution_path, opts.changelog_path) == (
        inputs / "layers.json",
        inputs / "ATTRIBUTION.md",
        inputs / "CHANGELOG.md",
    )


def test_fixture_ships_its_frozen_inputs() -> None:
    inputs = publish.REPO_ROOT / "fixtures" / "release-inputs"
    for name in ("ATTRIBUTION.md", "CHANGELOG.md"):
        assert (VDIR / name).read_bytes() == (inputs / name).read_bytes()
    assert read(VDIR / "manifest.json")["layers"] == read(inputs / "layers.json")


def write_parquet_copy(source: Path, target: Path, *, where: str = "true", geo: Any = None) -> None:
    """Copy a facilities.parquet, optionally dropping rows or replacing its 'geo' metadata."""
    metadata = geo if geo is not None else publish.parquet_geo_metadata(source)
    text = json.dumps(metadata, separators=(",", ":"), sort_keys=True).replace("'", "''")
    con = connect()
    try:
        con.execute(
            f"COPY (SELECT * FROM read_parquet('{source}') WHERE {where} ORDER BY id) "  # noqa: S608
            f"TO '{target}' (FORMAT parquet, COMPRESSION zstd, GEOPARQUET_VERSION 'NONE', "
            f"KV_METADATA {{geo: '{text}'}})"
        )
    finally:
        con.close()


def test_compare_reports_parquet_rows_and_geo_metadata(tmp_path: Path) -> None:
    committed = VDIR / "facilities.parquet"
    same = tmp_path / "same"
    shutil.copytree(FIXTURE, same)
    write_parquet_copy(committed, same / "v" / FIXTURE_RELEASE / "facilities.parquet")
    assert compare_releases(FIXTURE, same, compare_pmtiles=False) == []

    fewer = tmp_path / "fewer"
    shutil.copytree(FIXTURE, fewer)
    target = fewer / "v" / FIXTURE_RELEASE / "facilities.parquet"
    write_parquet_copy(committed, target, where="id <> 'gwa-01m47854008j5vt37xkj5ag72d'")
    assert compare_releases(FIXTURE, fewer, compare_pmtiles=False) == [
        "v/20000101-0000/facilities.parquet: rows differ"
    ]

    moved = tmp_path / "moved"
    shutil.copytree(FIXTURE, moved)
    geo = publish.parquet_geo_metadata(committed)
    geo["columns"]["geometry"]["bbox"] = [-125.0, 24.0, -66.0, 49.0]
    write_parquet_copy(committed, moved / "v" / FIXTURE_RELEASE / "facilities.parquet", geo=geo)
    assert compare_releases(FIXTURE, moved, compare_pmtiles=False) == [
        "v/20000101-0000/facilities.parquet: geo metadata differs"
    ]


def test_compare_reports_pmtiles_only_when_asked(tmp_path: Path) -> None:
    other = tmp_path / "other"
    shutil.copytree(FIXTURE, other)
    pmtiles = other / "v" / FIXTURE_RELEASE / "facilities.pmtiles"
    data = bytearray(pmtiles.read_bytes())
    data[102 - 1] = 11  # max_zoom 12 -> 11 in the header
    pmtiles.write_bytes(bytes(data))
    assert compare_releases(FIXTURE, other, compare_pmtiles=False) == []
    assert compare_releases(FIXTURE, other, compare_pmtiles=True) == [
        "v/20000101-0000/facilities.pmtiles: tile counts or metadata differ"
    ]
