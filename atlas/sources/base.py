"""The importer framework (07 §4.2, §4.3).

An importer reads its upstream files and returns an ImportResult: candidate records (with
PLACEHOLDER_ID), review items and metrics. apply_import matches candidates to stored records by
external id, keeps what reviewers own, validates every record it would write, and writes the
records, the review queue files and the import receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import AwareDatetime, Field, HttpUrl, JsonValue

from atlas import __version__
from atlas.jsonio import dumps_compact, dumps_pretty, record_json, write_text
from atlas.schema.record import PLACEHOLDER_ID, AtlasModel, FacilityRecord
from atlas.validate import validate_record

if TYPE_CHECKING:
    import httpx

    from atlas.geo.counties import CountyIndex
    from atlas.net import FetchResult
    from atlas.store import RecordStore

REVIEW_KINDS = (
    "unmatched",
    "missing_location",
    "unverified_upstream",
    "unknown_status",
    "unit_parse",
    "county_mismatch",
    "out_of_scope",
    "invalid",
    "conflict",
    "held_human_reviewed",
    "held_merged",
    "removed_upstream",
    "possible_duplicate",
    "geocode_failed",
)
COUNT_KEYS = (
    "candidates",
    "new",
    "updated",
    "unchanged",
    "held",
    "invalid",
    "conflicts",
    "removed_upstream",
    "review",
)
DEFAULT_CACHE_DIR = Path(".cache/atlas")
_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class InputSnapshot(AtlasModel):
    """One upstream file read by an importer, recorded in the receipt."""

    name: str
    url: HttpUrl
    retrieved_at: AwareDatetime
    sha256: str
    bytes: int
    license: str
    upstream_version: str | None = None
    etag: str | None = None
    last_modified: str | None = None


class ReviewItem(AtlasModel):
    """A line in review/queue/{source}.jsonl. kind is one of REVIEW_KINDS."""

    source: str
    kind: str
    external_id: str | None = None
    record_id: str | None = None
    reason: str
    data: dict[str, JsonValue] = Field(default_factory=dict)


class ImportReceipt(AtlasModel):
    """data/imports/{importer}.json: what one import read and did."""

    source: str
    importer_version: str
    atlas_version: str
    git_commit: str | None
    ran_at: AwareDatetime
    inputs: list[InputSnapshot]
    counts: dict[str, int]
    metrics: dict[str, float | int]


@dataclass(frozen=True)
class Candidate:
    """One record an importer proposes. match_values are its external_ids[match_key] values."""

    match_values: tuple[str, ...]
    record: FacilityRecord

    def __post_init__(self) -> None:
        if not self.match_values:
            raise ValueError("a candidate needs at least one match value")


@dataclass
class ImportResult:
    source: str
    inputs: list[InputSnapshot]
    candidates: list[Candidate]
    review: list[ReviewItem]
    metrics: dict[str, float | int] = field(default_factory=dict)


class ImportContext:
    """What an importer may use. Tests build one with the make_test_context fixture."""

    def __init__(
        self,
        *,
        now: datetime,
        today: date,
        records: Mapping[str, FacilityRecord],
        http: httpx.Client,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        input_path: Path | None = None,
        user_agent: str,
        new_id: Callable[[], str],
        counties_path: Path | None = None,
        counties: CountyIndex | None = None,
    ) -> None:
        if now.tzinfo is None:
            raise ValueError("ImportContext.now must be timezone-aware")
        self.now = now
        self.today = today
        self.records = records
        self.http = http
        self.cache_dir = cache_dir
        self.input_path = input_path
        self.user_agent = user_agent
        self.new_id = new_id
        self._counties_path = counties_path
        self._counties = counties

    def counties(self) -> CountyIndex:
        """The county index, loaded once."""
        if self._counties is None:
            from atlas.geo.counties import COUNTIES_ZIP, CountyIndex  # DuckDB on use

            self._counties = CountyIndex.load(self._counties_path or COUNTIES_ZIP)
        return self._counties

    def raw_path(self, source: str, sha256: str, ext: str) -> Path:
        """.cache/atlas/raw/{source}/{sha256}.{ext}"""
        segment = _SAFE_SEGMENT_RE.sub("_", source) or "_"
        return self.cache_dir / "raw" / segment / f"{sha256}.{ext.lstrip('.')}"


class Importer(Protocol):
    """What `atlas import` runs. Members are read-only, so plain class attributes satisfy them
    (for example `owned_external_keys = ("osm", "pnnl_im3")`)."""

    @property
    def name(self) -> str:
        """CLI name and receipt file: "osm" | "epoch" | "aigridwatch"."""
        ...

    @property
    def match_key(self) -> str:
        """external_ids key used to find existing records: "osm" | "epoch_name" | "aigridwatch_id"."""
        ...

    @property
    def owned_external_keys(self) -> tuple[str, ...]:
        """external_ids keys this importer rewrites, e.g. ("osm", "pnnl_im3")."""
        ...

    @property
    def review_sources(self) -> tuple[str, ...]:
        """review/queue/{x}.jsonl files it owns, e.g. ("osm", "pnnl"); must include name."""
        ...

    @property
    def version(self) -> str:
        """Bump when the mapping changes; written to the receipt."""
        ...

    @property
    def help(self) -> str:
        """One line for `atlas import --help`."""
        ...

    def add_arguments(self, parser: argparse.ArgumentParser) -> None: ...

    def run(self, ctx: ImportContext, args: argparse.Namespace) -> ImportResult: ...


def load_input(
    ctx: ImportContext,
    *,
    name: str,
    url: str,
    license: str,
    ext: str,
    fetch: Callable[[], FetchResult],
    upstream_version: str | None = None,
    path: Path | None = None,
    use_ctx_input: bool = True,
) -> tuple[bytes, InputSnapshot]:
    """Read one upstream file and describe it.

    The file comes from path if given, else from ctx.input_path (--input) unless use_ctx_input is
    False; a local file's retrieved_at is ctx.now. Otherwise fetch() is called and the raw bytes
    are kept at ctx.raw_path(name, sha256, ext).
    """
    local = path if path is not None else (ctx.input_path if use_ctx_input else None)
    if local is not None:
        data = local.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        snapshot = InputSnapshot(
            name=name,
            url=HttpUrl(url),
            retrieved_at=ctx.now,
            sha256=digest,
            bytes=len(data),
            license=license,
            upstream_version=upstream_version,
        )
        return data, snapshot
    result = fetch()
    data = result.content
    raw = ctx.raw_path(name, result.sha256, ext)
    if not raw.exists():
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(data)
    snapshot = InputSnapshot(
        name=name,
        url=HttpUrl(url),
        retrieved_at=result.retrieved_at,
        sha256=result.sha256,
        bytes=len(data),
        license=license,
        upstream_version=upstream_version,
        etag=result.headers.get("etag"),
        last_modified=result.headers.get("last-modified"),
    )
    return data, snapshot


def _comparable(record: FacilityRecord) -> dict[str, JsonValue]:
    """The record without timestamps that change on every run."""
    doc = record_json(record)
    for key in ("created_at", "updated_at", "last_verified_at"):
        doc.pop(key, None)
    sources = doc.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, dict):
                source.pop("retrieved_at", None)
    return doc


def git_commit(cwd: Path | None = None) -> str | None:
    """`git rev-parse HEAD`, or None outside a git checkout."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603  (fixed argv, no shell)
            [git, "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = out.stdout.strip()
    return commit if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", commit) else None


def _review_sort_key(item: ReviewItem) -> tuple[str, str, str, str]:
    return (item.kind, item.external_id or "", item.record_id or "", item.reason)


def apply_import(
    importer: Importer,
    result: ImportResult,
    ctx: ImportContext,
    *,
    store: RecordStore,
    review_dir: Path,
    receipts_dir: Path,
    dry_run: bool = False,
) -> ImportReceipt:
    """Merge an import into the store and return the receipt (see merge_import)."""
    receipt, _ = merge_import(
        importer,
        result,
        ctx,
        store=store,
        review_dir=review_dir,
        receipts_dir=receipts_dir,
        dry_run=dry_run,
    )
    return receipt


def merge_import(
    importer: Importer,
    result: ImportResult,
    ctx: ImportContext,
    *,
    store: RecordStore,
    review_dir: Path,
    receipts_dir: Path,
    dry_run: bool = False,
) -> tuple[ImportReceipt, list[ReviewItem]]:
    """Merge an import into the store; return the receipt and every review item, sorted.

    1. Stored records are indexed by external_ids[importer.match_key].
    2. A candidate matching more than one record is a conflict; one record is an update; none is new.
    3. New records get ctx.new_id() and ctx.now timestamps. A match whose review.state is not
       "machine" is held (held_human_reviewed), and so is a merged one (held_merged). Otherwise the
       candidate keeps the stored id, created_at, review, corrections, merged_into and every
       external_ids key it does not own. If nothing but timestamps and sources[*].retrieved_at
       changed, the record is unchanged and not rewritten; else updated_at and last_verified_at
       become ctx.now.
    4. Every record to be written must pass validate_record, or it becomes an "invalid" item.
    5. Stored records with match_key values that no candidate matched get "removed_upstream".
       Records are never deleted (07 §2.1).
    6. Unless dry_run: changed records, review/queue/{s}.jsonl for every s in
       importer.review_sources (rewritten, sorted, empty when there is nothing) and
       data/imports/{importer.name}.json are written.
    """
    if importer.name not in importer.review_sources:
        raise ValueError(f"importer {importer.name!r}: review_sources must include its name")
    for given in result.review:
        if given.source not in importer.review_sources:
            raise ValueError(
                f"review item source {given.source!r} is not in {importer.review_sources}"
            )

    stored = store.load()
    index: dict[str, set[str]] = {}
    for rid, record in stored.items():
        for value in record.external_ids.get(importer.match_key, []):
            index.setdefault(value, set()).add(rid)

    counts = dict.fromkeys(COUNT_KEYS, 0)
    counts["candidates"] = len(result.candidates)
    review: list[ReviewItem] = list(result.review)
    to_write: list[FacilityRecord] = []
    seen_ids: set[str] = set()
    claimed: dict[str, str] = {}  # stored or new id -> first match value that claimed it
    claimed_values: dict[str, str] = {}  # match value -> id it went to in this run
    counties = ctx.counties()

    def flag(
        kind: str,
        reason: str,
        *,
        external_id: str | None,
        record_id: str | None,
        data: dict[str, JsonValue] | None = None,
    ) -> None:
        review.append(
            ReviewItem(
                source=importer.name,
                kind=kind,
                external_id=external_id,
                record_id=record_id,
                reason=reason,
                data=data or {},
            )
        )

    for candidate in result.candidates:
        key = candidate.match_values[0]
        matched = sorted(set().union(*(index.get(v, set()) for v in candidate.match_values)))
        seen_ids.update(matched)
        if len(matched) > 1:
            counts["conflicts"] += 1
            flag(
                "conflict",
                f"{importer.match_key} values match {len(matched)} records",
                external_id=key,
                record_id=None,
                data={"record_ids": list(matched), "match_values": list(candidate.match_values)},
            )
            continue
        earlier = {claimed_values[v] for v in candidate.match_values if v in claimed_values}
        if earlier or (matched and matched[0] in claimed):
            counts["conflicts"] += 1
            flag(
                "conflict",
                "another candidate in this run has the same match values",
                external_id=key,
                record_id=matched[0] if matched else None,
                data={"match_values": list(candidate.match_values)},
            )
            continue

        incoming = candidate.record
        if not matched:
            final = incoming.model_copy(
                update={
                    "id": ctx.new_id(),
                    "created_at": ctx.now,
                    "updated_at": ctx.now,
                    "last_verified_at": ctx.now,
                }
            )
            outcome = "new"
        else:
            existing = stored[matched[0]]
            if existing.review.state != "machine":
                counts["held"] += 1
                flag(
                    "held_human_reviewed",
                    f"review.state is {existing.review.state}; changes go through a reviewed PR",
                    external_id=key,
                    record_id=existing.id,
                )
                claimed[existing.id] = key
                continue
            if existing.merged_into is not None:
                counts["held"] += 1
                flag(
                    "held_merged",
                    f"record is merged into {existing.merged_into}",
                    external_id=key,
                    record_id=existing.id,
                )
                claimed[existing.id] = key
                continue
            external_ids = {
                k: v
                for k, v in existing.external_ids.items()
                if k not in importer.owned_external_keys
            }
            external_ids.update(incoming.external_ids)
            final = incoming.model_copy(
                update={
                    "id": existing.id,
                    "created_at": existing.created_at,
                    "updated_at": existing.updated_at,
                    "last_verified_at": existing.last_verified_at,
                    "review": existing.review,
                    "corrections": existing.corrections,
                    "merged_into": existing.merged_into,
                    "external_ids": external_ids,
                }
            )
            if _comparable(final) == _comparable(existing):
                counts["unchanged"] += 1
                claimed[existing.id] = key
                for v in candidate.match_values:
                    claimed_values[v] = existing.id
                continue
            final = final.model_copy(update={"updated_at": ctx.now, "last_verified_at": ctx.now})
            outcome = "updated"

        # Round-trip through validation, so a record that only model_copy accepted is caught.
        try:
            final = FacilityRecord.model_validate(record_json(final))
        except ValueError as e:
            counts["invalid"] += 1
            flag(
                "invalid",
                "candidate does not pass the record schema",
                external_id=key,
                record_id=None if final.id == PLACEHOLDER_ID else final.id,
                data={"error": str(e)},
            )
            continue
        issues = validate_record(final, counties=counties, today=ctx.today)
        if issues:
            counts["invalid"] += 1
            flag(
                "invalid",
                f"{len(issues)} validation issue(s); not written",
                external_id=key,
                record_id=final.id if outcome == "updated" else None,
                data={
                    "issues": [
                        {"rule": i.rule, "pointer": i.pointer, "message": i.message} for i in issues
                    ]
                },
            )
            continue
        counts[outcome] += 1
        claimed[final.id] = key
        for v in candidate.match_values:
            claimed_values[v] = final.id
        to_write.append(final)

    for rid in sorted(stored):
        values = stored[rid].external_ids.get(importer.match_key, [])
        if values and rid not in seen_ids:
            counts["removed_upstream"] += 1
            flag(
                "removed_upstream",
                f"no {importer.name} candidate matched; the record is kept (07 §2.1)",
                external_id=values[0],
                record_id=rid,
            )

    counts["review"] = len(review)
    receipt = ImportReceipt(
        source=importer.name,
        importer_version=importer.version,
        atlas_version=__version__,
        git_commit=git_commit(),
        ran_at=ctx.now,
        inputs=list(result.inputs),
        counts=counts,
        metrics=dict(result.metrics),
    )
    review.sort(key=_review_sort_key)
    if dry_run:
        return receipt, review

    for record in to_write:
        store.write(record)
    for source in importer.review_sources:
        lines = [
            dumps_compact(record_json(i)).decode("utf-8") + "\n"
            for i in review
            if i.source == source
        ]
        write_text(review_dir / f"{source}.jsonl", "".join(lines))
    write_text(receipts_dir / f"{importer.name}.json", dumps_pretty(record_json(receipt)))
    return receipt, review


def read_review_queue(review_dir: Path, source: str) -> list[ReviewItem]:
    """Read review/queue/{source}.jsonl back (for tests and tooling)."""
    path = review_dir / f"{source}.jsonl"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return [ReviewItem.model_validate_json(line) for line in text.splitlines() if line]
