from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SITE_ZIZMOR = Path("/home/user/blogWebsite/.github/zizmor-requirements.txt")
ODBL_SHA256 = "607680718977f6f6c9607972afd98f208573f19251315ed1362a8589b51beaf5"
COUNTY_SHA256 = "faec522080681e79be5be435c981009a77891206ff8a7f1d142f3bf5da9ebd74"
GITHUB_ACTIONS_APP_ID = 15368


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ci_job_names(repo_root: Path) -> list[str]:
    text = (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    jobs = text[text.index("\njobs:\n") :]
    return re.findall(r"^    name: (\S+)\s*$", jobs, re.M)


def test_ruleset_checks_equal_ci_job_names(repo_root: Path) -> None:
    ruleset = json.loads(
        (repo_root / ".github" / "rulesets" / "main.json").read_text(encoding="utf-8")
    )
    (rule,) = [r for r in ruleset["rules"] if r["type"] == "required_status_checks"]
    checks = rule["parameters"]["required_status_checks"]
    assert sorted(c["context"] for c in checks) == sorted(ci_job_names(repo_root))
    assert sorted(ci_job_names(repo_root)) == ["lint", "test", "validate", "zizmor"]
    assert {c["integration_id"] for c in checks} == {GITHUB_ACTIONS_APP_ID}
    types = [r["type"] for r in ruleset["rules"]]
    assert "code_scanning" in types and "pull_request" in types
    assert ruleset["bypass_actors"] == []


def test_odbl_text_is_the_official_file(repo_root: Path) -> None:
    path = repo_root / "LICENSE-ODbL-1.0.txt"
    assert path.stat().st_size == 25_278
    assert sha256(path) == ODBL_SHA256


def test_county_file_is_the_census_release(repo_root: Path) -> None:
    path = repo_root / "reference" / "census" / "cb_2025_us_county_5m.zip"
    assert path.stat().st_size == 2_983_552
    assert sha256(path) == COUNTY_SHA256
    assert COUNTY_SHA256 in (repo_root / "reference" / "README.md").read_text(encoding="utf-8")


def hashes(text: str) -> tuple[list[str], list[str]]:
    pins = re.findall(r"^(\S+==\S+)", text, re.M)
    return pins, sorted(re.findall(r"--hash=sha256:([0-9a-f]{64})", text))


def test_zizmor_requirements_are_hash_pinned(repo_root: Path) -> None:
    pins, digests = hashes(
        (repo_root / ".github" / "zizmor-requirements.txt").read_text(encoding="utf-8")
    )
    assert pins == ["zizmor==1.30.1"]
    assert len(digests) == 11


@pytest.mark.skipif(not SITE_ZIZMOR.exists(), reason="the site repo is not checked out here")
def test_zizmor_requirements_match_the_site(repo_root: Path) -> None:
    ours = (repo_root / ".github" / "zizmor-requirements.txt").read_text(encoding="utf-8")
    assert hashes(ours) == hashes(SITE_ZIZMOR.read_text(encoding="utf-8"))


def test_licenses_and_owner_files(repo_root: Path) -> None:
    mit = (repo_root / "LICENSE").read_text(encoding="utf-8")
    assert mit.startswith("MIT License\n\nCopyright (c) 2026 moonspells\n")
    codeowners = (repo_root / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    assert re.search(r"^\* @moonman36$", codeowners, re.M)
    assert re.search(r"^/data/ @moonman36$", codeowners, re.M)
    security = (repo_root / "SECURITY.md").read_text(encoding="utf-8")
    assert "security@moonspells.dev" in security
    assert "https://github.com/moonspells/gigawatt-atlas/security/advisories/new" in security
    for name in ("DATA-LICENSE.md", "ATTRIBUTION.md", "CHANGELOG.md", "README.md"):
        assert (repo_root / name).stat().st_size > 0
    assert (
        (repo_root / "CHANGELOG.md")
        .read_text(encoding="utf-8")
        .startswith("# Gigawatt Atlas data changelog\n")
    )


def test_layout(repo_root: Path) -> None:
    for keep in ("data/records/.gitkeep", "data/imports/.gitkeep", "review/queue/.gitkeep"):
        assert (repo_root / keep).exists()
    assert (repo_root / ".python-version").read_text(encoding="utf-8") == "3.13\n"
    conftests = [p for d in ("tests", "atlas") for p in (repo_root / d).rglob("conftest.py")]
    assert conftests == [repo_root / "tests" / "conftest.py"]
    assert not (repo_root / "conftest.py").exists()


# ----------------------------------------------------------------- the review-round fixes (RD1-RD9)

CC_BY = "https://creativecommons.org/licenses/by/4.0/"
ODBL = "https://opendatacommons.org/licenses/odbl/1-0/"
LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
SETUP_UV_RE = re.compile(r"setup-uv@[0-9a-f]{40} # v[\d.]+\n\s+with: \{ version: '([\d.]+)'")
WORKFLOWS = ("ci.yml", "publish.yml", "basemap.yml")


def section(text: str, heading: str) -> str:
    """The body of a Markdown section, up to the next heading of the same or a higher level."""
    level = len(heading.split(" ", 1)[0])
    start = text.index(f"\n{heading}\n") + len(heading) + 2
    stop = re.compile(rf"^#{{1,{level}}} ", re.M).search(text, start)
    return text[start : stop.start() if stop else len(text)]


def test_uv_version_is_a_floor_not_a_cap(repo_root: Path) -> None:
    # Dependabot's uv updater runs a newer uv than CI (0.12.x in October 2026). A cap in
    # required-version made every Dependabot uv run fail with "Required uv version ... does not
    # match the running version", so version and security updates stopped silently (RD1).
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    required = pyproject["tool"]["uv"]["required-version"]
    assert re.fullmatch(r">=\d+\.\d+\.\d+", required), required
    floor = required.removeprefix(">=")
    (backend,) = pyproject["build-system"]["requires"]
    cap = re.fullmatch(r"uv_build>=([\d.]+),<(\d+)\.(\d+)", backend)
    assert cap is not None, backend
    assert cap[1] == floor
    assert (int(cap[2]), int(cap[3])) >= (0, 13), "uv_build must allow the uv Dependabot runs"
    pinned: set[str] = set()
    for name in WORKFLOWS:
        text = (repo_root / ".github" / "workflows" / name).read_text(encoding="utf-8")
        pinned.update(SETUP_UV_RE.findall(text))
    assert pinned == {floor}, pinned  # one uv in every workflow, bumped together with the floor
    assert f"{floor} or newer" in (repo_root / "README.md").read_text(encoding="utf-8")


def test_attribution_links_resolve_inside_a_release(repo_root: Path) -> None:
    # Every release ships ATTRIBUTION.md but not DATA-LICENSE.md or reference/README.md, so a
    # relative link must name a file the release has (attribution-broken-links).
    from atlas.publish import FILE_DESCRIPTIONS

    links = LINK_RE.findall((repo_root / "ATTRIBUTION.md").read_text(encoding="utf-8"))
    assert links
    for target in links:
        if not target.startswith(("https://", "#")):
            assert target in FILE_DESCRIPTIONS, f"{target}: not in a release"


def test_attribution_meets_cc_by_and_odbl(repo_root: Path) -> None:
    # CC BY 4.0 §3(a)(1): the license URI and an indication that the material was modified
    # (RD4, B9). The ODbL sources carry the ODbL URI; the copy-ready line carries both; the
    # PNNL entry carries the web-map repository's notice and the file's checksum (RD8).
    text = (repo_root / "ATTRIBUTION.md").read_text(encoding="utf-8")
    for source in ("### Epoch AI", "### AI GridWatch"):
        body = section(text, source)
        assert CC_BY in body, source
        assert "Changes made" in body, source
    pnnl = section(text, "### PNNL IM3 Open Source Data Center Atlas")
    assert ODBL in pnnl
    assert "BSD 2-Clause" in pnnl and "Battelle Memorial Institute" in pnnl
    assert "2e7bd7e650fe86fe0d156b4e483ebd331cfa98a0468ce33b932f8c1b6c3245df" in pnnl
    head = text.split("\n## Datasets\n", 1)[0]
    copy_line = " ".join(ln.removeprefix("> ") for ln in head.splitlines() if ln.startswith("> "))
    assert ODBL in copy_line and CC_BY in copy_line and "adapted" in copy_line
    corner = section(text, "## Basemap (a Produced Work)")
    assert "Epoch AI and" in corner and "(CC BY 4.0)" in corner


def test_attribution_names_every_osm_tag_the_importer_queries(repo_root: Path) -> None:
    from atlas.sources.osm import OVERPASS_QUERY

    used = section((repo_root / "ATTRIBUTION.md").read_text(encoding="utf-8"), "### OpenStreetMap")
    keys = re.findall(r'nwr\["([^"]+)"="data_center"\]', OVERPASS_QUERY)
    assert "construction" in keys
    for key in keys:
        assert f"`{key}`" in used, key
    assert "out tags bb" in OVERPASS_QUERY and "footprint" not in used


def test_takedowns_never_go_to_public_issues(repo_root: Path) -> None:
    # Personal-data and legal takedowns go to a private address, and the contact form that does
    # not exist yet is not linked as if it did (RD5).
    for name in ("DATA-LICENSE.md", "README.md", "SECURITY.md"):
        text = (repo_root / name).read_text(encoding="utf-8")
        assert "hello@moonspells.dev" in text, name
        assert "](https://moonspells.dev/contact" not in text, name
        # One paragraph, list item or heading at a time.
        blocks = [re.sub(r"\s+", " ", b) for b in re.split(r"\n\s*\n|\n(?=- |#)", text)]
        assert not [b for b in blocks if re.search(r"takedown.*open an issue", b, re.I)], name
        assert [b for b in blocks if re.search(r"takedown.*never (in )?a public issue", b, re.I)]


def test_produced_works_wording_keeps_odbl_share_alike(repo_root: Path) -> None:
    # ODbL 4.6: a Produced Work made from a changed database obliges you to offer that database,
    # and the PMTiles download is part of the database, not a Produced Work (RD9).
    text = re.sub(r"\s+", " ", (repo_root / "DATA-LICENSE.md").read_text(encoding="utf-8"))
    assert "share-alike rule does not apply" not in text
    assert "section 4.6" in text
    assert "`facilities.pmtiles` download is not" in text
    assert "tiles.moonspells.dev/rec/" in text


def test_readme_lists_every_command(repo_root: Path) -> None:
    import argparse

    from atlas.cli import build_parser

    def choices(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
        subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        return dict(subs[0].choices) if subs else {}

    table = section((repo_root / "README.md").read_text(encoding="utf-8"), "## Commands")
    for name, parser in choices(build_parser()).items():
        assert f"`atlas {name}" in table, name
        if name in ("import", "publish"):
            row = next(line for line in table.splitlines() if f"`atlas {name}" in line)
            for action in choices(parser):
                assert re.search(rf"(?<![\w-]){action}(?![\w-])", row), f"atlas {name} {action}"


def test_zizmor_requirements_name_the_site_repo_for_bumps(repo_root: Path) -> None:
    text = (repo_root / ".github" / "zizmor-requirements.txt").read_text(encoding="utf-8")
    assert "moonspells/moonspells.dev" in text  # docs/ops/dependencies.md is a site-repo file


# ------------------------------------------------------------------- ci.yml (RD3, pmtiles-in-ci)


def ci_job(repo_root: Path, name: str) -> str:
    """The text of one job of ci.yml, from its id line to the next job."""
    text = (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    body = text[text.index("\njobs:\n") :]
    parts = re.split(r"^  ([a-z][a-z0-9_-]*):\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2], strict=True))[name]


def release_age_script(repo_root: Path) -> str:
    """The Python the ci lint job runs on uv.lock (10 §11.8), dedented."""
    job = ci_job(repo_root, "lint")
    start = job.index("<<'PY'\n") + len("<<'PY'\n")
    end = job.index("\n          PY\n", start)
    return "\n".join(line.removeprefix("          ") for line in job[start:end].splitlines())


def run_release_age(repo_root: Path, lock: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    (cwd / "uv.lock").write_text(lock, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-I", "-"],
        input=release_age_script(repo_root),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def fresh_upload(lock: str, package: str) -> str:
    """lock with every upload-time of package set to one minute ago."""
    now = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    head, sep, rest = lock.partition(f'[[package]]\nname = "{package}"\n')
    assert sep, package
    block, nxt, tail = rest.partition("\n[[package]]\n")
    block = re.sub(r'upload-time = "[^"]+"', f'upload-time = "{now}"', block)
    return head + sep + block + nxt + tail


def test_release_age_check_passes_the_committed_lock(repo_root: Path, tmp_path: Path) -> None:
    lock = (repo_root / "uv.lock").read_text(encoding="utf-8")
    result = run_release_age(repo_root, lock, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("0 problem(s)")


def test_release_age_check_fails_a_file_uploaded_today(repo_root: Path, tmp_path: Path) -> None:
    # uv sync --locked and uv lock --check both accept this lock; only this step rejects it (RD3).
    lock = fresh_upload((repo_root / "uv.lock").read_text(encoding="utf-8"), "types-jsonschema")
    result = run_release_age(repo_root, lock, tmp_path)
    assert result.returncode == 1
    assert "types-jsonschema" in result.stdout
    assert "boto3" not in result.stdout


def test_release_age_check_honors_a_reviewed_exception(repo_root: Path, tmp_path: Path) -> None:
    # exclude-newer-package as uv 0.11.32 records it: a timestamp, or an inline table with a
    # span for a relative value (checked 2026-10-08).
    lock = fresh_upload((repo_root / "uv.lock").read_text(encoding="utf-8"), "boto3")
    later = (datetime.now(UTC) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lock = lock.replace(
        'exclude-newer-span = "PT24H"\n',
        f'exclude-newer-span = "PT24H"\n\n[options.exclude-newer-package]\nboto3 = "{later}"\n'
        'pydantic = { timestamp = "0001-01-01T00:00:00Z", span = "P3D" }\n',
        1,
    )
    result = run_release_age(repo_root, lock, tmp_path)
    assert result.returncode == 0, result.stdout
    assert "::warning::uv.lock: exclude-newer-package 'boto3'" in result.stdout
    assert "::warning::uv.lock: exclude-newer-package 'pydantic'" in result.stdout


def test_release_age_check_prints_lock_values_without_workflow_commands(
    repo_root: Path, tmp_path: Path
) -> None:
    lock = (repo_root / "uv.lock").read_text(encoding="utf-8")
    lock = lock.replace(
        'exclude-newer-span = "PT24H"\n',
        'exclude-newer-span = "PT24H"\n\n[options.exclude-newer-package]\n'
        '"x\\n::error::injected" = false\n',
        1,
    )
    result = run_release_age(repo_root, lock, tmp_path)
    assert result.returncode == 1
    assert "injected" in result.stdout
    assert not any(line.startswith("::error::") for line in result.stdout.splitlines())


def tippecanoe_commit(text: str) -> str:
    commits = re.findall(r"TIPPECANOE_COMMIT: ([0-9a-f]{40}) # tag 2\.79\.0", text)
    assert len(commits) == 1, commits
    return str(commits[0])


def test_ci_test_job_builds_the_publish_tippecanoe(repo_root: Path) -> None:
    # Without tippecanoe the PMTiles build test is skipped and fixture --check reuses the
    # committed archive, so a TIPPECANOE_ARGS regression first failed in publish.yml on main
    # (pmtiles-never-in-ci). The test job builds the same pinned commit as publish.yml.
    workflows = repo_root / ".github" / "workflows"
    job = ci_job(repo_root, "test")
    publish = (workflows / "publish.yml").read_text(encoding="utf-8")
    assert tippecanoe_commit(job) == tippecanoe_commit(publish)
    build = job.index("make -j")
    assert job.index("apt-get update") < job.index("apt-get install") < build
    assert build < job.index('>> "$GITHUB_PATH"') < job.index("tippecanoe --version")
    assert job.index("tippecanoe --version") < job.index("uv run --locked pytest")
    assert "timeout-minutes: 25" in job
    assert "uses: actions/cache" not in (workflows / "ci.yml").read_text(encoding="utf-8")
