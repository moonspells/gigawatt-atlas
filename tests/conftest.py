"""Shared fixtures for every test in the repo. This is the only conftest.py.

Tests are offline: real HTTP transports and urllib raise unless a test is marked `network` and
ATLAS_NETWORK_TESTS=1 is set. DuckDB's extension download is not affected (CI installs spatial
before pytest). Tests marked `tippecanoe` are skipped when tippecanoe is not on PATH.
"""

from __future__ import annotations

import os
import shutil
import urllib.request
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from atlas.geo.counties import COUNTIES_ZIP, CountyIndex
from atlas.ids import deterministic_ids
from atlas.net import make_client
from atlas.schema.record import FacilityRecord
from atlas.sources.base import ImportContext

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
FIXED_NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
TEST_ADDRESS = "93.184.216.34"


def _network_enabled() -> bool:
    return os.environ.get("ATLAS_NETWORK_TESTS") == "1"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_network = pytest.mark.skip(reason="network test; set ATLAS_NETWORK_TESTS=1 to run")
    skip_tippecanoe = pytest.mark.skip(reason="tippecanoe is not on PATH")
    has_tippecanoe = shutil.which("tippecanoe") is not None
    for item in items:
        if "network" in item.keywords and not _network_enabled():
            item.add_marker(skip_network)
        if "tippecanoe" in item.keywords and not has_tippecanoe:
            item.add_marker(skip_tippecanoe)


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real network access raises unless the test is marked network and the env var is set."""
    if request.node.get_closest_marker("network") is not None and _network_enabled():
        return

    def refuse(*args: object, **kwargs: object) -> Any:
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def counties() -> Iterator[CountyIndex]:
    index = CountyIndex.load(REPO_ROOT / COUNTIES_ZIP)
    yield index
    index.close()


@pytest.fixture
def fixed_now() -> datetime:
    return FIXED_NOW


@pytest.fixture
def today() -> date:
    return FIXED_NOW.date()


@pytest.fixture
def ids(fixed_now: datetime) -> Callable[[], str]:
    """Deterministic, increasing gwa- ids."""
    return deterministic_ids(1, fixed_now)


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """A scratch repo layout: data/records, data/imports, review/queue and data/orgs.json ([])."""
    for sub in ("data/records", "data/imports", "review/queue"):
        (tmp_path / sub).mkdir(parents=True)
    (tmp_path / "data" / "orgs.json").write_text("[]\n", encoding="utf-8")
    return tmp_path


@pytest.fixture(scope="session")
def fixture_records_dir() -> Path:
    return FIXTURES / "records"


@pytest.fixture(scope="session")
def fixture_orgs_path() -> Path:
    return FIXTURES / "orgs.json"


def base_record_data(record_id: str, now: datetime) -> dict[str, Any]:
    """A minimal valid record as a dict: an announced project in Loudoun County, VA."""
    ts = now.isoformat().replace("+00:00", "Z")
    return {
        "id": record_id,
        "record_type": "project",
        "canonical_name": "Example Test Project (Ashburn, VA)",
        "status": "announced",
        "evidence_level": "reported",
        "status_history": [
            {
                "seq": 1,
                "status": "announced",
                "event": "announced",
                "as_of": {"value": "2026-03", "precision": "month"},
                "source_ids": ["s1"],
            }
        ],
        "location": {
            "lat": 39.0438,
            "lon": -77.4874,
            "precision": "site",
            "city": "Ashburn",
            "county_name": "Loudoun",
            "county_fips": "51107",
            "state_abbr": "VA",
            "geocode_method": "manual",
        },
        "sources": [
            {
                "id": "s1",
                "url": "https://example.org/news/test-project",
                "publisher": "Example News",
                "source_type": "news",
                "license": "copyrighted-cite-only",
                "retrieved_at": ts,
                "supports": ["/canonical_name", "/location", "/capacity", "/parties"],
            }
        ],
        "review": {"state": "machine"},
        "created_at": ts,
        "updated_at": ts,
        "last_verified_at": ts,
    }


@pytest.fixture
def make_record(ids: Callable[[], str], fixed_now: datetime) -> Callable[..., FacilityRecord]:
    """Build a valid FacilityRecord; keyword arguments replace top-level keys of the base record.

    Status and derived dates are recomputed (apply_rollup) unless rollup=False is passed.
    """
    from atlas.schema.rollup import apply_rollup

    def factory(*, rollup: bool = True, **overrides: Any) -> FacilityRecord:
        data = base_record_data(ids(), fixed_now)
        data.update(overrides)
        record = FacilityRecord.model_validate(data)
        return apply_rollup(record) if rollup else record

    return factory


def _not_found(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, request=request)


@pytest.fixture
def make_test_context(
    tmp_path: Path,
    fixed_now: datetime,
    ids: Callable[[], str],
    counties: CountyIndex,
) -> Iterator[Callable[..., ImportContext]]:
    """Build an ImportContext for importer tests.

    Keyword overrides: any ImportContext argument (now, today, records, http, cache_dir,
    input_path, user_agent, new_id, counties), plus handler= (an httpx.MockTransport handler; the
    default answers 404) and resolver= (the default resolves every host to 93.184.216.34).
    """
    clients: list[httpx.Client] = []

    def factory(**overrides: Any) -> ImportContext:
        handler: Callable[[httpx.Request], httpx.Response] = overrides.pop("handler", _not_found)
        resolver: Callable[[str], list[str]] = overrides.pop(
            "resolver", lambda host: [TEST_ADDRESS]
        )
        if "http" not in overrides:
            client = make_client(transport=httpx.MockTransport(handler), resolver=resolver)
            clients.append(client)
            overrides["http"] = client
        records: dict[str, FacilityRecord] = overrides.pop("records", {})
        kwargs: dict[str, Any] = {
            "now": fixed_now,
            "today": fixed_now.date(),
            "records": records,
            "cache_dir": tmp_path / ".cache" / "atlas",
            "input_path": None,
            "user_agent": "moonspells-atlas-test/1.0",
            "new_id": ids,
            "counties": counties,
        }
        kwargs.update(overrides)
        return ImportContext(**kwargs)

    yield factory
    for client in clients:
        client.close()
