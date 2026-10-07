"""The runtime dependencies import and build their clients without warnings.

pytest runs with filterwarnings = error, so a DeprecationWarning from boto3, botocore or dateutil
would fail every test that touches them. If one appears after an upgrade, this test says so first;
the fix is an ignore line in pyproject.toml with a comment.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
import warnings
from pathlib import Path

from botocore.config import Config

R2_CONFIG = Config(
    request_checksum_calculation="when_required",
    response_checksum_validation="when_required",
    signature_version="s3v4",
)

SCRIPT = """
import boto3, duckdb, httpx, jsonschema, pydantic
from botocore.config import Config
client = boto3.client(
    "s3",
    endpoint_url="https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com",
    region_name="auto",
    aws_access_key_id="test",
    aws_secret_access_key="test",
    config=Config(request_checksum_calculation="when_required",
                  response_checksum_validation="when_required"),
)
print(client.meta.region_name)
"""


def test_imports_and_r2_client_raise_no_warnings_in_a_fresh_interpreter(repo_root: Path) -> None:
    result = subprocess.run(  # noqa: S603  (fixed argv, no shell)
        [sys.executable, "-W", "error", "-c", SCRIPT],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "auto"


def test_r2_client_in_process() -> None:
    import boto3
    from botocore.stub import Stubber

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        client = boto3.client(
            "s3",
            endpoint_url="https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com",
            region_name="auto",
            aws_access_key_id="test",
            aws_secret_access_key="test",
            config=R2_CONFIG,
        )
        with Stubber(client) as stub:
            stub.add_response(
                "head_object",
                {"ContentLength": 2, "Metadata": {"sha256": "ab"}},
                {"Bucket": "atlas-tiles", "Key": "v/20000101-0000/manifest.json"},
            )
            head = client.head_object(Bucket="atlas-tiles", Key="v/20000101-0000/manifest.json")
    assert head["Metadata"] == {"sha256": "ab"}


def test_lock_pins_the_planned_versions(repo_root: Path) -> None:
    lock = tomllib.loads((repo_root / "uv.lock").read_text(encoding="utf-8"))
    versions = {p["name"]: p["version"] for p in lock["package"]}
    assert versions["pydantic"].startswith("2.13.")
    assert versions["httpx"].startswith("0.28.")
    assert versions["duckdb"].startswith("1.5.")
    assert versions["jsonschema"].startswith("4.26.")
    assert versions["boto3"].startswith("1.43.")
    assert lock["options"]["exclude-newer-span"] == "PT24H"
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["tool"]["uv"]["exclude-newer"] == "24 hours"
    assert all("~=" in d for d in pyproject["project"]["dependencies"])
