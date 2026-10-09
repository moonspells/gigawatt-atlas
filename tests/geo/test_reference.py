"""atlas.geo.reference: the Census files an import downloads on first use (owner decision of
2026-10-09: the place polygons and the county-subdivision Gazetteer are not committed).

Every request here goes to an httpx.MockTransport; nothing touches the network.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from atlas.geo import reference
from atlas.geo.places import PLACE_POLYGONS_BYTES, PLACE_POLYGONS_SHA256
from atlas.geo.reference import (
    COUNTY_SUBDIVISIONS,
    PLACE_POLYGONS,
    ReferenceFile,
    cache_path,
    ensure_reference,
    verify_reference,
)
from atlas.geocode import COUSUBS_BYTES, COUSUBS_SHA256
from atlas.net import FetchError, OfflineTransport, RobotsDisallowed, fetch, make_client

DATA = b"PK\x05\x06" + b"\x00" * 18  # an empty zip archive
REF = ReferenceFile(
    name="sample.zip",
    url="https://www2.census.gov/geo/sample.zip",
    sha256=hashlib.sha256(DATA).hexdigest(),
    size=len(DATA),
    title="a sample file",
)
Handler = Callable[[httpx.Request], httpx.Response]


def serving(body: bytes, requests: list[str], content_type: str = "application/zip") -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        return httpx.Response(200, content=body, headers={"content-type": content_type})

    return handler


def client(handler: Handler) -> httpx.Client:
    return make_client(transport=httpx.MockTransport(handler), resolver=lambda h: ["93.184.216.34"])


def no_sleep(_: float) -> None:
    return None


def leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.rglob("*") if p.is_file()) if directory.exists() else []


def test_the_pins_are_the_census_files() -> None:
    assert (PLACE_POLYGONS.size, PLACE_POLYGONS.sha256) == (23_057_021, PLACE_POLYGONS_SHA256)
    assert PLACE_POLYGONS.size == PLACE_POLYGONS_BYTES
    assert PLACE_POLYGONS.sha256 == (
        "ce0e4019ecd4123d03d53aaa936eed0459b82e3e14b89a3dcd4d5e8b3308627d"
    )
    assert (COUNTY_SUBDIVISIONS.size, COUNTY_SUBDIVISIONS.sha256) == (1_478_116, COUSUBS_SHA256)
    assert COUNTY_SUBDIVISIONS.size == COUSUBS_BYTES
    for ref in reference.DOWNLOADED:
        assert ref.url.startswith("https://www2.census.gov/geo/")
        assert ref.url.endswith("/" + ref.name)
    assert cache_path(PLACE_POLYGONS, Path(".cache/atlas")) == Path(
        ".cache/atlas/reference/census/cb_2025_us_place_500k.zip"
    )


def test_the_files_are_documented(repo_root: Path) -> None:
    readme = (repo_root / "reference" / "README.md").read_text(encoding="utf-8")
    for ref in reference.DOWNLOADED:
        assert ref.url in readme and ref.sha256 in readme and f"{ref.size:,}" in readme
    assert ".cache/atlas/reference/census/" in readme


def test_downloaded_on_first_use_and_reused(tmp_path: Path) -> None:
    requests: list[str] = []
    with client(serving(DATA, requests)) as http:
        path = ensure_reference(REF, cache_dir=tmp_path, http=http, sleep=no_sleep)
        assert path == tmp_path / "reference" / "census" / "sample.zip"
        assert path.read_bytes() == DATA
        assert requests == ["/geo/sample.zip"]  # robots.txt is not consulted (_ROBOTS)
        assert leftovers(tmp_path) == ["sample.zip"]  # written atomically, no temporary file
        assert ensure_reference(REF, cache_dir=tmp_path, http=http, sleep=no_sleep) == path
    assert len(requests) == 1  # the copy is reused


# www2.census.gov/robots.txt as read on 2026-10-09 with the project's user agent (its first two
# groups; the rest only name other crawlers). A U.S. Government work, in the public domain.
CENSUS_ROBOTS = (
    "User-agent: *\n\nUser-agent: RavenCrawler\nDisallow: /\n\nUser-agent: MegaIndex\n"
    "Disallow: /\n\nUser-agent: Googlebot\nCrawl-delay: 30\n"
)


def census_host(body: bytes, requests: list[str]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=CENSUS_ROBOTS, headers={"content-type": "text/plain"})
        return httpx.Response(200, content=body, headers={"content-type": "application/zip"})

    return handler


def test_the_census_robots_txt_does_not_stop_the_download(tmp_path: Path) -> None:
    # The seed of 2026-10-09 stopped here: RFC 9309 (and Python's robotparser) join "User-agent: *"
    # with the RavenCrawler group after the blank line, so the crawler policy refuses every file
    # on the host. The pinned files are fetched without robots.txt, on a fresh cache.
    requests: list[str] = []
    with client(census_host(DATA, requests)) as http:
        with pytest.raises(RobotsDisallowed, match=r"robots\.txt disallows"):
            fetch(http, REF.url, sleep=no_sleep)  # what the crawler policy says
        requests.clear()
        path = ensure_reference(REF, cache_dir=tmp_path / "fresh", http=http, sleep=no_sleep)
    assert path.read_bytes() == DATA
    assert requests == ["/geo/sample.zip"]
    for ref in reference.DOWNLOADED:  # only the pinned Census files are fetched this way
        assert ref.url.startswith("https://www2.census.gov/") and len(ref.sha256) == 64


def test_a_download_that_is_not_the_pinned_file_leaves_no_file(tmp_path: Path) -> None:
    requests: list[str] = []
    other = DATA[:-1] + b"\x01"
    with client(serving(other, requests)) as http, pytest.raises(FetchError, match="nothing was"):
        ensure_reference(REF, cache_dir=tmp_path, http=http, sleep=no_sleep)
    assert leftovers(tmp_path) == []
    # A larger body is cut off at the pinned size, and a page is not a zip: no file either way.
    for body, content_type in ((DATA + b"x" * 100, "application/zip"), (DATA, "text/html")):
        with client(serving(body, [], content_type)) as http, pytest.raises(FetchError):
            ensure_reference(REF, cache_dir=tmp_path, http=http, sleep=no_sleep)
        assert leftovers(tmp_path) == []


def test_a_failed_download_fails_the_run(tmp_path: Path) -> None:
    def gone(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    with client(gone) as http, pytest.raises(FetchError, match="HTTP 404"):
        ensure_reference(REF, cache_dir=tmp_path, http=http, sleep=no_sleep)
    assert leftovers(tmp_path) == []


def test_offline_without_a_copy_names_the_file_and_how_to_get_it(tmp_path: Path) -> None:
    requests: list[str] = []
    with client(serving(DATA, requests)) as http, pytest.raises(FetchError) as e:
        ensure_reference(REF, cache_dir=tmp_path, http=http, offline=True)
    message = str(e.value)
    assert "sample.zip" in message and REF.url in message and REF.sha256 in message
    assert str(tmp_path / "reference" / "census") in message and "without --offline" in message
    assert requests == []
    # Through the offline transport too (a context that was not told it is offline).
    offline = make_client(transport=OfflineTransport(), resolver=lambda h: ["93.184.216.34"])
    with offline as http, pytest.raises(FetchError, match="without --offline"):
        ensure_reference(REF, cache_dir=tmp_path, http=http, sleep=no_sleep)
    assert leftovers(tmp_path) == []


def test_a_cached_copy_is_checked_on_every_use(tmp_path: Path) -> None:
    path = cache_path(REF, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(DATA + b"changed")
    requests: list[str] = []
    with client(serving(DATA, requests)) as http, pytest.raises(FetchError, match="not the pinned"):
        ensure_reference(REF, cache_dir=tmp_path, http=http, sleep=no_sleep)
    assert requests == []  # never replaced silently
    with pytest.raises(FetchError, match="not found"):
        verify_reference(REF, tmp_path / "missing.zip")
    path.write_bytes(DATA)
    assert verify_reference(REF, path) == path


def test_the_context_downloads_the_county_subdivisions_on_first_use(
    make_test_context: Callable[..., object],
    tmp_path: Path,
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlas.sources.base import ImportContext

    sample = (
        repo_root / "tests/fixtures/geocode/gazetteer/2025_Gaz_cousubs_sample.zip"
    ).read_bytes()
    monkeypatch.setattr(
        reference,
        "COUNTY_SUBDIVISIONS",
        ReferenceFile(
            name=COUNTY_SUBDIVISIONS.name,
            url=COUNTY_SUBDIVISIONS.url,
            sha256=hashlib.sha256(sample).hexdigest(),
            size=len(sample),
            title=COUNTY_SUBDIVISIONS.title,
        ),
    )
    requests: list[str] = []
    ctx = make_test_context(gazetteer=None, handler=serving(sample, requests))
    assert isinstance(ctx, ImportContext)
    monkeypatch.setattr("time.sleep", no_sleep)  # the per-host delay between the two requests
    gazetteer = ctx.gazetteer()
    assert gazetteer.cousub_count == 15 and gazetteer.has_locality("CT", "Bloomfield")
    assert ctx.gazetteer() is gazetteer
    assert requests == [
        "/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_Gaz_cousubs_national.zip",
    ]
    assert (
        ctx.cache_dir / "reference" / "census" / COUNTY_SUBDIVISIONS.name
    ).read_bytes() == sample

    offline = make_test_context(gazetteer=None, places=None, offline=True, cache_dir=tmp_path / "c")
    assert isinstance(offline, ImportContext)
    with pytest.raises(FetchError, match=r"2025_Gaz_cousubs_national\.zip"):
        offline.gazetteer()
    with pytest.raises(FetchError, match=r"cb_2025_us_place_500k\.zip"):
        offline.places()
