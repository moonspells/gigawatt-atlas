"""atlas.r2: the allow-list, Cache-Control, the immutable rule, upload order and credentials.

boto3 is exercised with botocore.stub.Stubber and LocalUploader; nothing reaches the network.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from boto3.s3.transfer import TransferConfig
from botocore.stub import ANY, Stubber

from atlas import r2
from atlas.publish import ReleaseError, upload_release
from atlas.r2 import (
    IMMUTABLE,
    SHORT_CACHE,
    DisallowedKey,
    ImmutableConflict,
    LocalUploader,
    ObjectInfo,
    R2ConfigError,
    R2Settings,
    S3Uploader,
    UploadItem,
    cache_control_for,
    content_type_for,
    make_s3_client,
    plan_item,
    plan_release,
    settings_from_env,
    upload_item,
)

ACCOUNT = "0123456789abcdef0123456789abcdef"
SECRET = "very-secret-value-0123456789"
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "release"
RELEASE = "20000101-0000"


class RecordingUploader:
    """An in-memory Uploader that records every call."""

    def __init__(self, stored: dict[str, str] | None = None) -> None:
        self.stored = dict(stored or {})
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def head(self, key: str) -> ObjectInfo | None:
        self.calls.append(("head", key, {}))
        sha = self.stored.get(key)
        return None if key not in self.stored else ObjectInfo(sha256=sha or None)

    def put(
        self, key: str, path: Path, *, content_type: str, cache_control: str, sha256: str
    ) -> None:
        headers = {"content_type": content_type, "cache_control": cache_control, "sha256": sha256}
        self.calls.append(("put", key, headers))
        self.stored[key] = sha256

    def puts(self) -> list[str]:
        return [key for op, key, _ in self.calls if op == "put"]


def quiet(_: str) -> None:
    return None


def item(tmp_path: Path, key: str, data: bytes = b"{}") -> UploadItem:
    path = tmp_path / key.replace("/", "_")
    path.write_bytes(data)
    return plan_item(key, path)


# --------------------------------------------------------------------------- allow-list


@pytest.mark.parametrize(
    ("key", "content_type"),
    [
        ("v/20261012-1200/manifest.json", "application/json"),
        ("v/20261012-1200/records.jsonl.gz", "application/gzip"),
        ("v/20261012-1200/facilities.geojson", "application/geo+json"),
        ("v/20261012-1200/facilities.csv", "text/csv; charset=utf-8"),
        ("v/20261012-1200/facilities.parquet", "application/octet-stream"),
        ("v/20261012-1200/facilities.pmtiles", "application/octet-stream"),
        ("basemap/conus-z10-20261006.pmtiles", "application/octet-stream"),
        ("v/20261012-1200/README.md", "text/markdown; charset=utf-8"),
        ("v/20261012-1200/LICENSE-ODbL-1.0.txt", "text/plain; charset=utf-8"),
        ("v/20261012-1200/vendor/duckdb-eh.wasm", "application/wasm"),
        ("vendor/duckdb-mvp.wasm", "application/wasm"),
    ],
)
def test_content_types(key: str, content_type: str) -> None:
    assert content_type_for(key) == content_type


@pytest.mark.parametrize(
    "key",
    [
        "v/20261012-1200/index.html",
        "v/20261012-1200/logo.svg",
        "v/20261012-1200/app.js",
        "v/20261012-1200/LICENSE",
        "v/20261012-1200/data.gz",
        "v/20261012-1200/duckdb.wasm",  # .wasm only inside vendor/
        "v/20261012-1200/.json",
        "v/20261012-1200/x.JSONL",
    ],
)
def test_allow_list_rejects_other_files(key: str) -> None:
    with pytest.raises(DisallowedKey):
        content_type_for(key)


@pytest.mark.parametrize(
    "key", ["../v/x.json", "/v/x.json", "v//x.json", "v/x y.json", "v\\x.json", "", "v/../x.json"]
)
def test_malformed_keys(key: str) -> None:
    with pytest.raises(DisallowedKey):
        content_type_for(key)


@pytest.mark.parametrize(
    ("key", "cache_control"),
    [
        ("v/20261012-1200/manifest.json", IMMUTABLE),
        ("rec/gwa-01m47854008j5vt37xkj5ag72d/4a98d26ab89b.json", IMMUTABLE),
        ("basemap/conus-z10-20261006.pmtiles", IMMUTABLE),
        ("overlays/counties/2025/counties.pmtiles", IMMUTABLE),
        ("archive/2026-10/manifest.json", IMMUTABLE),
        ("atlas/latest.json", SHORT_CACHE),
        ("atlas/pending.json", SHORT_CACHE),
        ("runs/latest.json", SHORT_CACHE),
    ],
)
def test_cache_control_per_prefix(key: str, cache_control: str) -> None:
    assert cache_control_for(key) == cache_control
    assert IMMUTABLE == "public, max-age=31536000, immutable"
    assert SHORT_CACHE == "public, max-age=60"


@pytest.mark.parametrize(
    "key", ["atlas/other.json", "runs/20261012.json", "latest.json", "data/x.json", "vx/a.json"]
)
def test_cache_control_rejects_other_keys(key: str) -> None:
    with pytest.raises(DisallowedKey, match="no Cache-Control rule"):
        cache_control_for(key)


# ------------------------------------------------------------------------- S3 uploader


def stubbed() -> tuple[Any, S3Uploader]:
    client = make_s3_client(R2Settings(ACCOUNT, "test-key", "test-secret"))
    return client, S3Uploader(
        client, "atlas-tiles", transfer_config=TransferConfig(use_threads=False)
    )


def test_client_config_for_r2() -> None:
    client = make_s3_client(R2Settings(ACCOUNT, "test-key", "test-secret"))
    config = client.meta.config
    assert client.meta.endpoint_url == f"https://{ACCOUNT}.r2.cloudflarestorage.com"
    assert client.meta.region_name == "auto"
    assert config.signature_version == "s3v4"
    assert config.request_checksum_calculation == "when_required"
    assert config.response_checksum_validation == "when_required"
    assert config.retries["mode"] == "standard"
    assert config.s3["addressing_style"] == "path"


def test_s3_put_sends_type_cache_and_sha_but_no_content_encoding(tmp_path: Path) -> None:
    client, uploader = stubbed()
    up = item(tmp_path, "v/20261012-1200/facilities.pmtiles", b"PMTiles")
    expected = {
        "Bucket": "atlas-tiles",
        "Key": up.key,
        "Body": ANY,
        "ContentType": "application/octet-stream",
        "CacheControl": IMMUTABLE,
        "Metadata": {"sha256": up.sha256},
    }
    with Stubber(client) as stub:
        stub.add_client_error(
            "head_object",
            service_error_code="404",
            http_status_code=404,
            expected_params={"Bucket": "atlas-tiles", "Key": up.key},
        )
        stub.add_response("put_object", {}, expected)  # exact params: any extra key fails
        assert upload_item(uploader, up, log=quiet) is True
        stub.assert_no_pending_responses()


def test_s3_head_reads_the_sha256_metadata(tmp_path: Path) -> None:
    client, uploader = stubbed()
    with Stubber(client) as stub:
        stub.add_response(
            "head_object",
            {
                "ContentLength": 7,
                "ContentType": "application/json",
                "CacheControl": IMMUTABLE,
                "Metadata": {"sha256": "ab" * 32},
            },
            {"Bucket": "atlas-tiles", "Key": "v/x/manifest.json"},
        )
        info = uploader.head("v/x/manifest.json")
    assert info == ObjectInfo(
        sha256="ab" * 32, bytes=7, content_type="application/json", cache_control=IMMUTABLE
    )


def test_s3_head_other_errors_propagate() -> None:
    from botocore.exceptions import ClientError

    client, uploader = stubbed()
    with Stubber(client) as stub:
        stub.add_client_error("head_object", service_error_code="403", http_status_code=403)
        with pytest.raises(ClientError):
            uploader.head("v/x/manifest.json")


# ---------------------------------------------------------------------- immutable rule


def test_immutable_same_sha_is_skipped(tmp_path: Path) -> None:
    up = item(tmp_path, "v/20261012-1200/summary.json")
    uploader = RecordingUploader({up.key: up.sha256})
    report = r2.UploadReport()
    assert upload_item(uploader, up, report=report, log=quiet) is False
    assert uploader.puts() == []
    assert report.skipped == [up.key]


@pytest.mark.parametrize("stored", ["0" * 64, ""])
def test_immutable_different_or_unknown_sha_fails(tmp_path: Path, stored: str) -> None:
    up = item(tmp_path, "v/20261012-1200/summary.json")
    uploader = RecordingUploader({up.key: stored})
    with pytest.raises(ImmutableConflict, match="never overwritten"):
        upload_item(uploader, up, log=quiet)
    assert uploader.puts() == []


def test_short_cache_keys_are_overwritten(tmp_path: Path) -> None:
    up = item(tmp_path, "atlas/latest.json")
    uploader = RecordingUploader({up.key: "0" * 64})
    assert upload_item(uploader, up, log=quiet) is True
    assert uploader.calls == [
        (
            "put",
            up.key,
            {"content_type": "application/json", "cache_control": SHORT_CACHE, "sha256": up.sha256},
        )
    ]
    with pytest.raises(ImmutableConflict):
        upload_item(RecordingUploader({up.key: "0" * 64}), up, no_overwrite=True, log=quiet)


def test_local_uploader_round_trip(tmp_path: Path) -> None:
    up = item(tmp_path, "v/20261012-1200/records.jsonl.gz", b"\x1f\x8b\x08\x00")
    target = LocalUploader(tmp_path / "bucket")
    assert target.head(up.key) is None
    upload_item(target, up, log=quiet)
    info = target.head(up.key)
    assert info == ObjectInfo(up.sha256, 4, "application/gzip", IMMUTABLE)
    meta = json.loads(
        (tmp_path / "bucket" / "v" / "20261012-1200" / "records.jsonl.gz.meta.json").read_text(
            "utf-8"
        )
    )
    assert "content_encoding" not in meta
    assert upload_item(target, up, log=quiet) is False  # same sha: skipped


# -------------------------------------------------------------------------- upload order


def test_upload_order_latest_last(tmp_path: Path) -> None:
    out = tmp_path / "out"
    v = out / "v" / "20261012-1200"
    for name in ("manifest.json", "facilities.csv", "schema/facility.v1.json", "summary.json"):
        (v / name).parent.mkdir(parents=True, exist_ok=True)
        (v / name).write_text("{}", encoding="utf-8")
    for key in ("rec/gwa-01m47854008j5vt37xkj5ag72d/aaaaaaaaaaaa.json", "atlas/latest.json"):
        (out / key).parent.mkdir(parents=True, exist_ok=True)
        (out / key).write_text("{}", encoding="utf-8")
    keys = [i.key for i in plan_release(out, "20261012-1200", latest=True)]
    assert keys == [
        "v/20261012-1200/facilities.csv",
        "v/20261012-1200/schema/facility.v1.json",
        "v/20261012-1200/summary.json",
        "v/20261012-1200/manifest.json",
        "rec/gwa-01m47854008j5vt37xkj5ag72d/aaaaaaaaaaaa.json",
        "atlas/latest.json",
    ]
    assert "atlas/latest.json" not in [
        i.key for i in plan_release(out, "20261012-1200", latest=False)
    ]
    (out / "atlas" / "latest.json").unlink()
    with pytest.raises(r2.R2Error, match="--no-latest"):
        plan_release(out, "20261012-1200", latest=True)


def test_upload_release_of_the_fixture(tmp_path: Path) -> None:
    uploader = RecordingUploader()
    upload_release(FIXTURE, RELEASE, uploader, latest=False, log=quiet)
    puts = uploader.puts()
    manifest = puts.index(f"v/{RELEASE}/manifest.json")
    assert all(k.startswith(f"v/{RELEASE}/") for k in puts[:manifest])
    assert all(k.startswith("rec/") for k in puts[manifest + 1 :])
    assert "atlas/latest.json" not in puts
    for op, key, headers in uploader.calls:
        if op == "put":
            assert headers["content_type"] == content_type_for(key)
            assert headers["cache_control"] == IMMUTABLE
            assert headers["sha256"] == hashlib.sha256((FIXTURE / key).read_bytes()).hexdigest()
    # A second run finds every object and sends nothing.
    again = RecordingUploader(uploader.stored)
    report = upload_release(FIXTURE, RELEASE, again, latest=False, log=quiet)
    assert again.puts() == [] and len(report.skipped) == len(puts)


def test_the_fixture_never_writes_latest() -> None:
    with pytest.raises(ReleaseError, match=r"never writes atlas/latest\.json"):
        upload_release(FIXTURE, RELEASE, RecordingUploader(), latest=True, log=quiet)


def test_dry_run_sends_nothing() -> None:
    lines: list[str] = []
    report = upload_release(FIXTURE, RELEASE, None, latest=False, dry_run=True, log=lines.append)
    assert report.uploaded == [] and lines and all(line.startswith("would put ") for line in lines)


# ---------------------------------------------------------------------------- credentials


def test_missing_variables_are_named_without_values() -> None:
    with pytest.raises(R2ConfigError) as e:
        settings_from_env({"R2_TILES_SECRET_ACCESS_KEY": SECRET})
    message = str(e.value)
    assert "CF_ACCOUNT_ID" in message and "R2_TILES_ACCESS_KEY_ID" in message
    assert "R2_TILES_SECRET_ACCESS_KEY" not in message
    assert SECRET not in message


def test_malformed_values_are_not_printed() -> None:
    env = {
        "CF_ACCOUNT_ID": f"not-an-account-{SECRET}",
        "R2_TILES_ACCESS_KEY_ID": "id",
        "R2_TILES_SECRET_ACCESS_KEY": SECRET,
    }
    with pytest.raises(R2ConfigError, match="CF_ACCOUNT_ID") as e:
        settings_from_env(env)
    assert SECRET not in str(e.value)
    env["CF_ACCOUNT_ID"] = ACCOUNT
    env["R2_TILES_BUCKET"] = "Bad_Bucket"
    with pytest.raises(R2ConfigError, match="R2_TILES_BUCKET"):
        settings_from_env(env)


def test_settings_from_env_and_repr() -> None:
    settings = settings_from_env(
        {
            "CF_ACCOUNT_ID": ACCOUNT,
            "R2_TILES_ACCESS_KEY_ID": "id-123",
            "R2_TILES_SECRET_ACCESS_KEY": SECRET,
        }
    )
    assert settings.bucket == "atlas-tiles"
    assert settings.endpoint_url == f"https://{ACCOUNT}.r2.cloudflarestorage.com"
    assert SECRET not in repr(settings) and "id-123" not in repr(settings)


def test_storage_errors_are_reported_by_the_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from botocore.exceptions import ClientError

    from atlas.cli import main

    class Failing(RecordingUploader):
        def head(self, key: str) -> ObjectInfo | None:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "HeadObject"
            )

    monkeypatch.setattr(r2, "s3_uploader_from_env", lambda env=None: Failing())
    args = ["publish", "upload", str(FIXTURE), "--release", RELEASE, "--no-latest"]
    assert main(args) == 1
    err = capsys.readouterr().err
    assert "R2: An error occurred (AccessDenied)" in err
