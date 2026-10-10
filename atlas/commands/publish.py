"""`atlas publish build|verify|upload|put|fixture|takedown` (07 §5.4, §6.8, §11; 10 §11.8).

build writes a release directory (no network except the previous release's manifest), verify
re-checks one, upload sends it to R2 (or --local-target), put uploads one file under the same
rules (the basemap), fixture rebuilds or checks the pinned fixture release 20000101-0000, and
takedown deletes records from rec/ and from every release still stored except the live one.
Exit codes: 0 ok, 1 a check, upload or delete failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

PR_NUMBER_ENV = "ATLAS_PR_NUMBER"
TILES_BASE_ENV = "ATLAS_TILES_BASE"
DEFAULT_TILES = "https://tiles.moonspells.dev"


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _report(problems: list[str], what: str) -> int:
    for problem in problems:
        _err(f"{what}: {problem}")
    _err(f"{what}: {len(problems)} problem(s)")
    return 1


def _pr_number() -> int | None:
    value = os.environ.get(PR_NUMBER_ENV, "").strip()
    return int(value) if value.isdigit() else None


def _git_commit() -> str | None:
    sha = os.environ.get("GITHUB_SHA", "").strip()
    if sha:
        return sha
    from atlas.sources.base import git_commit

    return git_commit()


def _build(args: argparse.Namespace) -> int:
    from atlas.geo.counties import COUNTIES_ZIP, CountyIndex
    from atlas.net import FetchError
    from atlas.publish import BuildOptions, ReleaseError, build_release, release_now, release_time

    now = datetime.now(UTC)
    release: str = args.release or release_now(now)
    try:
        when = release_time(release)
    except ValueError as e:
        _err(f"publish build: {e}")
        return 2
    if args.fixture:
        commit, pr = "fixture", None
    elif args.deterministic:
        commit, pr = None, None
    else:
        commit, pr = _git_commit(), _pr_number()
    opts = BuildOptions(
        records_dir=args.records,
        orgs_path=args.orgs,
        layers_path=args.layers,
        attribution_path=args.attribution,
        changelog_path=args.changelog,
        out_dir=args.out,
        release=release,
        generated_at=when if args.deterministic else now,
        previous=args.previous,
        allow_count_change=args.allow_count_change,
        skip_pmtiles=args.skip_pmtiles,
        fixture=args.fixture,
        tiles_base=args.tiles_base or os.environ.get(TILES_BASE_ENV) or DEFAULT_TILES,
        git_commit=commit,
        pr_number=pr,
        today=now.date(),
    )
    counties = CountyIndex.load(COUNTIES_ZIP)
    try:
        result = build_release(opts, counties=counties)
    except ReleaseError as e:
        return _report(e.problems, "publish build")
    except FetchError as e:
        return _report([str(e)], "publish build")
    finally:
        counties.close()
    counts = result.manifest["counts"]
    print(f"built release {release} in {args.out}: {counts}")
    if args.github_output:
        latest = "false" if args.fixture else "true"
        with Path(args.github_output).open("a", encoding="utf-8", newline="\n") as f:
            f.write(f"release={release}\nlatest={latest}\n")
    return 0


def _verify(args: argparse.Namespace) -> int:
    from atlas.publish import verify_release

    release, problems = verify_release(args.dir, args.release)
    if problems:
        return _report(problems, "publish verify")
    print(f"verified release {release} in {args.dir}")
    return 0


def _upload(args: argparse.Namespace) -> int:
    from atlas.publish import ReleaseError, upload_release
    from atlas.r2 import LocalUploader, R2Error, Uploader, s3_uploader_from_env, storage_errors

    uploader: Uploader | None = None
    try:
        if args.local_target is not None:
            uploader = LocalUploader(args.local_target)
        elif not args.dry_run:
            uploader = s3_uploader_from_env()
        upload_release(
            args.dir,
            args.release,
            uploader,
            latest=not args.no_latest,
            renew=args.renew,
            dry_run=args.dry_run,
        )
    except ReleaseError as e:
        return _report(e.problems, "publish upload")
    except R2Error as e:
        return _report([str(e)], "publish upload")
    except storage_errors() as e:
        return _report([f"R2: {e}"], "publish upload")
    return 0


def _put(args: argparse.Namespace) -> int:
    from atlas.r2 import (
        LocalUploader,
        R2Error,
        Uploader,
        plan_item,
        s3_uploader_from_env,
        storage_errors,
        upload_item,
    )

    path: Path = args.file
    key: str = args.key
    try:
        if not path.is_file():
            raise R2Error(f"{path} is not a file")
        if Path(key).suffix != path.suffix:
            raise R2Error(f"{path.name} and key {key} have different extensions")
        item = plan_item(key, path)
        if args.immutable and not item.immutable:
            raise R2Error(f"{key} is a short-cache key ({item.cache_control}); use --short-cache")
        if args.short_cache and item.immutable:
            raise R2Error(f"{key} is an immutable key; use --immutable")
        if args.dry_run:
            print(
                f"would put {item.key} ({item.content_type}; {item.cache_control}; {item.sha256})"
            )
            return 0
        uploader: Uploader = (
            LocalUploader(args.local_target)
            if args.local_target is not None
            else s3_uploader_from_env()
        )
        sent = upload_item(uploader, item, no_overwrite=args.no_overwrite)
        if not sent:
            print(f"skip {key}: already stored with the same SHA-256")
    except R2Error as e:
        return _report([str(e)], "publish put")
    except storage_errors() as e:
        return _report([f"R2: {e}"], "publish put")
    return 0


def _fixture(args: argparse.Namespace) -> int:
    from atlas.geo.counties import COUNTIES_ZIP, CountyIndex
    from atlas.publish import (
        FIXTURE_OUT,
        REPO_ROOT,
        ReleaseError,
        build_release,
        check_fixture,
        fixture_options,
    )

    today = datetime.now(UTC).date()
    out = REPO_ROOT / FIXTURE_OUT
    counties = CountyIndex.load(COUNTIES_ZIP)
    try:
        if args.check:
            diffs, notes = check_fixture(out, counties=counties, today=today)
            for note in notes:
                print(f"note: {note}")
            if diffs:
                for diff in diffs:
                    _err(f"publish fixture: {diff}")
                _err(f"{FIXTURE_OUT} is out of date; run uv run atlas publish fixture")
                return 1
            print(f"{FIXTURE_OUT} matches its records")
            return 0
        result = build_release(fixture_options(out, today=today), counties=counties)
    except ReleaseError as e:
        return _report(e.problems, "publish fixture")
    finally:
        counties.close()
    print(f"wrote the fixture release {result.release} to {FIXTURE_OUT}")
    return 0


def _takedown(args: argparse.Namespace) -> int:
    from atlas.publish import ReleaseError, plan_takedown, run_takedown
    from atlas.r2 import Bucket, LocalUploader, R2Error, s3_uploader_from_env, storage_errors

    try:
        bucket: Bucket = (
            LocalUploader(args.local_target)
            if args.local_target is not None
            else s3_uploader_from_env()
        )
        plan = plan_takedown(bucket, args.id, keep=args.keep)
        run_takedown(
            bucket,
            plan,
            tiles_base=args.tiles_base or os.environ.get(TILES_BASE_ENV) or DEFAULT_TILES,
            dry_run=args.dry_run,
        )
    except ReleaseError as e:
        return _report(e.problems, "publish takedown")
    except R2Error as e:
        return _report([str(e)], "publish takedown")
    except storage_errors() as e:
        return _report([f"R2: {e}"], "publish takedown")
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from atlas.publish import (
        DEFAULT_ATTRIBUTION,
        DEFAULT_CHANGELOG,
        DEFAULT_LAYERS,
        DEFAULT_OUT,
        DEFAULT_PREVIOUS,
    )

    parser = sub.add_parser("publish", help="build, verify and upload a release (07 §11)")
    actions = parser.add_subparsers(dest="publish_command", metavar="action", required=True)

    build = actions.add_parser("build", help="validate and write a release directory")
    build.add_argument("--records", type=Path, default=Path("data/records"))
    build.add_argument("--orgs", type=Path, default=Path("data/orgs.json"))
    build.add_argument("--layers", type=Path, default=DEFAULT_LAYERS)
    build.add_argument(
        "--attribution", type=Path, default=DEFAULT_ATTRIBUTION, help="copied as ATTRIBUTION.md"
    )
    build.add_argument(
        "--changelog", type=Path, default=DEFAULT_CHANGELOG, help="copied as CHANGELOG.md"
    )
    build.add_argument("--out", type=Path, default=DEFAULT_OUT)
    build.add_argument("--release", default=None, help="YYYYMMDD-HHMM UTC (default: now)")
    build.add_argument(
        "--previous",
        default=DEFAULT_PREVIOUS,
        help="URL of atlas/latest.json, a manifest or release dir, or none (default: %(default)s)",
    )
    build.add_argument(
        "--allow-count-change", action="store_true", help="skip the ±10%% record-count check"
    )
    build.add_argument("--skip-pmtiles", action="store_true", help="do not run tippecanoe")
    build.add_argument(
        "--deterministic",
        action="store_true",
        help="generated_at from the release id; no clock, git or PR number in the output",
    )
    build.add_argument(
        "--fixture", action="store_true", help="mark the release as the fixture (no latest.json)"
    )
    build.add_argument(
        "--tiles-base",
        default=None,
        help=f"tiles origin (default: env {TILES_BASE_ENV}, else https://tiles.moonspells.dev)",
    )
    build.add_argument(
        "--github-output", type=Path, default=None, help="append release= and latest="
    )
    build.set_defaults(handler=_build)

    verify = actions.add_parser("verify", help="re-check a release directory against its manifest")
    verify.add_argument("dir", type=Path)
    verify.add_argument("--release", default=None)
    verify.set_defaults(handler=_verify)

    upload = actions.add_parser("upload", help="upload a release directory to R2")
    upload.add_argument("dir", type=Path)
    upload.add_argument("--release", required=True)
    upload.add_argument("--no-latest", action="store_true", help="do not write atlas/latest.json")
    upload.add_argument(
        "--renew",
        action="store_true",
        help="put again objects stored with the same SHA-256, restarting their 120-day lifecycle "
        "age (the fixture release; docs/r2-setup.md §7)",
    )
    upload.add_argument("--dry-run", action="store_true", help="print the plan; send nothing")
    upload.add_argument(
        "--local-target", type=Path, default=None, help="write to this directory instead of R2"
    )
    upload.set_defaults(handler=_upload)

    put = actions.add_parser("put", help="upload one file under the R2 rules (the basemap)")
    put.add_argument("file", type=Path)
    put.add_argument("--key", required=True)
    mode = put.add_mutually_exclusive_group(required=True)
    mode.add_argument("--immutable", action="store_true")
    mode.add_argument("--short-cache", action="store_true")
    put.add_argument(
        "--no-overwrite", action="store_true", help="fail if the key holds another object"
    )
    put.add_argument("--dry-run", action="store_true")
    put.add_argument(
        "--local-target", type=Path, default=None, help="write to this directory instead of R2"
    )
    put.set_defaults(handler=_put)

    fixture = actions.add_parser("fixture", help="rebuild fixtures/release (20000101-0000)")
    fixture.add_argument(
        "--check", action="store_true", help="rebuild in a temp dir and compare; exit 1 on a diff"
    )
    fixture.set_defaults(handler=_fixture)

    takedown = actions.add_parser(
        "takedown", help="delete records from rec/ and from stored releases (07 §5.4)"
    )
    takedown.add_argument(
        "--id", action="append", required=True, help="record id (gwa-...); repeat for several"
    )
    takedown.add_argument(
        "--keep",
        action="append",
        default=[],
        help="also keep this release (atlas/latest.json's release is always kept)",
    )
    takedown.add_argument("--dry-run", action="store_true", help="list the keys; delete nothing")
    takedown.add_argument(
        "--local-target", type=Path, default=None, help="act on this directory instead of R2"
    )
    takedown.add_argument(
        "--tiles-base", default=None, help=f"for the purge URLs (default: env {TILES_BASE_ENV})"
    )
    takedown.set_defaults(handler=_takedown)
