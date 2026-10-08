"""atlas.publish.verify_release on tampered releases: one test per check (07 §6.8 step 3).

verify runs again in the upload job before anything is sent, so each check is a last safety net;
every one is fed a bad input here.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from atlas import publish
from atlas.geo.counties import CountyIndex
from atlas.jsonio import dumps_pretty
from atlas.publish import FIXTURE_RELEASE, BuildOptions, build_release, verify_release

FIXTURE = publish.REPO_ROOT / "fixtures" / "release"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RELEASE = "20261012-1200"
ASHBURN = "gwa-01m47854008j5vt37xkj5ag72d"


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, doc: Any) -> None:
    path.write_text(dumps_pretty(doc), encoding="utf-8")


@pytest.fixture(scope="module")
def release_build(tmp_path_factory: pytest.TempPathFactory, counties: CountyIndex) -> Path:
    """A real (non-fixture) release with atlas/latest.json."""
    out = tmp_path_factory.mktemp("verify") / "release"
    opts = BuildOptions(
        records_dir=FIXTURES / "records",
        orgs_path=FIXTURES / "orgs.json",
        out_dir=out,
        release=RELEASE,
        generated_at=datetime(2026, 10, 12, 12, 0, tzinfo=UTC),
        skip_pmtiles=True,
        today=datetime(2026, 10, 12, tzinfo=UTC).date(),
    )
    build_release(opts, counties=counties, log=lambda _: None)
    return out


@pytest.fixture
def fixture_copy(tmp_path: Path) -> Path:
    target = tmp_path / "fixture"
    shutil.copytree(FIXTURE, target)
    return target


@pytest.fixture
def release_copy(tmp_path: Path, release_build: Path) -> Path:
    target = tmp_path / "release"
    shutil.copytree(release_build, target)
    return target


def problems(root: Path, release: str | None = None) -> list[str]:
    return verify_release(root, release)[1]


def test_untampered_copies_verify(fixture_copy: Path, release_copy: Path) -> None:
    assert problems(fixture_copy) == []
    assert problems(release_copy) == []


def rec_path(root: Path, rid: str = ASHBURN) -> Path:
    (path,) = (root / "rec" / rid).glob("*.json")
    return path


def test_rec_name_must_be_the_hash_of_its_bytes(fixture_copy: Path) -> None:
    path = rec_path(fixture_copy)
    doc = json.loads(path.read_bytes())
    doc["canonical_name"] = "Tampered"
    path.write_bytes(json.dumps(doc, separators=(",", ":")).encode("utf-8"))
    key = path.relative_to(fixture_copy).as_posix()
    assert problems(fixture_copy) == [f"{key}: name is not the SHA-256 prefix of its bytes"]


def test_rec_record_id_must_match_the_key(fixture_copy: Path) -> None:
    path = rec_path(fixture_copy)
    other = "gwa-01m4785401mdczvghe70mfz68q"
    data = path.read_bytes().replace(ASHBURN.encode(), other.encode())
    path.unlink()
    renamed = path.with_name(f"{publish.sha256_bytes(data)[:12]}.json")
    renamed.write_bytes(data)
    key = renamed.relative_to(fixture_copy).as_posix()
    found = problems(fixture_copy)
    assert f"{key}: record id does not match the key" in found


def test_rec_key_shape_and_json(fixture_copy: Path) -> None:
    bad = fixture_copy / "rec" / ASHBURN / "notahash.json"
    bad.write_text("{}", encoding="utf-8")
    not_json = b"not json"
    name = f"{publish.sha256_bytes(not_json)[:12]}.json"
    (fixture_copy / "rec" / ASHBURN / name).write_bytes(not_json)
    found = problems(fixture_copy)
    assert f"rec/{ASHBURN}/notahash.json is not rec/{{id}}/{{sha256[:12]}}.json" in found
    assert f"rec/{ASHBURN}/{name}: not JSON" in found


def test_map_entry_without_a_rec_object(fixture_copy: Path) -> None:
    path = rec_path(fixture_copy)
    key = path.relative_to(fixture_copy).as_posix()
    path.unlink()
    assert problems(fixture_copy) == [f"facilities-map.json: {key} is missing"]


def test_unexpected_entries_at_the_root(fixture_copy: Path) -> None:
    (fixture_copy / "extra.json").write_text("{}", encoding="utf-8")
    assert f"unexpected extra.json in {fixture_copy}" in problems(fixture_copy)


def edit_manifest(root: Path, release: str, change: Callable[[dict[str, Any]], None]) -> None:
    path = root / "v" / release / "manifest.json"
    doc = read(path)
    change(doc)
    write(path, doc)


def test_manifest_contract_version(fixture_copy: Path) -> None:
    edit_manifest(fixture_copy, FIXTURE_RELEASE, lambda m: m.update(contract_version=2))
    assert problems(fixture_copy) == ["manifest contract_version is not 1"]


def test_manifest_release_id(fixture_copy: Path) -> None:
    edit_manifest(fixture_copy, FIXTURE_RELEASE, lambda m: m.update(release="20000101-0001"))
    assert problems(fixture_copy) == ["manifest release '20000101-0001' is not 20000101-0000"]


def test_manifest_content_type(fixture_copy: Path) -> None:
    def change(m: dict[str, Any]) -> None:
        entry = next(f for f in m["files"] if f["path"] == "facilities.parquet")
        entry["content_type"] = "application/octet-stream"

    edit_manifest(fixture_copy, FIXTURE_RELEASE, change)
    assert problems(fixture_copy) == [
        "v/20000101-0000/facilities.parquet: content_type is not application/vnd.apache.parquet"
    ]


def test_manifest_bytes_and_order(fixture_copy: Path) -> None:
    def change(m: dict[str, Any]) -> None:
        m["files"].reverse()
        m["files"][0]["bytes"] += 1

    edit_manifest(fixture_copy, FIXTURE_RELEASE, change)
    found = problems(fixture_copy)
    assert "manifest files are not sorted by path" in found
    assert any("bytes, manifest says" in p for p in found)


def test_file_size_cap(fixture_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publish, "FILE_MAX_BYTES", 40_000)
    assert problems(fixture_copy) == [
        "v/20000101-0000/facilities.pmtiles: 51569 bytes > 40000",
    ]


def test_fixture_must_not_have_latest(fixture_copy: Path) -> None:
    (fixture_copy / "atlas").mkdir()
    write(fixture_copy / "atlas" / "latest.json", {"release": FIXTURE_RELEASE, "manifest": "x"})
    found = problems(fixture_copy)
    assert "atlas/latest.json must not exist for the fixture release" in found


def test_latest_must_point_to_this_release(release_copy: Path) -> None:
    pointer = release_copy / "atlas" / "latest.json"
    doc = read(pointer)
    write(pointer, dict(doc, release="20261011-0600"))
    assert problems(release_copy) == [
        "atlas/latest.json points to '20261011-0600', not 20261012-1200"
    ]
    write(pointer, dict(doc, manifest="https://tiles.moonspells.dev/v/20261011-0600/manifest.json"))
    assert problems(release_copy) == [
        "atlas/latest.json: manifest URL does not end in /v/20261012-1200/manifest.json"
    ]
    pointer.write_text("not json", encoding="utf-8")
    assert problems(release_copy) == ["atlas/latest.json: not JSON"]


def test_only_latest_under_atlas(release_copy: Path) -> None:
    (release_copy / "atlas" / "pending.json").write_text("{}", encoding="utf-8")
    assert problems(release_copy) == [
        "atlas/pending.json: a release build writes only atlas/latest.json under atlas/"
    ]


def test_one_release_per_directory(release_copy: Path) -> None:
    shutil.copytree(release_copy / "v" / RELEASE, release_copy / "v" / "20261012-1300")
    assert verify_release(release_copy) == (
        None,
        [f"{release_copy}/v holds 2 releases; pass --release"],
    )
    found = problems(release_copy, RELEASE)
    assert f"{release_copy}/v must hold only {RELEASE}, found {RELEASE}, 20261012-1300" in found
