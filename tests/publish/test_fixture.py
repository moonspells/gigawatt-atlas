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
from atlas.jsonio import dumps_pretty
from atlas.publish import FIXTURE_RELEASE, check_fixture, verify_release

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
