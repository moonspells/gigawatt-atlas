"""`atlas publish build`, `verify`, `upload --local-target` and `put` end to end."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from atlas.cli import main

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RELEASE = "20261012-1200"
R2_VARS = (
    "CF_ACCOUNT_ID",
    "R2_TILES_ACCESS_KEY_ID",
    "R2_TILES_SECRET_ACCESS_KEY",
    "R2_TILES_BUCKET",
)


def build_args(out: Path, *extra: str) -> list[str]:
    return [
        "publish",
        "build",
        "--records",
        str(FIXTURES / "records"),
        "--orgs",
        str(FIXTURES / "orgs.json"),
        "--out",
        str(out),
        "--release",
        RELEASE,
        "--previous",
        "none",
        "--skip-pmtiles",
        *extra,
    ]


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def no_r2_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*R2_VARS, "ATLAS_TILES_BASE", "ATLAS_PR_NUMBER", "GITHUB_SHA"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def built(tmp_path: Path, no_r2_env: None) -> Path:
    out = tmp_path / "release"
    assert main(build_args(out, "--deterministic")) == 0
    return out


def test_build_verify_upload_end_to_end(
    built: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["publish", "verify", str(built)]) == 0
    target = tmp_path / "bucket"
    assert (
        main(["publish", "upload", str(built), "--release", RELEASE, "--local-target", str(target)])
        == 0
    )
    out = capsys.readouterr().out
    assert f"upload {RELEASE}:" in out
    stored = sorted(
        p.relative_to(target).as_posix()
        for p in target.rglob("*")
        if p.is_file() and not p.name.endswith(".meta.json")
    )
    built_files = sorted(p.relative_to(built).as_posix() for p in built.rglob("*") if p.is_file())
    assert stored == built_files
    for key in stored:
        assert (target / key).read_bytes() == (built / key).read_bytes()
        meta = read(target / f"{key}.meta.json")
        assert set(meta) == {"bytes", "cache_control", "content_type", "sha256"}
    assert read(target / "atlas/latest.json.meta.json")["cache_control"] == "public, max-age=60"
    pmeta = read(target / f"v/{RELEASE}/facilities.parquet.meta.json")
    assert pmeta["content_type"] == "application/octet-stream"
    assert pmeta["cache_control"] == "public, max-age=31536000, immutable"
    # Re-running is a no-op for immutable keys; latest.json is rewritten.
    assert (
        main(["publish", "upload", str(built), "--release", RELEASE, "--local-target", str(target)])
        == 0
    )
    assert "1 uploaded" in capsys.readouterr().out


def test_deterministic_build_has_no_clock_or_git(built: Path) -> None:
    manifest = read(built / "v" / RELEASE / "manifest.json")
    assert manifest["generated_at"] == "2026-10-12T12:00:00Z"
    assert manifest["git"] == {"commit": None, "pr_number": None}


def test_build_reads_git_and_pr_from_env(
    tmp_path: Path, no_r2_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("ATLAS_PR_NUMBER", "42")
    monkeypatch.setenv("ATLAS_TILES_BASE", "https://tiles.example.org")
    github_output = tmp_path / "github_output"
    github_output.write_text("before=1\n", encoding="utf-8")
    out = tmp_path / "release"
    assert main(build_args(out, "--github-output", str(github_output))) == 0
    manifest = read(out / "v" / RELEASE / "manifest.json")
    assert manifest["git"] == {"commit": "a" * 40, "pr_number": 42}
    assert manifest["base_url"] == f"https://tiles.example.org/v/{RELEASE}/"
    assert (
        github_output.read_text(encoding="utf-8") == f"before=1\nrelease={RELEASE}\nlatest=true\n"
    )
    pointer = read(out / "atlas" / "latest.json")
    assert pointer["manifest"] == f"https://tiles.example.org/v/{RELEASE}/manifest.json"


def test_fixture_build_reports_latest_false(tmp_path: Path, no_r2_env: None) -> None:
    github_output = tmp_path / "github_output"
    out = tmp_path / "release"
    assert (
        main(build_args(out, "--fixture", "--deterministic", "--github-output", str(github_output)))
        == 0
    )
    assert github_output.read_text(encoding="utf-8") == f"release={RELEASE}\nlatest=false\n"
    assert read(out / "v" / RELEASE / "manifest.json")["git"]["commit"] == "fixture"
    assert not (out / "atlas").exists()


def test_build_failures_exit_1(
    tmp_path: Path, no_r2_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    previous = tmp_path / "manifest.json"
    previous.write_text(
        json.dumps({"release": "20261011-0000", "counts": {"facilities": 100}}), "utf-8"
    )
    args = build_args(tmp_path / "release")
    args[args.index("--previous") + 1] = str(previous)
    assert main(args) == 1
    assert "more than ±10%" in capsys.readouterr().err
    assert main([*args, "--allow-count-change"]) == 0
    assert main(build_args(tmp_path / "x", "--release", "2026")) == 2


def test_tampered_release_fails_verify_and_upload(
    built: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv = built / "v" / RELEASE / "facilities.csv"
    csv.write_bytes(csv.read_bytes() + b"x")
    assert main(["publish", "verify", str(built)]) == 1
    assert "SHA-256 does not match" in capsys.readouterr().err
    target = tmp_path / "bucket"
    assert (
        main(["publish", "upload", str(built), "--release", RELEASE, "--local-target", str(target)])
        == 1
    )
    assert not target.exists()


def test_disallowed_file_fails_verify(built: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (built / "v" / RELEASE / "index.html").write_text("<script></script>", encoding="utf-8")
    assert main(["publish", "verify", str(built)]) == 1
    err = capsys.readouterr().err
    assert "allow-list" in err and "not listed in the manifest" in err


def test_upload_without_credentials_names_the_variables(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["publish", "upload", str(built), "--release", RELEASE]) == 1
    err = capsys.readouterr().err
    assert "CF_ACCOUNT_ID" in err and "R2_TILES_SECRET_ACCESS_KEY" in err
    assert main(["publish", "upload", str(built), "--release", RELEASE, "--dry-run"]) == 0


def test_put_basemap_rules(
    tmp_path: Path, no_r2_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = tmp_path / "conus-z10-20261006.pmtiles"
    archive.write_bytes(b"PMTiles\x03" + b"\0" * 200)
    target = tmp_path / "bucket"
    key = "basemap/conus-z10-20261006.pmtiles"
    put = ["publish", "put", str(archive), "--key", key, "--local-target", str(target)]
    assert main([*put, "--immutable", "--no-overwrite"]) == 0
    meta = read(target / f"{key}.meta.json")
    assert meta["content_type"] == "application/octet-stream"
    assert meta["cache_control"] == "public, max-age=31536000, immutable"
    assert main([*put, "--immutable", "--no-overwrite"]) == 0
    assert "already stored" in capsys.readouterr().out
    archive.write_bytes(b"PMTiles\x03" + b"\1" * 200)
    assert main([*put, "--immutable", "--no-overwrite"]) == 1
    assert "never overwritten" in capsys.readouterr().err
    assert main([*put, "--short-cache"]) == 1
    assert "use --immutable" in capsys.readouterr().err
    assert (
        main(["publish", "put", str(archive), "--key", key, "--immutable"]) == 1
    )  # no credentials
    html = tmp_path / "x.html"
    html.write_text("<p>", encoding="utf-8")
    assert (
        main(["publish", "put", str(html), "--key", "basemap/x.html", "--immutable", "--dry-run"])
        == 1
    )
    assert main(["publish", "put", str(archive), "--key", key]) == 2  # a cache mode is required
