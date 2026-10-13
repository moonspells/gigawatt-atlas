"""The importer framework (07 §4.2, §4.3).

An importer reads its upstream files and returns an ImportResult: candidate records (with
PLACEHOLDER_ID), review items and metrics. apply_import matches candidates to stored records by
external id, keeps what reviewers own, validates every record it would write, and writes the
records, the review queue files and the import receipt. classify_source gives a cited link its
source_type (07 §3.4) from its host, its path and its link text.
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
from typing import TYPE_CHECKING, Protocol, TypeGuard
from urllib.parse import unquote, urlsplit

from pydantic import AwareDatetime, Field, HttpUrl, JsonValue

from atlas import __version__
from atlas.jsonio import dumps_compact, dumps_pretty, record_json, write_text
from atlas.schema.record import PLACEHOLDER_ID, AtlasModel, Correction, FacilityRecord, SourceType
from atlas.validate import validate_record

if TYPE_CHECKING:
    import httpx

    from atlas.geo.counties import CountyIndex
    from atlas.geo.places import PlaceIndex
    from atlas.geo.reference import ReferenceFile
    from atlas.geocode import Gazetteer
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
    "merged",
    "removed_upstream",
    "review",
)
DEFAULT_CACHE_DIR = Path(".cache/atlas")
_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


# ---------------------------------------------------------------------------- source types
# Who published a link says less than what it links to: an SEC filing or a press release on an
# investor-relations site, a council resolution on a city's document server, a permit stored on
# Google Drive. classify_source reads, in order: the host, the path, and (on hosts that store other
# people's documents or a company's investor pages) the link text.

# Platforms that host local governments' agendas, codes, minutes and permit files.
GOVERNMENT_HOST_MARKERS = (
    "legistar",
    "granicus",
    "civicplus",
    "civicweb",
    "municode",
    "ecode360",
    "boarddocs",
    "iqm2",
    "novusagenda",
    "primegov",
    "escribemeetings",
    "govonlinesaas",  # permit portals (Linn County Public Health's EasyAir)
    "councildocuments",  # Fort Wayne City Council's document server
)
# Independent system operators and regional transmission organizations (07 §4.1).
ISO_RTO_HOSTS = (
    "misoenergy.org",
    "pjm.com",
    "ercot.com",
    "caiso.com",
    "iso-ne.com",
    "nyiso.com",
    "spp.org",
)
# Wires that publish companies' releases as written.
NEWSWIRE_HOSTS = (
    "prnewswire.com",
    "globenewswire.com",
    "businesswire.com",
    "accessnewswire.com",
    "newsfilecorp.com",
)
# Hosts that store documents uploaded by anyone: the link text says what the document is.
DOCUMENT_HOSTS = (
    "drive.google.com",
    "docs.google.com",
    "scribd.com",
    "dropbox.com",
    "dl.dropboxusercontent.com",
    "static1.squarespace.com",
)
# An investor-relations site: ir.example.com, investors.example.com, investor.example.com.
_IR_HOST_RE = re.compile(r"^(?:ir|investors?)\.")
# EDGAR accession numbers (0001641172-25-013199) and investor sites' filing pages.
_SEC_PATH_RE = re.compile(r"\b\d{10}-\d{2}-\d{6}\b|/sec-filings?/|/edgar/", re.I)
_RELEASE_PATH_RE = re.compile(r"/(?:press-releases?|news-releases?|news-details|press)/", re.I)
_SEC_TEXT_RE = re.compile(r"\bSEC\b|\b(?i:form\s*)?(?i:8-?K|10-?K|10-?Q|S-1|20-F|6-K|DEF\s?14A)\b")
_RELEASE_TEXT_RE = re.compile(r"\bpress\s+release\b|\bnews\s+release\b", re.I)
_GOVERNMENT_TEXT_RE = re.compile(
    r"\bpermit(?:s|ting)?\b|\bordinance\b|\bresolution\b|\b(?:re)?zoning\b|\bcouncil\b"
    r"|\bcommission\b|\bboard of (?:supervisors|commissioners)\b|\btax abatement\b",
    re.I,
)
_ISO_TEXT_RE = re.compile(r"\b(?:MISO|PJM|ERCOT|CAISO|NYISO|ISO-NE|SPP)\b")


def _host_is(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def classify_source(url: str, title: str | None = None) -> SourceType:
    """The source_type of a cited link (07 §3.4), from the host, the path and the link text.

    - sec.gov, an EDGAR accession number in the path, or an investor site's sec-filings page:
      sec_filing ("SEC 8k Filing" at ir.applieddigital.com/sec-filings/...).
    - .gov, .us and .mil hosts, and the platforms in GOVERNMENT_HOST_MARKERS: government_record.
    - ISO/RTO hosts: utility_or_iso_filing (cdn.misoenergy.org's new-load list).
    - Newswires, and an investor site's press-release pages: company_release.
    - On an investor site or a document store (Google Drive, Scribd), the link text decides: SEC
      form names give sec_filing, "press release" company_release, an ISO name
      utility_or_iso_filing, and permits, ordinances, resolutions, zoning, councils and
      commissions government_record ("Air Construction Permit" on drive.google.com).
    - Anything else is news. A news site's headline is not read: "County approves permit" there
      is still news.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = unquote(parts.path or "")
    text = title or ""
    if _host_is(host, ("sec.gov",)) or _SEC_PATH_RE.search(path):
        return "sec_filing"
    if host.endswith((".gov", ".us", ".mil")) or any(m in host for m in GOVERNMENT_HOST_MARKERS):
        return "government_record"
    if _host_is(host, ISO_RTO_HOSTS):
        return "utility_or_iso_filing"
    investor_site = bool(_IR_HOST_RE.match(host))
    if _host_is(host, NEWSWIRE_HOSTS) or (investor_site and _RELEASE_PATH_RE.search(path)):
        return "company_release"
    if investor_site or _host_is(host, DOCUMENT_HOSTS):
        if _SEC_TEXT_RE.search(text):
            return "sec_filing"
        if _RELEASE_TEXT_RE.search(text) or investor_site:
            return "company_release"
        if _ISO_TEXT_RE.search(text):
            return "utility_or_iso_filing"
        if _GOVERNMENT_TEXT_RE.search(text):
            return "government_record"
    return "news"


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
    """One record an importer proposes. match_values are its external_ids[match_key] values.

    joined: the match values a reviewed join brought into this record (config/overrides, for
    example OSM's `join`), so stored records that each hold some of them are merged into one
    (merge_import) instead of being a conflict. split_from: the match value a reviewed split took
    out of another record (OSM's `alone`), so the record that still lists it is not this one.
    """

    match_values: tuple[str, ...]
    record: FacilityRecord
    joined: tuple[str, ...] = ()
    split_from: str | None = None

    def __post_init__(self) -> None:
        if not self.match_values:
            raise ValueError("a candidate needs at least one match value")
        if not set(self.joined) <= set(self.match_values):
            raise ValueError("a candidate's joined values must be among its match values")
        if self.split_from is not None and self.split_from not in self.match_values:
            raise ValueError("a candidate's split_from must be one of its match values")


@dataclass
class ImportResult:
    source: str
    inputs: list[InputSnapshot]
    candidates: list[Candidate]
    review: list[ReviewItem]
    metrics: dict[str, float | int] = field(default_factory=dict)


class ImportContext:
    """What an importer may use. Tests build one with the make_test_context fixture.

    now is the run's clock. input_retrieved_at (`--input-retrieved-at`) is when the local input
    files were fetched, for a replay of saved inputs (load_input); without it, now.

    The Census place polygons and county-subdivision Gazetteer are not committed: places() and
    gazetteer() find them in cache_dir/reference/census/ or download them there on first use
    (atlas.geo.reference), unless the caller passes them (tests pass the small samples).
    offline (`atlas import --offline`) forbids that download.
    """

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
        places_path: Path | None = None,
        places: PlaceIndex | None = None,
        gazetteer: Gazetteer | None = None,
        offline: bool = False,
        input_retrieved_at: datetime | None = None,
    ) -> None:
        if now.tzinfo is None:
            raise ValueError("ImportContext.now must be timezone-aware")
        if input_retrieved_at is not None and input_retrieved_at.tzinfo is None:
            raise ValueError("ImportContext.input_retrieved_at must be timezone-aware")
        self.now = now
        self.today = today
        self.records = records
        self.http = http
        self.cache_dir = cache_dir
        self.input_path = input_path
        self.input_retrieved_at = input_retrieved_at
        self.user_agent = user_agent
        self.new_id = new_id
        self.offline = offline
        self._counties_path = counties_path
        self._counties = counties
        self._places_path = places_path
        self._places = places
        self._gazetteer = gazetteer

    def counties(self) -> CountyIndex:
        """The county index, loaded once."""
        if self._counties is None:
            from atlas.geo.counties import COUNTIES_ZIP, CountyIndex  # DuckDB on use

            self._counties = CountyIndex.load(self._counties_path or COUNTIES_ZIP)
        return self._counties

    def reference_file(self, ref: ReferenceFile) -> Path:
        """A downloaded-on-first-use Census file (atlas.geo.reference.ensure_reference): the
        checked copy in cache_dir, fetched first when there is none. FetchError when it cannot be
        had (offline, a failed download, a copy that is not the pinned file)."""
        from atlas.geo.reference import ensure_reference

        return ensure_reference(ref, cache_dir=self.cache_dir, http=self.http, offline=self.offline)

    def places(self) -> PlaceIndex:
        """The Census place polygons (atlas.geo.places), loaded once: places.city_at(lat, lon,
        state) is the city of a record that has a point and no stated place, and
        geocode(..., places=ctx.places()) names a Census match's city by it. From places_path
        (`--places`, checked against the pin), else from the cache, downloaded on first use."""
        if self._places is None:
            from atlas.geo import reference
            from atlas.geo.places import PlaceIndex  # DuckDB on use

            if self._places_path is not None:
                path = reference.verify_reference(reference.PLACE_POLYGONS, self._places_path)
            else:
                path = self.reference_file(reference.PLACE_POLYGONS)
            self._places = PlaceIndex.load(path)
        return self._places

    def gazetteer(self) -> Gazetteer:
        """The Census Gazetteer with its county subdivisions (New England towns, townships),
        loaded once, for every import that geocodes: the county-subdivision file comes from the
        cache, downloaded on first use."""
        if self._gazetteer is None:
            from atlas.geo import reference
            from atlas.geocode import Gazetteer

            cousubs = self.reference_file(reference.COUNTY_SUBDIVISIONS)
            self._gazetteer = Gazetteer.load(cousub_zip=cousubs)
        return self._gazetteer

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
    False; a local file's retrieved_at is ctx.input_retrieved_at (`--input-retrieved-at`, when
    the saved files were fetched), else ctx.now. Otherwise fetch() is called and the raw bytes
    are kept at ctx.raw_path(name, sha256, ext).
    """
    local = path if path is not None else (ctx.input_path if use_ctx_input else None)
    if local is not None:
        data = local.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        snapshot = InputSnapshot(
            name=name,
            url=HttpUrl(url),
            retrieved_at=ctx.input_retrieved_at or ctx.now,
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


def _observation(value: object) -> TypeGuard[dict[str, JsonValue]]:
    """True for a corrections[] summary of an `other` event (an observation)."""
    return isinstance(value, dict) and value.get("event") == "other" and "as_of" in value


def _undated(c: Correction) -> dict[str, JsonValue]:
    """A correction without the date of the observation it removed or changed (in `old`, and in
    `new` when the op kept that date). OSM dates its observations with each snapshot and AI
    GridWatch its undated stages with each file, so the same decision on a later input can name
    a later date: it is the same decision, and the history must not grow by one entry a week."""
    doc = record_json(c)
    old, new = doc["old"], doc["new"]
    if _observation(old):
        if _observation(new) and new["as_of"] == old["as_of"]:
            doc["new"] = {k: v for k, v in new.items() if k != "as_of"}
        doc["old"] = {k: v for k, v in old.items() if k != "as_of"}
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

    1. Stored records are indexed by external_ids[importer.match_key]. A value that a merged
       record and a record that is not merged both hold goes to the one that is not merged: the
       survivor of a merge also holds the values of the records merged into it.
    2. A candidate matching more than one record is a conflict, unless a reviewed join brought its
       values together (Candidate.joined) and every record's values lie within the candidate's:
       then the record created first (then the smallest id) survives and takes the candidate, and
       each other one gets merged_into the survivor and a /merged_into correction (07 §6.6,
       counted as merged). A record held below blocks the join (a conflict). A candidate of a
       reviewed split (Candidate.split_from) takes the merged record that holds that value and
       unmerges it; without one, it matches only records whose values all lie within its own, so
       the record that still lists the value is left to drop it on its own update. A withdrawn
       join is reversed the same way: a candidate with no join or split whose values are exactly
       a merged record's takes that record back and unmerges it, when the record it is merged
       into still holds those values (as a join's merge leaves it) and no other candidate of
       this run holds any of them. One record is an update; none is new.
    3. New records get ctx.new_id() and ctx.now timestamps. A match whose review.state is not
       "machine" is held (held_human_reviewed), and so is a merged one (held_merged). Otherwise the
       candidate keeps the stored id, created_at, review, merged_into and every external_ids key
       it does not own; its corrections are the stored ones followed by those of the candidate
       the stored record does not have yet (append-only, so an override's history reaches the
       stored record; one that differs from a stored one only by the date of the observation it
       removed or changed is the same decision, _undated). If nothing but timestamps and sources[*].retrieved_at changed, the record
       is unchanged and not rewritten; else updated_at and last_verified_at become ctx.now. A
       candidate an override holds has scope out_of_scope, so its stored record is unpublished.
    4. Every record to be written must pass validate_record, or it becomes an "invalid" item.
    5. Stored records with match_key values that no candidate holds get "removed_upstream".
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
    holders: dict[str, set[str]] = {}
    for rid, record in stored.items():
        for value in record.external_ids.get(importer.match_key, []):
            holders.setdefault(value, set()).add(rid)
    index = {
        value: {r for r in rids if stored[r].merged_into is None} or rids
        for value, rids in holders.items()
    }

    def values_of(rid: str) -> set[str]:
        return set(stored[rid].external_ids.get(importer.match_key, []))

    holding: dict[str, set[int]] = {}  # match value -> the candidates of this run that hold it
    for i, held in enumerate(result.candidates):
        for v in held.match_values:
            holding.setdefault(v, set()).add(i)

    def withdrawn(i: int, candidate: Candidate) -> str | None:
        """The merged record a withdrawn join leaves (07 §6.6 step 7). A join's merge leaves the
        survivor holding the merged record's values; when a candidate with no join or split of
        its own has exactly those values, and no other candidate of this run holds any of them
        (the survivor's candidate no longer does), the join is gone, and the merge is reversed
        as a split would reverse it. A merge a reviewer made by hand, into a record that does not
        hold the values (another source's record), stays."""
        mine = set(candidate.match_values)
        if candidate.joined or candidate.split_from is not None:
            return None
        if any(holding.get(v, set()) - {i} for v in mine):
            return None
        for rid in sorted(set().union(*(holders.get(v, set()) for v in mine))):
            survivor = stored[rid].merged_into
            if survivor in stored and values_of(rid) == mine and mine <= values_of(survivor):
                return rid
        return None

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

    def merge_others(key: str, survivor: str, others: list[str]) -> None:
        """Rewrite each record of others as merged into survivor (07 §6.6)."""
        for rid in others:
            other = stored[rid]
            note = Correction(
                on=ctx.today,
                pointer="/merged_into",
                old=None,
                new=survivor,
                reason=f"merged into {survivor}: a reviewed join brings its {importer.match_key} "
                "values into that record (07 §6.6)",
            )
            merged = other.model_copy(
                update={
                    "merged_into": survivor,
                    "corrections": [*other.corrections, note],
                    "updated_at": ctx.now,
                }
            )
            merged = FacilityRecord.model_validate(record_json(merged))
            claimed[rid] = key
            issues = validate_record(merged, counties=counties, today=ctx.today)
            if issues:
                counts["invalid"] += 1
                flag(
                    "invalid",
                    f"{len(issues)} validation issue(s); not merged into {survivor}",
                    external_id=key,
                    record_id=rid,
                    data={
                        "issues": [
                            {"rule": i.rule, "pointer": i.pointer, "message": i.message}
                            for i in issues
                        ]
                    },
                )
                continue
            counts["merged"] += 1
            to_write.append(merged)

    for n, candidate in enumerate(result.candidates):
        key = candidate.match_values[0]
        mine = set(candidate.match_values)
        seen_ids.update(*(holders.get(v, set()) for v in candidate.match_values))
        matched = sorted(set().union(*(index.get(v, set()) for v in candidate.match_values)))
        unmerge: str | None = None  # why the merged record is taken back
        if candidate.split_from is not None:
            split = candidate.split_from
            merged_holders = sorted(r for r in holders.get(split, set()) if stored[r].merged_into)
            if merged_holders:
                matched = merged_holders
                unmerge = f"a reviewed split takes {split} out of that record"
            else:
                matched = [r for r in matched if values_of(r) <= mine]
        elif (left := withdrawn(n, candidate)) is not None:
            matched = [left]
            more = len(mine) - 1
            unmerge = f"no reviewed join brings {key}{f' and {more} more' if more else ''} into "
            unmerge += "that record any more"
        others: list[str] = []
        if (
            len(matched) > 1
            and candidate.joined
            and not unmerge
            and all(values_of(r) <= mine for r in matched)
        ):
            blocked = [
                r
                for r in matched
                if stored[r].review.state != "machine" or stored[r].merged_into is not None
            ]
            if blocked:
                counts["conflicts"] += 1
                flag(
                    "conflict",
                    f"a reviewed join brings {len(matched)} records together, but "
                    f"{', '.join(blocked)} is reviewed or merged: merge them in a reviewed PR",
                    external_id=key,
                    record_id=None,
                    data={
                        "record_ids": list(matched),
                        "match_values": list(candidate.match_values),
                    },
                )
                continue
            survivor = min(matched, key=lambda r: (stored[r].created_at, r))
            others = [r for r in matched if r != survivor]
            matched = [survivor]
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
        if earlier or any(r in claimed for r in (*matched, *others)):
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
            if existing.merged_into is not None and not unmerge:
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
            corrections = list(existing.corrections)
            decided = [_undated(c) for c in existing.corrections]
            for c in incoming.corrections:
                if c not in corrections and _undated(c) not in decided:
                    corrections.append(c)
            if unmerge:
                corrections.append(
                    Correction(
                        on=ctx.today,
                        pointer="/merged_into",
                        old=existing.merged_into,
                        new=None,
                        reason=f"unmerged from {existing.merged_into}: {unmerge}",
                    )
                )
            final = incoming.model_copy(
                update={
                    "id": existing.id,
                    "created_at": existing.created_at,
                    "updated_at": existing.updated_at,
                    "last_verified_at": existing.last_verified_at,
                    "review": existing.review,
                    "corrections": corrections,
                    "merged_into": None if unmerge else existing.merged_into,
                    "external_ids": external_ids,
                }
            )
            if _comparable(final) == _comparable(existing):
                counts["unchanged"] += 1
                claimed[existing.id] = key
                for v in candidate.match_values:
                    claimed_values[v] = existing.id
                merge_others(key, existing.id, others)
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
        merge_others(key, final.id, others)

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
