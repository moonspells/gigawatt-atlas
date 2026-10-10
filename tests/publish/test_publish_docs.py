"""The owner runbooks say what the code and the plan require (docs/publishing.md, docs/r2-setup.md)."""

from __future__ import annotations

import re
from pathlib import Path

from atlas import publish, r2

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
WORKFLOWS = ROOT / ".github" / "workflows"


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


def step_name(step: str) -> str:
    name = re.search(r"^(?:\s*)name: (.+)$", step, flags=re.M)
    return name[1] if name else step


def test_step_9_says_which_token_values_go_where() -> None:
    # Cloudflare's token page shows a Token value next to the S3 key pair, and the runbook named
    # only the pair, so the owner could not tell whether to keep it (SV2-4, RD-R2, R2-1, RD2-5).
    step = section(text("r2-setup.md"), 9)
    assert "three values: **Token value**, **Access Key ID** and **Secret Access Key**" in step
    assert (
        "Only the Access Key ID and the Secret Access Key go into the `production` Environment"
        in step
    )
    assert "The **Token value** is not stored anywhere" in step
    # The runbook's claim holds: the pipeline reads the key pair and no Cloudflare API token.
    names = (r2.ENV_ACCOUNT, r2.ENV_ACCESS_KEY, r2.ENV_SECRET_KEY)
    assert all(f"`{name}`" in step for name in names)
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = path.read_text(encoding="utf-8")
        assert not re.search(r"CLOUDFLARE_API_TOKEN|CF_API_TOKEN|secrets\.CF_", workflow), path
    # ... and the secrets reach the steps the runbook names, and no others.
    holders = sorted(
        f"{path.name}: {step_name(step)}"
        for path in WORKFLOWS.glob("*.yml")
        for step in re.split(r"^      - ", path.read_text(encoding="utf-8"), flags=re.M)
        if "secrets.R2_" in step
    )
    assert holders == [
        "basemap.yml: Upload to R2 basemap/",
        "publish.yml: Delete the records from rec/ and from every stored release except the live one",
        "publish.yml: Upload to R2 (v/, rec/, then atlas/latest.json unless the fixture)",
    ], holders
    assert (
        "The secrets reach only the upload steps of `publish.yml` and `basemap.yml` and the "
        "takedown step of `publish.yml`" in step
    )


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


def test_wrangler_runs_from_the_site_clone() -> None:
    # pnpm exec uses the site's pinned wrangler (npx would fetch the newest one), so the commands
    # run in the site clone, and the CORS file is named by its path in the atlas clone.
    doc = text("r2-setup.md")
    assert "npx wrangler" not in doc
    lines = commands(doc, "wrangler r2 ")
    assert lines and all(line.startswith("pnpm exec wrangler r2 ") for line in lines)
    (cors,) = commands(doc, "wrangler r2 bucket cors set")
    assert cors.endswith(" atlas-tiles --file <gigawatt-atlas clone>/r2/cors.json")
    assert (ROOT / "r2" / "cors.json").is_file()
