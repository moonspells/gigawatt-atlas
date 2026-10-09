"""Census reference files fetched on first use, not committed (owner decision of 2026-10-09).

reference/census/ holds the county polygons and the place and county Gazetteer files. Two more
U.S. Census Bureau files (public domain) are needed by the importers and are not committed, to
keep the repository small:

- the 2025 cartographic boundary file of places (cb_2025_us_place_500k.zip, 23 MB), which gives
  the city a record names (atlas.geo.places);
- the 2025 Gazetteer file of county subdivisions (2025_Gaz_cousubs_national.zip, 1.5 MB), which
  places New England towns and townships (atlas.geocode).

An import that needs one asks ensure_reference() for it. The copy in
`{cache_dir}/reference/census/{name}` (by default `.cache/atlas/reference/census/`) is used when
it is there; otherwise the file is fetched from census.gov through atlas.net.fetch, under the same
crawler policy as every fetch (the address guard, robots.txt, redirects re-checked, a byte cap of
the pinned size, a zip content type). Its size and SHA-256 must equal the values pinned below
before anything is written, and it is written atomically, so a failed, cut-off or altered download
leaves no file. A copy is checked against the pin every time it is used, and one that differs fails
the run. With --offline and no copy, the run fails with a message naming the file and how to get
it. Tests use small samples (tests/fixtures/geocode/) and never fetch these.

To move to a newer release: download it, check it, and update the pin here, the docs that list it
(reference/README.md) and the tests that pin it, in one pull request.

Importing this module does no I/O and loads neither DuckDB nor httpx.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from atlas.geo.counties import sha256_file

if TYPE_CHECKING:
    import httpx

# Where an import keeps the files, under its --cache-dir (atlas.sources.base.DEFAULT_CACHE_DIR).
CACHE_SUBDIR = Path("reference") / "census"
ZIP_TYPES = ("application/zip", "application/x-zip-compressed", "application/octet-stream")
DOWNLOAD_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class ReferenceFile:
    """A file an import downloads on first use, pinned by size and SHA-256."""

    name: str  # the file name, in the cache and on census.gov
    url: str
    sha256: str
    size: int
    title: str  # what it is, for messages


PLACE_POLYGONS = ReferenceFile(
    name="cb_2025_us_place_500k.zip",
    url="https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_place_500k.zip",
    sha256="ce0e4019ecd4123d03d53aaa936eed0459b82e3e14b89a3dcd4d5e8b3308627d",
    size=23_057_021,
    title="the Census 2025 place polygons (cartographic boundary file, 1:500,000)",
)
COUNTY_SUBDIVISIONS = ReferenceFile(
    name="2025_Gaz_cousubs_national.zip",
    url=(
        "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/"
        "2025_Gaz_cousubs_national.zip"
    ),
    sha256="5e498edbf27e2426ec6661415851ea42c6a635f3cc3a2b79eccc2c3c19b2892e",
    size=1_478_116,
    title="the Census 2025 Gazetteer file of county subdivisions",
)
DOWNLOADED = (PLACE_POLYGONS, COUNTY_SUBDIVISIONS)


def cache_path(ref: ReferenceFile, cache_dir: Path) -> Path:
    """Where an import with this --cache-dir keeps ref."""
    return cache_dir / CACHE_SUBDIR / ref.name


def _error(message: str) -> Exception:
    from atlas.net import FetchError  # httpx on use

    return FetchError(message)


def verify_reference(ref: ReferenceFile, path: Path) -> Path:
    """path, after checking that it is ref (its size and SHA-256); FetchError otherwise."""
    if not path.is_file():
        raise _error(f"{path}: not found ({ref.title}, {ref.name})")
    actual = sha256_file(path)
    if actual != ref.sha256 or path.stat().st_size != ref.size:
        raise _error(
            f"{path}: SHA-256 {actual} ({path.stat().st_size:,} bytes) is not the pinned "
            f"{ref.sha256} ({ref.size:,} bytes) of {ref.title}; delete it, or replace it with "
            f"{ref.url}"
        )
    return path


def ensure_reference(
    ref: ReferenceFile,
    *,
    cache_dir: Path,
    http: httpx.Client,
    offline: bool = False,
    sleep: Callable[[float], None] | None = None,
) -> Path:
    """The checked local copy of ref, downloaded into cache_dir on first use (see the module
    docstring). Raises atlas.net.FetchError when the copy differs from the pin, when --offline
    forbids the download, or when the download fails or differs from the pin (nothing is then
    written)."""
    from atlas.jsonio import write_bytes
    from atlas.net import OfflineError, fetch

    path = cache_path(ref, cache_dir)
    if path.exists():
        return verify_reference(ref, path)
    how = (
        f"download {ref.url} ({ref.size:,} bytes, SHA-256 {ref.sha256}) into {path.parent}/, "
        "or run the import once without --offline"
    )
    if offline:
        raise _error(f"{ref.name} ({ref.title}) is needed and not in {path.parent}/: {how}")
    try:
        result = fetch(
            http,
            ref.url,
            allowed_types=ZIP_TYPES,
            max_bytes=ref.size,
            timeout=DOWNLOAD_TIMEOUT_S,
            sleep=sleep if sleep is not None else time.sleep,
        )
    except OfflineError as e:
        raise _error(f"{ref.name} ({ref.title}) is needed and not in {path.parent}/: {how}") from e
    data = result.content
    digest = hashlib.sha256(data).hexdigest()
    if digest != ref.sha256 or len(data) != ref.size:
        raise _error(
            f"{ref.url}: the download is {len(data):,} bytes with SHA-256 {digest}, not the pinned "
            f"{ref.size:,} bytes and {ref.sha256}, so nothing was written. The Census may have "
            "replaced the file: check the new one and update the pin in atlas/geo/reference.py"
        )
    write_bytes(path, data)
    return path
