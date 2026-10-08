"""publish.yml and basemap.yml, checked as text (no YAML parser), plus the records gate run in bash.

tests/test_workflows.py covers the hygiene rules shared by every workflow; these are the publish
pipeline's own rules (07 §6.8, §5.4).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
PUBLISH = WORKFLOWS / "publish.yml"


def jobs(text: str) -> dict[str, str]:
    """{job id: its text} for the two-space-indented jobs under `jobs:`."""
    body = text[text.index("\njobs:\n") + len("\njobs:\n") :]
    parts = re.split(r"^  ([a-z][a-z0-9_-]*):\n", body, flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def steps(job: str) -> list[str]:
    """The text of each step of a job, in order."""
    section = job[job.index("    steps:\n") :]
    return [f"- {s}" for s in re.split(r"^      - ", section, flags=re.M)[1:]]


def run_script(step: str) -> str:
    """The body of a step's `run: |` block, dedented."""
    lines = step.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "run: |")
    body = [line[10:] for line in lines[start + 1 :] if line.startswith("          ")]
    return "\n".join(body) + "\n"


@pytest.mark.parametrize("name", ["publish.yml", "basemap.yml"])
def test_apt_get_install_follows_an_update(name: str) -> None:
    for job in jobs((WORKFLOWS / name).read_text(encoding="utf-8")).values():
        for step in steps(job):
            if "apt-get install" in step:
                assert step.index("apt-get update") < step.index("apt-get install"), step


def publish_jobs() -> dict[str, str]:
    return jobs(PUBLISH.read_text(encoding="utf-8"))


def gate_step() -> str:
    (step,) = [s for s in steps(publish_jobs()["build"]) if "id: records" in s]
    return step


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    ("records", "fixture", "skip"),
    [([], "false", "true"), (["gwa-x.json"], "false", "false"), ([], "true", "false")],
)
def test_records_gate(tmp_path: Path, records: list[str], fixture: str, skip: str) -> None:
    # The first merge to main (before the seed PR) has no records: the run must end green with a
    # notice and upload nothing, instead of failing on "no in-scope facility to publish".
    (tmp_path / "data" / "records").mkdir(parents=True)
    (tmp_path / "data" / "records" / ".gitkeep").touch()
    for name in records:
        (tmp_path / "data" / "records" / name).write_text("{}", encoding="utf-8")
    output = tmp_path / "github_output"
    output.touch()
    env = {"PATH": os.environ["PATH"], "FIXTURE": fixture, "GITHUB_OUTPUT": str(output)}
    result = subprocess.run(  # noqa: S603  (fixed argv: bash reading the workflow's own script)
        ["bash", "-e", "-c", run_script(gate_step())],  # noqa: S607
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert output.read_text(encoding="utf-8") == f"skip={skip}\n"
    assert ("::notice::" in result.stdout) is (skip == "true")


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    ("fixture", "latest", "flags"),
    [("true", "false", ["--no-latest", "--renew"]), ("false", "true", [])],
)
def test_fixture_upload_renews_its_objects(
    tmp_path: Path, fixture: str, latest: str, flags: list[str]
) -> None:
    # The fixture is re-dispatched to outlive the 120-day lifecycle rule on v/; without --renew
    # the upload skips every object it already holds and their age does not restart (RD2-1).
    # A real release never renews: its rec/ objects are shared with earlier releases.
    (step,) = [s for s in steps(publish_jobs()["upload"]) if "atlas publish upload" in s]
    assert "FIXTURE: ${{ inputs.fixture == true }}" in step
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGS_OUT"\n', encoding="utf-8")
    fake_uv.chmod(0o755)
    args_out = tmp_path / "args"
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "RELEASE": "20000101-0000" if fixture == "true" else "20261012-1200",
        "LATEST": latest,
        "FIXTURE": fixture,
        "ARGS_OUT": str(args_out),
    }
    subprocess.run(  # noqa: S603  (fixed argv: bash reading the workflow's own script)
        ["bash", "-e", "-c", run_script(step)],  # noqa: S607
        cwd=tmp_path,
        env=env,
        check=True,
    )
    argv = args_out.read_text(encoding="utf-8").splitlines()
    head = ["run", "--locked", "atlas", "publish", "upload", f"{tmp_path}/release"]
    assert argv == [*head, "--release", env["RELEASE"], *flags]


def test_every_later_build_step_honours_the_gate() -> None:
    build = steps(publish_jobs()["build"])
    gate = next(i for i, s in enumerate(build) if "id: records" in s)
    assert "actions/checkout@" in build[0] and gate == 1  # nothing slow runs before the gate
    for step in build[gate + 1 :]:
        assert "if: steps.records.outputs.skip != 'true'" in step, step
    upload = publish_jobs()["upload"]
    assert "    if: needs.build.outputs.release != ''\n" in upload


def test_takedown_runs_alone_and_dry_by_default() -> None:
    all_jobs = publish_jobs()
    assert "    if: inputs.takedown == ''\n" in all_jobs["build"]
    job = all_jobs["takedown"]
    assert "    if: github.event_name == 'workflow_dispatch' && inputs.takedown != ''\n" in job
    assert "    environment: production\n" in job
    assert "    permissions: { contents: read }\n" in job
    (step,) = [s for s in steps(job) if "atlas publish takedown" in s]
    assert "IDS: ${{ inputs.takedown }}" in step  # through env, never inside run
    script = run_script(step)
    assert 'if [ "$APPLY" != "true" ]; then args+=(--dry-run); fi' in script
    assert "$IDS" in script and "${{" not in script
