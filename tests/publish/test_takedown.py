"""`atlas publish takedown`: removing a record from rec/ and from stored releases (07 §5.4).

Releases are built from the fixture records and uploaded to a LocalUploader bucket, the same code
path as R2 (S3Uploader's list, get and delete are covered with Stubber in test_r2.py).
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from atlas.cli import main
from atlas.geo.counties import CountyIndex
from atlas.publish import (
    BuildOptions,
    ReleaseError,
    build_release,
    plan_takedown,
    release_time,
    run_takedown,
    upload_release,
)
from atlas.r2 import LocalUploader
from atlas.store import RecordStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TARGET = "gwa-01m4785401mdczvghe70mfz68q"  # the record taken down (no record merges into it)
SHARED = "gwa-01m47854008j5vt37xkj5ag72d"  # another record, in every release
OLD, OLDER, HOTFIX = "20261012-1200", "20261011-1200", "20261013-1200"
UNRELATED = "20261010-1200"  # built before TARGET existed


def quiet(_: str) -> None:
    return None


def build(
    root: Path, release: str, counties: CountyIndex, records: Path = FIXTURES / "records"
) -> Path:
    out = root / release
    opts = BuildOptions(
        records_dir=records,
        orgs_path=FIXTURES / "orgs.json",
        out_dir=out,
        release=release,
        generated_at=release_time(release),
        skip_pmtiles=True,
        today=date(2026, 10, 13),
    )
    build_release(opts, counties=counties, log=quiet)
    return out


def records_without(tmp: Path, rid: str) -> Path:
    target = tmp / f"records-without-{rid}"
    shutil.copytree(FIXTURES / "records", target)
    (target / f"{rid}.json").unlink()
    return target


def records_redacted(tmp: Path, rid: str) -> Path:
    target = tmp / f"records-redacted-{rid}"
    shutil.copytree(FIXTURES / "records", target)
    store = RecordStore(target)
    record = store.load()[rid]
    store.write(record.model_copy(update={"aliases": record.aliases[:1]}))  # one alias removed
    return target


@pytest.fixture(scope="module")
def releases(tmp_path_factory: pytest.TempPathFactory, counties: CountyIndex) -> dict[str, Path]:
    tmp = tmp_path_factory.mktemp("takedown")
    without = records_without(tmp, TARGET)
    return {
        UNRELATED: build(tmp, UNRELATED, counties, without),
        OLDER: build(tmp, OLDER, counties),
        OLD: build(tmp, OLD, counties),
        HOTFIX: build(tmp, HOTFIX, counties, without),
        f"{HOTFIX}-redacted": build(
            tmp / "redacted", HOTFIX, counties, records_redacted(tmp, TARGET)
        ),
    }


def bucket_with(tmp_path: Path, releases: dict[str, Path], *names: str) -> LocalUploader:
    """Upload releases in order; the last one is what atlas/latest.json points to."""
    bucket = LocalUploader(tmp_path / "bucket")
    for name in names:
        release = name.removesuffix("-redacted")
        upload_release(releases[name], release, bucket, latest=True, log=quiet)
    return bucket


def test_removed_record_leaves_every_release_but_the_live_one(
    tmp_path: Path, releases: dict[str, Path]
) -> None:
    bucket = bucket_with(tmp_path, releases, UNRELATED, OLDER, OLD, HOTFIX)
    before = set(bucket.list_keys(""))
    plan = plan_takedown(bucket, [TARGET])
    assert plan.keep == [HOTFIX]
    assert plan.releases == [OLDER, OLD]  # UNRELATED never held the record
    for release in (OLDER, OLD):
        keys = [k for k in plan.delete if k.startswith(f"v/{release}/")]
        assert keys[0] == f"v/{release}/manifest.json"  # no reader meets a manifest without files
        assert set(keys) == {k for k in before if k.startswith(f"v/{release}/")}
    rec = [k for k in plan.delete if k.startswith("rec/")]
    assert rec and all(k.startswith(f"rec/{TARGET}/") for k in rec)
    lines: list[str] = []
    urls = run_takedown(bucket, plan, log=lines.append)
    assert urls == [f"https://tiles.moonspells.dev/{k}" for k in plan.delete]
    after = set(bucket.list_keys(""))
    assert after == before - set(plan.delete)
    assert not [k for k in after if TARGET in k]
    assert "atlas/latest.json" in after
    assert any(k.startswith(f"rec/{SHARED}/") for k in after)  # shared objects stay
    assert any(k.startswith(f"v/{UNRELATED}/") for k in after)
    assert any(line.startswith("next: purge the edge cache") for line in lines)


def test_redacted_record_keeps_only_its_new_rec_object(
    tmp_path: Path, releases: dict[str, Path]
) -> None:
    bucket = bucket_with(tmp_path, releases, OLD, f"{HOTFIX}-redacted")
    rec_before = bucket.list_keys(f"rec/{TARGET}/")
    assert len(rec_before) == 2
    plan = plan_takedown(bucket, [TARGET])
    assert plan.releases == [OLD]
    (old_rec,) = [k for k in plan.delete if k.startswith("rec/")]
    run_takedown(bucket, plan, log=quiet)
    (kept,) = bucket.list_keys(f"rec/{TARGET}/")
    assert kept != old_rec and kept in rec_before
    assert any("still holds a taken-down id" in note for note in plan.notes)


def test_refuses_while_the_live_release_still_serves_the_record(
    tmp_path: Path, releases: dict[str, Path]
) -> None:
    bucket = bucket_with(tmp_path, releases, OLDER, OLD)
    with pytest.raises(ReleaseError, match="the record is unchanged; land the hotfix PR"):
        plan_takedown(bucket, [TARGET])


def test_dry_run_deletes_nothing(tmp_path: Path, releases: dict[str, Path]) -> None:
    bucket = bucket_with(tmp_path, releases, OLD, HOTFIX)
    before = bucket.list_keys("")
    lines: list[str] = []
    plan = plan_takedown(bucket, [TARGET])
    run_takedown(bucket, plan, dry_run=True, log=lines.append)
    assert bucket.list_keys("") == before
    assert sum(line.startswith("would delete v/") for line in lines) == len(
        [k for k in before if k.startswith(f"v/{OLD}/")]
    )


def test_partial_release_without_records_is_deleted(
    tmp_path: Path, releases: dict[str, Path]
) -> None:
    bucket = bucket_with(tmp_path, releases, OLD, HOTFIX)
    # An upload that stopped before records.jsonl.gz and the manifest still left the CSV.
    for key in bucket.list_keys(f"v/{OLD}/"):
        if not key.endswith("facilities.csv"):
            bucket.delete(key)
    plan = plan_takedown(bucket, [TARGET])
    assert plan.releases == [OLD]
    assert f"v/{OLD}/facilities.csv" in plan.delete
    assert any("unreadable or missing" in note for note in plan.notes)


def test_keep_names_the_hotfix_release_without_latest(
    tmp_path: Path, releases: dict[str, Path]
) -> None:
    bucket = LocalUploader(tmp_path / "bucket")
    upload_release(releases[OLD], OLD, bucket, latest=False, log=quiet)
    upload_release(releases[HOTFIX], HOTFIX, bucket, latest=False, log=quiet)
    plan = plan_takedown(bucket, [TARGET], keep=[HOTFIX])
    assert (plan.keep, plan.releases) == ([HOTFIX], [OLD])
    # Keeping a release that still holds the record unchanged would leave it served.
    with pytest.raises(ReleaseError, match="the record is unchanged"):
        plan_takedown(
            bucket_with(tmp_path / "b", releases, OLDER, OLD, HOTFIX), [TARGET], keep=[OLDER]
        )


def test_the_fixture_release_is_never_deleted(tmp_path: Path, releases: dict[str, Path]) -> None:
    bucket = bucket_with(tmp_path, releases, OLD, HOTFIX)
    fixture = Path(__file__).resolve().parents[2] / "fixtures" / "release"
    upload_release(fixture, "20000101-0000", bucket, latest=False, log=quiet)
    plan = plan_takedown(bucket, [TARGET])
    assert plan.keep == ["20000101-0000", HOTFIX]
    assert plan.releases == [OLD]
    assert not [k for k in plan.delete if k.startswith("v/20000101-0000/")]


def test_bad_input(tmp_path: Path, releases: dict[str, Path]) -> None:
    empty = LocalUploader(tmp_path / "empty")
    with pytest.raises(ReleaseError, match=r"atlas/latest\.json not found; pass --keep"):
        plan_takedown(empty, [TARGET])
    with pytest.raises(ReleaseError, match="kept release 20261013-1200 has no"):
        plan_takedown(empty, [TARGET], keep=[HOTFIX])
    with pytest.raises(ReleaseError) as e:
        plan_takedown(empty, ["gwa-x", TARGET], keep=["2026-10-13"])
    assert e.value.problems == [
        "'gwa-x' is not a record id (gwa-...)",
        "--keep '2026-10-13' is not a release id (YYYYMMDD-HHMM)",
    ]
    with pytest.raises(ReleaseError, match="no record id given"):
        plan_takedown(empty, [])


def test_cli_takedown_with_a_local_target(
    tmp_path: Path, releases: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    bucket = bucket_with(tmp_path, releases, OLD, HOTFIX)
    args = ["publish", "takedown", "--id", TARGET, "--local-target", str(tmp_path / "bucket")]
    assert main([*args, "--dry-run"]) == 0
    assert f"would delete v/{OLD}/manifest.json" in capsys.readouterr().out
    assert any(k.startswith(f"v/{OLD}/") for k in bucket.list_keys(""))
    assert main(args) == 0
    out = capsys.readouterr().out
    assert f"deleted v/{OLD}/manifest.json" in out
    assert not [k for k in bucket.list_keys("") if k.startswith(f"v/{OLD}/") or TARGET in k]
