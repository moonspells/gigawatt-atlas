"""Uploads to the R2 bucket atlas-tiles (07 §11.2, 10 §11.8).

Every key passes three rules before anything is sent:

- an extension allow-list with an explicit Content-Type per extension (anything else fails the
  job); Content-Encoding is never set, because Cloudflare ignores Range when it must decompress;
- a Cache-Control per key prefix: immutable for v/, rec/, basemap/, overlays/ and archive/,
  max-age=60 for atlas/latest.json, atlas/pending.json and runs/latest.json; any other key fails;
- immutable keys are never overwritten: a HEAD comes first, the same x-amz-meta-sha256 is skipped
  and a different one fails.

Deleting is for takedowns only (`atlas publish takedown`, docs/publishing.md §7): the Bucket
protocol adds list, get and delete to Uploader.

Credentials come only from the environment (the `production` GitHub Environment in CI):
CF_ACCOUNT_ID, R2_TILES_ACCESS_KEY_ID, R2_TILES_SECRET_ACCESS_KEY and R2_TILES_BUCKET. Error
messages name missing variables and never print their values. boto3 is imported on use.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

IMMUTABLE = "public, max-age=31536000, immutable"
SHORT_CACHE = "public, max-age=60"

IMMUTABLE_PREFIXES = ("v/", "rec/", "basemap/", "overlays/", "archive/")
SHORT_CACHE_KEYS = ("atlas/latest.json", "atlas/pending.json", "runs/latest.json")
LATEST_KEY = "atlas/latest.json"

# Longest suffix first: .jsonl.gz before .json and .gz.
CONTENT_TYPES: tuple[tuple[str, str], ...] = (
    (".jsonl.gz", "application/gzip"),
    (".geojson", "application/geo+json"),
    (".parquet", "application/vnd.apache.parquet"),  # required by GeoParquet 1.1; not compressed
    (".pmtiles", "application/octet-stream"),
    (".json", "application/json"),
    (".csv", "text/csv; charset=utf-8"),
    (".md", "text/markdown; charset=utf-8"),
    (".txt", "text/plain; charset=utf-8"),
    (".wasm", "application/wasm"),  # only inside a vendor/ directory
)

ENV_ACCOUNT = "CF_ACCOUNT_ID"
ENV_ACCESS_KEY = "R2_TILES_ACCESS_KEY_ID"
ENV_SECRET_KEY = "R2_TILES_SECRET_ACCESS_KEY"  # noqa: S105  (a variable name, not a secret)
ENV_BUCKET = "R2_TILES_BUCKET"
DEFAULT_BUCKET = "atlas-tiles"

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$")
_ACCOUNT_RE = re.compile(r"^[0-9a-f]{32}$")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


class R2Error(Exception):
    """An upload rule refused a key, or the bucket holds something unexpected."""


class DisallowedKey(R2Error):  # noqa: N818  (reads as the rule it reports)
    """The key's extension or prefix is not on the allow-list."""


class ImmutableConflict(R2Error):  # noqa: N818
    """An immutable key already holds an object with a different (or unknown) SHA-256."""


class R2ConfigError(R2Error):
    """A credential or bucket variable is missing or malformed."""


# ------------------------------------------------------------------------------- key rules


def check_key(key: str) -> None:
    """Refuse keys that are empty, absolute, contain '..', '//', backslashes or odd characters."""
    if not _KEY_RE.match(key) or ".." in key.split("/"):
        raise DisallowedKey(f"{key!r} is not a valid object key")


def content_type_for(key: str) -> str:
    """The Content-Type for key's extension. Any other extension raises DisallowedKey."""
    check_key(key)
    name = key.rsplit("/", 1)[-1].lower()
    for suffix, content_type in CONTENT_TYPES:
        if name.endswith(suffix) and len(name) > len(suffix):
            if suffix == ".wasm" and "vendor" not in key.split("/")[:-1]:
                raise DisallowedKey(f"{key}: .wasm is allowed only inside a vendor/ directory")
            return content_type
    raise DisallowedKey(f"{key}: extension not on the upload allow-list (10 §11.8)")


def cache_control_for(key: str) -> str:
    """Cache-Control by key prefix. Any other key raises DisallowedKey."""
    check_key(key)
    if key in SHORT_CACHE_KEYS:
        return SHORT_CACHE
    if key.startswith(IMMUTABLE_PREFIXES):
        return IMMUTABLE
    raise DisallowedKey(
        f"{key}: no Cache-Control rule for this key (allowed: {', '.join(IMMUTABLE_PREFIXES)} "
        f"and {', '.join(SHORT_CACHE_KEYS)})"
    )


def is_immutable(key: str) -> bool:
    return cache_control_for(key) == IMMUTABLE


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------------------------- uploaders


@dataclass(frozen=True)
class ObjectInfo:
    """What a HEAD returns about a stored object."""

    sha256: str | None
    bytes: int | None = None
    content_type: str | None = None
    cache_control: str | None = None


class Uploader(Protocol):
    """Where objects go: R2 (S3Uploader) or a local directory (LocalUploader, for tests)."""

    def head(self, key: str) -> ObjectInfo | None:
        """The stored object's metadata, or None when the key is free."""
        ...

    def put(
        self, key: str, path: Path, *, content_type: str, cache_control: str, sha256: str
    ) -> None:
        """Store path at key with these headers and x-amz-meta-sha256. Never Content-Encoding."""
        ...


class Bucket(Uploader, Protocol):
    """An Uploader that can also list, read and delete objects (takedowns)."""

    def list_keys(self, prefix: str) -> list[str]:
        """Every key that starts with prefix ("" or ending in "/"), sorted."""
        ...

    def get(self, key: str) -> bytes | None:
        """The object's bytes, or None when the key is free."""
        ...

    def delete(self, key: str) -> None:
        """Remove the object; a free key is not an error."""
        ...


def check_prefix(prefix: str) -> None:
    """A listing prefix is "" or a valid key followed by "/"."""
    if prefix and (not prefix.endswith("/") or not _KEY_RE.match(prefix[:-1])):
        raise DisallowedKey(f"{prefix!r} is not a valid key prefix")
    if ".." in prefix.split("/"):
        raise DisallowedKey(f"{prefix!r} is not a valid key prefix")


def _not_found(error: Any) -> bool:
    response = getattr(error, "response", {}) or {}
    code = str(response.get("Error", {}).get("Code"))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in _NOT_FOUND_CODES or status == 404


class S3Uploader:
    """R2 over the S3 API with boto3 (upload_file, so large files go multipart)."""

    def __init__(self, client: Any, bucket: str, *, transfer_config: Any = None) -> None:
        self.client = client
        self.bucket = bucket
        self.transfer_config = transfer_config

    def head(self, key: str) -> ObjectInfo | None:
        from botocore.exceptions import ClientError

        try:
            response = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if _not_found(e):
                return None
            raise
        metadata = {str(k).lower(): str(v) for k, v in (response.get("Metadata") or {}).items()}
        length = response.get("ContentLength")
        return ObjectInfo(
            sha256=metadata.get("sha256"),
            bytes=int(length) if length is not None else None,
            content_type=response.get("ContentType"),
            cache_control=response.get("CacheControl"),
        )

    def put(
        self, key: str, path: Path, *, content_type: str, cache_control: str, sha256: str
    ) -> None:
        extra = {
            "ContentType": content_type,
            "CacheControl": cache_control,
            "Metadata": {"sha256": sha256},
        }
        kwargs: dict[str, Any] = {"ExtraArgs": extra}
        if self.transfer_config is not None:
            kwargs["Config"] = self.transfer_config
        self.client.upload_file(str(path), self.bucket, key, **kwargs)

    def list_keys(self, prefix: str) -> list[str]:
        check_prefix(prefix)
        paginator = self.client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys += [str(obj["Key"]) for obj in page.get("Contents", [])]
        return sorted(keys)

    def get(self, key: str) -> bytes | None:
        from botocore.exceptions import ClientError

        check_key(key)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if _not_found(e):
                return None
            raise
        return bytes(response["Body"].read())

    def delete(self, key: str) -> None:
        check_key(key)
        self.client.delete_object(Bucket=self.bucket, Key=key)


class LocalUploader:
    """Writes objects under root, each with a {key}.meta.json sidecar (`--local-target`, tests)."""

    META_SUFFIX = ".meta.json"

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        check_key(key)
        return self.root.joinpath(*key.split("/"))

    def _meta(self, key: str) -> Path:
        path = self._path(key)
        return path.with_name(path.name + self.META_SUFFIX)

    def head(self, key: str) -> ObjectInfo | None:
        meta = self._meta(key)
        if not self._path(key).exists():
            return None
        if not meta.exists():
            return ObjectInfo(sha256=None, bytes=self._path(key).stat().st_size)
        data = json.loads(meta.read_text(encoding="utf-8"))
        return ObjectInfo(
            sha256=data.get("sha256"),
            bytes=data.get("bytes"),
            content_type=data.get("content_type"),
            cache_control=data.get("cache_control"),
        )

    def put(
        self, key: str, path: Path, *, content_type: str, cache_control: str, sha256: str
    ) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        meta = {
            "bytes": target.stat().st_size,
            "cache_control": cache_control,
            "content_type": content_type,
            "sha256": sha256,
        }
        self._meta(key).write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n", "utf-8")

    def list_keys(self, prefix: str) -> list[str]:
        check_prefix(prefix)
        if not self.root.is_dir():
            return []
        keys = (
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*")
            if p.is_file() and not p.name.endswith(self.META_SUFFIX)
        )
        return sorted(k for k in keys if k.startswith(prefix))

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
        self._meta(key).unlink(missing_ok=True)


# ----------------------------------------------------------------------------- credentials


@dataclass(frozen=True)
class R2Settings:
    account_id: str = field(repr=False)
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    bucket: str = DEFAULT_BUCKET

    @property
    def endpoint_url(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"


def settings_from_env(env: Mapping[str, str] | None = None) -> R2Settings:
    """Read the R2 variables. Errors name the variables, never their values."""
    source = os.environ if env is None else env
    names = (ENV_ACCOUNT, ENV_ACCESS_KEY, ENV_SECRET_KEY)
    missing = [name for name in names if not source.get(name, "").strip()]
    if missing:
        raise R2ConfigError(
            "missing environment variables: "
            + ", ".join(missing)
            + " (CI: the `production` Environment; see docs/r2-setup.md)"
        )
    account = source[ENV_ACCOUNT].strip()
    if not _ACCOUNT_RE.match(account):
        raise R2ConfigError(f"{ENV_ACCOUNT} is not a 32-character lowercase hex account id")
    bucket = source.get(ENV_BUCKET, "").strip() or DEFAULT_BUCKET
    if not _BUCKET_RE.match(bucket):
        raise R2ConfigError(f"{ENV_BUCKET} is not a valid bucket name")
    return R2Settings(
        account_id=account,
        access_key_id=source[ENV_ACCESS_KEY].strip(),
        secret_access_key=source[ENV_SECRET_KEY].strip(),
        bucket=bucket,
    )


def make_s3_client(settings: R2Settings) -> Any:
    """A boto3 S3 client for R2. R2 rejects the default checksums of boto3 1.36+."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        region_name="auto",
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key,
        config=Config(
            signature_version="s3v4",
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 5, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )


def storage_errors() -> tuple[type[Exception], ...]:
    """The boto3 and botocore exception bases, for callers that report them as job failures."""
    from boto3.exceptions import Boto3Error
    from botocore.exceptions import BotoCoreError, ClientError

    return (Boto3Error, BotoCoreError, ClientError)


def s3_uploader_from_env(env: Mapping[str, str] | None = None) -> S3Uploader:
    from boto3.s3.transfer import TransferConfig

    settings = settings_from_env(env)
    # 64 MiB parts: the 382 MB basemap goes up in 6 parts, well under R2's 10,000-part limit.
    transfer = TransferConfig(
        multipart_threshold=64 * 1024 * 1024, multipart_chunksize=64 * 1024 * 1024
    )
    return S3Uploader(make_s3_client(settings), settings.bucket, transfer_config=transfer)


# --------------------------------------------------------------------------------- uploads


@dataclass(frozen=True)
class UploadItem:
    key: str
    path: Path
    content_type: str
    cache_control: str
    sha256: str

    @property
    def immutable(self) -> bool:
        return self.cache_control == IMMUTABLE


def plan_item(key: str, path: Path, *, sha256: str | None = None) -> UploadItem:
    """Apply the allow-list and the Cache-Control rule to one file."""
    return UploadItem(
        key=key,
        path=path,
        content_type=content_type_for(key),
        cache_control=cache_control_for(key),
        sha256=sha256 or sha256_file(path),
    )


@dataclass
class UploadReport:
    uploaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return f"{len(self.uploaded)} uploaded, {len(self.skipped)} unchanged"


def upload_item(
    uploader: Uploader,
    item: UploadItem,
    *,
    no_overwrite: bool = False,
    report: UploadReport | None = None,
    log: Callable[[str], None] = print,
) -> bool:
    """Upload one item under the overwrite rules. Returns True when it was sent.

    Immutable keys (and any key with no_overwrite) are checked with HEAD first: an object with
    the same x-amz-meta-sha256 is skipped; a different or missing checksum raises
    ImmutableConflict. Short-cache keys are overwritten unless no_overwrite is set.
    """
    report = report if report is not None else UploadReport()
    if item.immutable or no_overwrite:
        existing = uploader.head(item.key)
        if existing is not None:
            if existing.sha256 == item.sha256:
                report.skipped.append(item.key)
                return False
            stored = existing.sha256 if existing.sha256 else "no sha256 metadata"
            raise ImmutableConflict(
                f"{item.key} already exists with a different object ({stored}); immutable keys "
                "are never overwritten, so publish under a new release id or key"
            )
    uploader.put(
        item.key,
        item.path,
        content_type=item.content_type,
        cache_control=item.cache_control,
        sha256=item.sha256,
    )
    report.uploaded.append(item.key)
    log(f"put {item.key} ({item.content_type}; {item.cache_control})")
    return True


def _files_under(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file())


def _key(release_dir: Path, path: Path) -> str:
    return path.relative_to(release_dir).as_posix()


def plan_release(release_dir: Path, release: str, *, latest: bool) -> list[UploadItem]:
    """The upload order for a built release directory (07 §6.8 step 3).

    1. v/{release}/** except the manifest, 2. v/{release}/manifest.json, 3. rec/**,
    4. atlas/latest.json when latest is set. A reader that follows latest.json therefore never
    finds a manifest whose files are missing, and a manifest never lists a file not yet stored.
    """
    version_dir = release_dir / "v" / release
    manifest = version_dir / "manifest.json"
    if not manifest.is_file():
        raise R2Error(f"{manifest} not found")
    items = [plan_item(_key(release_dir, p), p) for p in _files_under(version_dir) if p != manifest]
    items.append(plan_item(_key(release_dir, manifest), manifest))
    items += [plan_item(_key(release_dir, p), p) for p in _files_under(release_dir / "rec")]
    if latest:
        pointer = release_dir / LATEST_KEY
        if not pointer.is_file():
            raise R2Error(f"{pointer} not found; build without --fixture or pass --no-latest")
        items.append(plan_item(LATEST_KEY, pointer))
    return items


def valid_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.match(value))
