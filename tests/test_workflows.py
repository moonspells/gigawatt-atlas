"""Workflow hygiene (10 §8.4), checked as text so no YAML parser is needed.

zizmor in CI covers more; these checks fail fast and locally.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"))
USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$")
PINNED_RE = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}$")
VERSION_COMMENT_RE = re.compile(r"^\s+#\s+v\d+\.\d+\.\d+\s*$")
RUN_RE = re.compile(r"^(\s*)(?:-\s+)?run:\s*(.*)$")


def ids(paths: list[Path]) -> list[str]:
    return [p.name for p in paths]


def test_there_are_workflows() -> None:
    assert "ci.yml" in ids(WORKFLOWS)


@pytest.mark.parametrize("path", WORKFLOWS, ids=ids(WORKFLOWS))
def test_actions_are_pinned_to_a_sha_with_a_version_comment(path: Path) -> None:
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = USES_RE.match(line)
        if not m:
            continue
        ref, rest = m.group(1), m.group(2)
        assert PINNED_RE.match(ref), f"{path.name}:{n}: {ref} is not owner/repo@<40-hex sha>"
        assert VERSION_COMMENT_RE.match(rest), f"{path.name}:{n}: missing '# vX.Y.Z' comment"


def step_block(lines: list[str], index: int) -> list[str]:
    """The lines of the step that contains lines[index]."""
    start = index
    while start > 0 and not lines[start].lstrip().startswith("- "):
        start -= 1
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = start + 1
    while end < len(lines):
        line = lines[end]
        stripped = line.lstrip()
        current = len(line) - len(stripped)
        if stripped and (current < indent or (current == indent and stripped.startswith("- "))):
            break
        end += 1
    return lines[start:end]


@pytest.mark.parametrize("path", WORKFLOWS, ids=ids(WORKFLOWS))
def test_checkout_never_persists_credentials(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if re.search(r"uses:\s*actions/checkout@", line):
            block = "\n".join(step_block(lines, i))
            assert re.search(r"persist-credentials:\s*false", block), f"{path.name}:{i + 1}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=ids(WORKFLOWS))
def test_no_pull_request_target(path: Path) -> None:
    assert "pull_request_target" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", WORKFLOWS, ids=ids(WORKFLOWS))
def test_top_level_permissions_are_empty(path: Path) -> None:
    assert re.search(r"^permissions: \{\}\s*$", path.read_text(encoding="utf-8"), re.M)


def run_blocks(text: str) -> list[tuple[int, str]]:
    """(line number, text) of every run: value, including multi-line block scalars."""
    lines = text.splitlines()
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        m = RUN_RE.match(lines[i])
        if not m:
            i += 1
            continue
        key_indent = len(m.group(1)) + (2 if lines[i].lstrip().startswith("- ") else 0)
        value = m.group(2)
        body = [value]
        j = i + 1
        if value.startswith(("|", ">")) or value == "":
            while j < len(lines) and (
                not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > key_indent
            ):
                body.append(lines[j])
                j += 1
        out.append((i + 1, "\n".join(body)))
        i = j
    return out


@pytest.mark.parametrize("path", WORKFLOWS, ids=ids(WORKFLOWS))
def test_no_expressions_inside_run(path: Path) -> None:
    for n, body in run_blocks(path.read_text(encoding="utf-8")):
        assert "${{" not in body, (
            f"{path.name}:{n}: pass values through env:, not ${{{{ }}}} in run"
        )


def test_run_block_parser_sees_multiline_scripts() -> None:
    text = "jobs:\n  a:\n    steps:\n      - run: |\n          echo ok\n          echo ${{ x }}\n      - run: echo b\n"
    blocks = run_blocks(text)
    assert len(blocks) == 2
    assert "${{ x }}" in blocks[0][1]
    assert blocks[1][1] == "echo b"
