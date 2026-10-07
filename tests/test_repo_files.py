from __future__ import annotations

import hashlib
import json
import re
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
