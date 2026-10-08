"""The owner runbooks say what the code and the plan require (docs/publishing.md, docs/r2-setup.md)."""

from __future__ import annotations

import re
from pathlib import Path

from atlas import publish

DOCS = Path(__file__).resolve().parents[2] / "docs"


def text(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def commands(doc: str, verb: str) -> list[str]:
    """Every shell command line (joined across trailing backslashes) that contains verb."""
    joined = re.sub(r"\\\n\s*", " ", doc)
    return [line for line in joined.splitlines() if verb in line and not line.startswith("#")]


def test_tiles_domain_gets_minimum_tls_1_2() -> None:
    # Wrangler's domain add defaults to TLS 1.0 and the dashboard flow has no TLS field (10 §3.2).
    doc = text("r2-setup.md")
    add = commands(doc, "wrangler r2 bucket domain add")
    assert add and all("--min-tls 1.2" in line for line in add)
    assert any(
        "--min-tls 1.2" in line for line in commands(doc, "wrangler r2 bucket domain update")
    )
    assert "-tls1_1 -cipher 'DEFAULT:@SECLEVEL=0'" in re.sub(r"\\\n\s*", " ", doc)


def test_publishing_documents_the_takedown_and_csv_escaping() -> None:
    doc = text("publishing.md")
    assert "## 7. Takedowns (07 §5.4)" in doc
    assert "atlas publish takedown --id" in doc
    assert "`takedown_apply`" in doc
    assert "In `facilities.csv` only, a text cell that starts with `=`" in doc
    assert "`application/vnd.apache.parquet`" in doc
    assert "application/octet-stream` | GeoParquet" not in doc


def test_fixture_command_in_the_docs_matches_fixture_options() -> None:
    doc = re.sub(r"\s+", " ", text("publishing.md"))
    opts = publish.fixture_options(Path("fixtures/release"))
    root = publish.REPO_ROOT
    for flag, path in (
        ("--layers", opts.layers_path),
        ("--attribution", opts.attribution_path),
        ("--changelog", opts.changelog_path),
    ):
        assert f"{flag} {path.relative_to(root).as_posix()}" in doc
