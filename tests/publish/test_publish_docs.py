"""The owner runbooks say what the code and the plan require (docs/publishing.md, docs/r2-setup.md)."""

from __future__ import annotations

import re
from pathlib import Path

from atlas import publish

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"


def text(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def section(doc: str, number: int) -> str:
    """The body of the `## {number}.` section, whitespace collapsed to single spaces."""
    start = doc.index(f"\n## {number}. ")
    stop = doc.find("\n## ", start + 1)
    return re.sub(r"\s+", " ", doc[start : stop if stop != -1 else len(doc)])


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


def test_fixture_renewal_puts_the_objects_again() -> None:
    # The lifecycle rule deletes v/ objects 120 days after their last upload, and a plain upload
    # skips an object stored with the same SHA-256, so "dispatch the fixture again" alone left
    # the fixture's age unchanged (RD2-1). The renewal the docs give must use --renew.
    from atlas.cli import build_parser

    lifecycle = section(text("r2-setup.md"), 7)
    assert "dispatch `publish.yml` with `fixture` again" not in lifecycle
    assert "`atlas publish upload --renew`" in lifecycle
    assert "A plain upload skips those objects and leaves their age as it was" in lifecycle
    fixture = section(text("publishing.md"), 6)
    assert "Uploading it again is a no-op" not in fixture
    assert "`--no-latest --renew`" in fixture
    args = build_parser().parse_args(
        ["publish", "upload", "fixtures/release", "--release", "20000101-0000", "--renew"]
    )
    assert args.renew is True
