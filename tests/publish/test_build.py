"""atlas.publish.build_release over the fixture records (07 §11.1, §6.8 steps 1-2)."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import shutil
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from atlas import publish
from atlas.geo.counties import CountyIndex
from atlas.geo.duck import connect
from atlas.jsonio import dumps_compact, read_json, record_json
from atlas.net import make_client
from atlas.publish import (
    CSV_COLUMNS,
    MAP_KEYS,
    BuildOptions,
    BuildResult,
    Facility,
    ReleaseError,
    build_release,
    check_facilities,
    make_facility,
    public_record,
    verify_release,
)
from atlas.schema.record import FacilityRecord
from atlas.store import RecordStore

RELEASE = "20261012-1200"
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
TODAY = date(2026, 10, 12)
RECORDS = Path(__file__).resolve().parents[1] / "fixtures" / "records"
ORGS = Path(__file__).resolve().parents[1] / "fixtures" / "orgs.json"
MERGED_ID = "gwa-01m47854070vw56jt2y440t7p8"
OUT_OF_SCOPE_ID = "gwa-01m4785408jrs4nmnq9bthqa2h"
STATE_ONLY_ID = "gwa-01m4785405ksmnvwymzaxynj4x"
RLO = "‮"

V_FILES = {
    "manifest.json",
    "facilities.parquet",
    "facilities.geojson",
    "facilities.csv",
    "facilities-map.json",
    "records.jsonl.gz",
    "summary.json",
    "feed.json",
    "schema/facility.v1.json",
    "LICENSE-ODbL-1.0.txt",
    "ATTRIBUTION.md",
    "README.md",
    "CHANGELOG.md",
}


def options(out: Path, **changes: Any) -> BuildOptions:
    base: dict[str, Any] = {
        "records_dir": RECORDS,
        "orgs_path": ORGS,
        "out_dir": out,
        "release": RELEASE,
        "generated_at": NOW,
        "previous": "none",
        "skip_pmtiles": True,
        "git_commit": "0" * 40,
        "pr_number": 12,
        "today": TODAY,
    }
    base.update(changes)
    return BuildOptions(**base)


def quiet(_: str) -> None:
    return None


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory, counties: CountyIndex) -> BuildResult:
    out = tmp_path_factory.mktemp("release") / "out"
    return build_release(options(out), counties=counties, log=quiet)


def vdir(result: BuildResult) -> Path:
    return result.out_dir / "v" / RELEASE


def manifest(result: BuildResult) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((vdir(result) / "manifest.json").read_bytes())
    return data


def load_map(result: BuildResult) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((vdir(result) / "facilities-map.json").read_bytes())
    return data


def jsonl_ids(result: BuildResult) -> list[str]:
    raw = gzip.decompress((vdir(result) / "records.jsonl.gz").read_bytes())
    return [json.loads(line)["id"] for line in raw.decode("utf-8").splitlines()]


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return make_client(transport=httpx.MockTransport(handler), resolver=lambda _: ["93.184.216.34"])


def copy_records(tmp_path: Path) -> Path:
    target = tmp_path / "records"
    shutil.copytree(RECORDS, target)
    return target


# ------------------------------------------------------------------------------ layout


def test_every_release_file_exists(built: BuildResult) -> None:
    files = {p.relative_to(vdir(built)).as_posix() for p in vdir(built).rglob("*") if p.is_file()}
    assert files == V_FILES
    assert (built.out_dir / "atlas" / "latest.json").is_file()
    assert sorted(p.name for p in built.out_dir.iterdir()) == ["atlas", "rec", "v"]
    assert verify_release(built.out_dir, RELEASE) == (RELEASE, [])


def test_manifest_checksums_and_bytes_match(built: BuildResult) -> None:
    files = manifest(built)["files"]
    paths = [str(f["path"]) for f in files]
    assert paths == sorted(paths)
    assert "manifest.json" not in paths
    assert set(paths) == V_FILES - {"manifest.json"}
    for entry in files:
        data = (vdir(built) / str(entry["path"])).read_bytes()
        assert entry["bytes"] == len(data)
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
    on_disk = read_json(vdir(built) / "manifest.json")
    assert on_disk == built.manifest


def test_manifest_fields(built: BuildResult) -> None:
    m = built.manifest
    assert m["release"] == RELEASE
    assert m["generated_at"] == "2026-10-12T12:00:00Z"
    assert m["contract_version"] == 1
    assert m["schema_version"] == "1.0.0"
    assert m["license"] == "ODbL-1.0"
    assert m["fixture"] is False
    assert m["attribution"] == (
        f"Gigawatt Atlas, moonspells.dev/atlas, release {RELEASE}, ODbL 1.0 "
        "(https://opendatacommons.org/licenses/odbl/1-0/); contains information from OpenStreetMap "
        "contributors (ODbL), PNNL IM3 (ODbL), and data adapted from Epoch AI and AI GridWatch "
        "(CC BY 4.0, https://creativecommons.org/licenses/by/4.0/)"
    )
    assert m["base_url"] == f"https://tiles.moonspells.dev/v/{RELEASE}/"
    assert m["rec_base_url"] == "https://tiles.moonspells.dev/rec/"
    assert m["git"] == {"commit": "0" * 40, "pr_number": 12}
    assert m["layers"] == read_json(publish.REPO_ROOT / "overlays" / "out" / "layers.json")
    counts = m["counts"]
    assert isinstance(counts, dict)
    assert counts["records_public"] == 8
    assert counts["facilities"] == 7
    assert counts["merged"] == 1
    by_status = counts["by_status"]
    by_group = counts["by_group"]
    assert isinstance(by_status, dict) and isinstance(by_group, dict)
    assert len(by_status) == 8 and sum(int(str(v)) for v in by_status.values()) == 7
    assert sorted(by_group) == ["cx", "op", "pa", "pl", "uc"]
    assert by_group == {"op": 2, "uc": 0, "pl": 2, "cx": 2, "pa": 1}


def test_latest_pointer(built: BuildResult) -> None:
    pointer = read_json(built.out_dir / "atlas" / "latest.json")
    assert pointer == {
        "release": RELEASE,
        "manifest": f"https://tiles.moonspells.dev/v/{RELEASE}/manifest.json",
    }


# ----------------------------------------------------------------------- map and scope


def test_facilities_map_has_exactly_the_16_keys(built: BuildResult) -> None:
    doc = load_map(built)
    assert doc["type"] == "FeatureCollection"
    assert doc["release"] == RELEASE
    assert len(MAP_KEYS) == 16
    ids = [f["properties"]["id"] for f in doc["features"]]
    assert ids == sorted(ids)
    for feature in doc["features"]:
        assert list(feature["properties"]) == sorted(MAP_KEYS)
        assert set(feature["properties"]) == set(MAP_KEYS)
        assert feature["type"] == "Feature"
    by_id = {f["properties"]["id"]: f for f in doc["features"]}
    ashburn = by_id["gwa-01m47854008j5vt37xkj5ag72d"]["properties"]
    assert ashburn["ot"] == "colocation"
    assert ashburn["o"] == "Example Colocation"
    assert ashburn["a"] == "EXC Ashburn"
    assert (ashburn["m"], ashburn["b"], ashburn["g"], ashburn["t"]) == (36.0, "it", "op", "2026-10")
    assert by_id[STATE_ONLY_ID]["geometry"] is None
    point = by_id["gwa-01m47854008j5vt37xkj5ag72d"]["geometry"]
    assert point == {"type": "Point", "coordinates": [-77.4874, 39.0438]}


def test_merged_and_out_of_scope_records(built: BuildResult) -> None:
    map_ids = {f["properties"]["id"] for f in load_map(built)["features"]}
    assert len(map_ids) == 7
    assert MERGED_ID not in map_ids
    assert OUT_OF_SCOPE_ID not in map_ids
    # records.jsonl.gz carries merged records (for redirects) but never out-of-scope ones.
    ids = jsonl_ids(built)
    assert MERGED_ID in ids
    assert OUT_OF_SCOPE_ID not in ids
    assert len(ids) == 8
    rec_ids = {p.parent.name for p in (built.out_dir / "rec").rglob("*.json")}
    assert rec_ids == map_ids
    csv_text = (vdir(built) / "facilities.csv").read_text(encoding="utf-8")
    geojson = (vdir(built) / "facilities.geojson").read_text(encoding="utf-8")
    for text in (csv_text, geojson):
        assert MERGED_ID not in text
        assert OUT_OF_SCOPE_ID not in text
    con = connect()
    try:
        source = str(vdir(built) / "facilities.parquet")
        rows = con.execute("SELECT id FROM read_parquet(?) ORDER BY id", [source]).fetchall()
    finally:
        con.close()
    assert {r[0] for r in rows} == map_ids


def test_h_is_the_sha256_prefix_of_the_rec_bytes(built: BuildResult) -> None:
    store = RecordStore(RECORDS).load()
    for feature in load_map(built)["features"]:
        props = feature["properties"]
        path = built.out_dir / "rec" / props["id"] / f"{props['h']}.json"
        data = path.read_bytes()
        assert hashlib.sha256(data).hexdigest()[:12] == props["h"]
        assert data == dumps_compact(record_json(public_record(store[props["id"]])))
        assert len(data) <= publish.REC_MAX_BYTES


def test_invisible_characters_are_stripped(tmp_path: Path, counties: CountyIndex) -> None:
    records = copy_records(tmp_path)
    store = RecordStore(records)
    rid = "gwa-01m47854008j5vt37xkj5ag72d"
    original = store.load()[rid]
    planted = original.model_copy(
        update={"canonical_name": f"Example{RLO} Colocation Ashburn Campus (Ashburn, VA)"}
    )
    store.write(planted)
    result = build_release(
        options(tmp_path / "out", records_dir=records), counties=counties, log=quiet
    )
    for path in (tmp_path / "out").rglob("*"):
        if path.is_file() and path.suffix != ".parquet":
            data = path.read_bytes()
            if path.name.endswith(".gz"):
                data = gzip.decompress(data)
            assert RLO.encode("utf-8") not in data, path
    props = {f["properties"]["id"]: f["properties"] for f in load_map(result)["features"]}[rid]
    rec = (tmp_path / "out" / "rec" / rid / f"{props['h']}.json").read_bytes()
    # h is computed after the transform, so it equals the hash of the unplanted record.
    assert hashlib.sha256(rec).hexdigest()[:12] == props["h"]
    assert rec == dumps_compact(record_json(public_record(original)))


# ------------------------------------------------------------------------- release checks


def manifest_doc(facilities: int, release: str = "20261011-0600") -> dict[str, Any]:
    return {"release": release, "counts": {"facilities": facilities}}


@pytest.mark.parametrize(("previous", "ok"), [(7, True), (8, False), (6, False)])
def test_count_check_within_ten_percent(
    tmp_path: Path, counties: CountyIndex, previous: int, ok: bool
) -> None:
    prev = tmp_path / "manifest.json"
    prev.write_text(json.dumps(manifest_doc(previous)), encoding="utf-8")
    opts = options(tmp_path / "out", previous=str(prev))
    if ok:
        result = build_release(opts, counties=counties, log=quiet)
        assert result.previous is not None and result.previous.facilities == previous
    else:
        with pytest.raises(ReleaseError, match="more than ±10%"):
            build_release(opts, counties=counties, log=quiet)
        allowed = build_release(
            options(tmp_path / "out2", previous=str(prev), allow_count_change=True),
            counties=counties,
            log=quiet,
        )
        assert manifest(allowed)["counts"]["facilities"] == 7


@pytest.mark.parametrize(
    ("previous", "now", "ok"),
    [(1000, 1100, True), (1000, 900, True), (1000, 1101, False), (1000, 899, False), (0, 0, True)],
)
def test_count_tolerance_is_ten_percent_of_the_previous(previous: int, now: int, ok: bool) -> None:
    prev = publish.PreviousRelease("20261011-0600", previous, "test")
    assert (publish.check_count(prev, now, allow=False) is None) is ok
    assert publish.check_count(prev, now, allow=True) is None
    assert publish.check_count(None, now, allow=False) is None


def test_previous_url_404_means_first_release(tmp_path: Path, counties: CountyIndex) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404, request=request)

    with mock_client(handler) as client:
        result = build_release(
            options(tmp_path / "out", previous="https://tiles.moonspells.dev/atlas/latest.json"),
            counties=counties,
            http=client,
            log=quiet,
        )
    assert result.previous is None
    assert seen == ["https://tiles.moonspells.dev/atlas/latest.json"]  # no robots.txt request


def test_previous_url_follows_the_pointer(tmp_path: Path, counties: CountyIndex) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/atlas/latest.json":
            body = {
                "release": "20261011-0600",
                "manifest": "https://tiles.moonspells.dev/v/20261011-0600/manifest.json",
            }
            return httpx.Response(200, json=body, request=request)
        if request.url.path == "/v/20261011-0600/manifest.json":
            return httpx.Response(200, json=manifest_doc(20), request=request)
        return httpx.Response(404, request=request)

    with mock_client(handler) as client, pytest.raises(ReleaseError, match="facilities 7 vs 20"):
        build_release(
            options(tmp_path / "out", previous="https://tiles.moonspells.dev/atlas/latest.json"),
            counties=counties,
            http=client,
            log=quiet,
        )


def test_previous_url_other_errors_fail(tmp_path: Path, counties: CountyIndex) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request)

    with mock_client(forbidden) as client, pytest.raises(ReleaseError, match=r"HTTP 403"):
        build_release(
            options(tmp_path / "out", previous="https://tiles.moonspells.dev/atlas/latest.json"),
            counties=counties,
            http=client,
            log=quiet,
        )
    assert not (tmp_path / "out").exists()

    calls: list[str] = []

    def unavailable(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(503, request=request)

    with mock_client(unavailable) as client, pytest.raises(ReleaseError, match="HTTP 503"):
        publish.load_previous(
            "https://tiles.moonspells.dev/atlas/latest.json", http=client, sleep=lambda _: None
        )
    assert len(calls) == 4  # the first try and three retries


def expired_manifest(request: httpx.Request) -> httpx.Response:
    """latest.json still points to a release whose manifest is gone (lifecycle or takedown)."""
    if request.url.path == "/atlas/latest.json":
        body = {
            "release": "20260601-0000",
            "manifest": "https://tiles.moonspells.dev/v/20260601-0000/manifest.json",
        }
        return httpx.Response(200, json=body, request=request)
    return httpx.Response(404, request=request)


def test_previous_manifest_gone_fails_without_the_override(
    tmp_path: Path, counties: CountyIndex
) -> None:
    with (
        mock_client(expired_manifest) as client,
        pytest.raises(ReleaseError, match=r"previous manifest: .* HTTP 404"),
    ):
        build_release(
            options(tmp_path / "out", previous="https://tiles.moonspells.dev/atlas/latest.json"),
            counties=counties,
            http=client,
            log=quiet,
        )


def test_previous_manifest_gone_is_recovered_with_allow_count_change(
    tmp_path: Path, counties: CountyIndex
) -> None:
    lines: list[str] = []
    with mock_client(expired_manifest) as client:
        result = build_release(
            options(
                tmp_path / "out",
                previous="https://tiles.moonspells.dev/atlas/latest.json",
                allow_count_change=True,
            ),
            counties=counties,
            http=client,
            log=lines.append,
        )
    assert result.previous is None
    assert verify_release(tmp_path / "out", RELEASE) == (RELEASE, [])
    (note,) = [n for n in result.notes if "previous release" in n]
    assert note.startswith("previous release not read (previous manifest: ")
    assert note.endswith("count check skipped (--allow-count-change)")
    assert f"note: {note}" in lines


def test_previous_pointer_without_manifest_fails(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/atlas/latest.json":
            body = {"release": "20261011-0600", "manifest": "https://tiles.moonspells.dev/v/x"}
            return httpx.Response(200, json=body, request=request)
        return httpx.Response(404, request=request)

    with mock_client(handler) as client, pytest.raises(ReleaseError, match="previous manifest"):
        publish.load_previous("https://tiles.moonspells.dev/atlas/latest.json", http=client)


def test_previous_release_directory(built: BuildResult) -> None:
    previous = publish.load_previous(str(built.out_dir))
    assert previous is not None
    assert (previous.release, previous.facilities) == (RELEASE, 7)


def test_points_must_be_inside_the_us_bounds(built: BuildResult) -> None:
    record = RecordStore(RECORDS).load()["gwa-01m47854008j5vt37xkj5ag72d"]
    good = make_facility(public_record(record))
    assert check_facilities([good]) == []
    for lonlat in [(-50.0, 39.0), (-77.0, 10.0), (-77.0, 80.0), (-190.0, 39.0)]:
        bad = Facility(good.record, good.rec_bytes, good.h, good.row, lonlat)
        (problem,) = check_facilities([bad])
        assert "outside the US bounds" in problem


def test_size_budgets(
    tmp_path: Path, counties: CountyIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(publish, "MAP_BUDGET_GZIP_BYTES", 100)
    with pytest.raises(ReleaseError, match=r"facilities-map\.json: \d+ bytes gzipped > 100"):
        build_release(options(tmp_path / "a"), counties=counties, log=quiet)
    monkeypatch.undo()
    monkeypatch.setattr(publish, "REC_MAX_BYTES", 1000)
    with pytest.raises(ReleaseError, match=r"rec/gwa-.*\.json: \d+ bytes > 1000"):
        build_release(options(tmp_path / "b"), counties=counties, log=quiet)


def test_validation_failure_blocks_the_build(tmp_path: Path, counties: CountyIndex) -> None:
    records = copy_records(tmp_path)
    store = RecordStore(records)
    record = store.load()["gwa-01m47854008j5vt37xkj5ag72d"]
    store.write(record.model_copy(update={"status": "announced"}))  # no longer the rollup
    with pytest.raises(ReleaseError) as e:
        build_release(options(tmp_path / "out", records_dir=records), counties=counties, log=quiet)
    assert any(p.startswith("validate:") and "[rollup]" in p for p in e.value.problems)
    assert not (tmp_path / "out").exists()


def test_no_facility_fails_outside_the_fixture(tmp_path: Path, counties: CountyIndex) -> None:
    empty = tmp_path / "records"
    empty.mkdir()
    with pytest.raises(ReleaseError, match="no in-scope facility"):
        build_release(options(tmp_path / "out", records_dir=empty), counties=counties, log=quiet)


def test_out_dir_with_other_files_is_refused(tmp_path: Path, counties: CountyIndex) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "notes.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(ReleaseError, match="not a release"):
        build_release(options(out), counties=counties, log=quiet)
    assert (out / "notes.txt").read_text(encoding="utf-8") == "keep me"


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_missing_tippecanoe_fails_before_touching_out(
    tmp_path: Path, counties: CountyIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    build_release(options(out), counties=counties, log=quiet)
    before = snapshot(out)

    def started(*_: object, **__: object) -> None:
        pytest.fail("the build went past the tool check without tippecanoe")

    # The staging directory alone would also leave out untouched, after a full build that fails
    # only in write_pmtiles; the early check must stop it before validation (R2-2).
    monkeypatch.setattr(publish, "_validate", started)
    monkeypatch.setattr(publish, "write_parquet", started)
    monkeypatch.setattr(publish, "tippecanoe_path", lambda: None)
    with pytest.raises(ReleaseError, match="tippecanoe is not on PATH"):
        build_release(options(out, release="20261012-1300", skip_pmtiles=False), counties=counties)
    missing = tmp_path / "gone.pmtiles"
    with pytest.raises(ReleaseError, match=r"gone\.pmtiles: no such PMTiles archive"):
        build_release(
            options(out, release="20261012-1300", skip_pmtiles=False, reuse_pmtiles=missing),
            counties=counties,
        )
    assert snapshot(out) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]  # no staging left behind


@pytest.mark.parametrize("stage", ["parquet", "post-check"])
def test_a_failed_build_leaves_the_previous_build_in_place(
    tmp_path: Path, counties: CountyIndex, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    out = tmp_path / "out"
    build_release(options(out), counties=counties, log=quiet)
    before = snapshot(out)
    if stage == "parquet":
        monkeypatch.setattr(publish, "write_parquet", lambda path, facilities: (0, 0))
        match = "facilities.parquet: 0 rows for 7 facilities"
    else:
        monkeypatch.setattr(publish, "FILE_MAX_BYTES", 10)
        match = r"bytes > 10"
    with pytest.raises(ReleaseError, match=match):
        build_release(options(out, release="20261012-1300"), counties=counties, log=quiet)
    assert snapshot(out) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]


def test_rebuild_replaces_the_previous_build(tmp_path: Path, counties: CountyIndex) -> None:
    out = tmp_path / "out"
    build_release(options(out), counties=counties, log=quiet)
    stale = out / "rec" / "gwa-00000000000000000000000000" / "000000000000.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}", encoding="utf-8")
    build_release(options(out, release="20261012-1300"), counties=counties, log=quiet)
    assert not stale.exists()
    assert [p.name for p in (out / "v").iterdir()] == ["20261012-1300"]


def test_fixture_flag_never_writes_latest(tmp_path: Path, counties: CountyIndex) -> None:
    result = build_release(
        options(tmp_path / "out", fixture=True, git_commit="fixture", pr_number=None),
        counties=counties,
        log=quiet,
    )
    assert result.manifest["fixture"] is True
    assert not (tmp_path / "out" / "atlas").exists()
    assert verify_release(tmp_path / "out")[1] == []


def test_builds_are_reproducible(tmp_path: Path, counties: CountyIndex) -> None:
    a = build_release(options(tmp_path / "a"), counties=counties, log=quiet)
    b = build_release(options(tmp_path / "b"), counties=counties, log=quiet)
    files_a = sorted(p.relative_to(a.out_dir) for p in a.out_dir.rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(b.out_dir) for p in b.out_dir.rglob("*") if p.is_file())
    assert files_a == files_b
    for rel in files_a:
        assert (a.out_dir / rel).read_bytes() == (b.out_dir / rel).read_bytes(), rel


def test_bad_release_id_and_tiles_base(tmp_path: Path, counties: CountyIndex) -> None:
    with pytest.raises(ReleaseError, match="YYYYMMDD-HHMM"):
        build_release(options(tmp_path / "a", release="2026-10-12"), counties=counties, log=quiet)
    with pytest.raises(ReleaseError, match="tiles base"):
        build_release(
            options(tmp_path / "b", tiles_base="https://tiles.moonspells.dev/x"),
            counties=counties,
            log=quiet,
        )


# ------------------------------------------------------------------------------- formats


def test_geoparquet_metadata_and_row_count(built: BuildResult) -> None:
    path = vdir(built) / "facilities.parquet"
    geo = publish.parquet_geo_metadata(path)
    assert geo["version"] == "1.1.0"
    assert geo["primary_column"] == "geometry"
    column = geo["columns"]["geometry"]
    assert column["encoding"] == "WKB"
    assert column["geometry_types"] == ["Point"]
    assert "crs" not in column
    assert column["covering"] == {
        "bbox": {
            "xmin": ["bbox", "xmin"],
            "ymin": ["bbox", "ymin"],
            "xmax": ["bbox", "xmax"],
            "ymax": ["bbox", "ymax"],
        }
    }
    xmin, ymin, xmax, ymax = column["bbox"]
    assert xmin < xmax and ymin < ymax
    con = connect()
    try:
        (count,) = con.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone() or (
            0,
        )
        cursor = con.execute("SELECT * FROM read_parquet(?) LIMIT 0", [str(path)])
        described = {str(d[0]): str(d[1]) for d in cursor.description or []}
        nulls = con.execute(
            "SELECT count(*) FROM read_parquet(?) WHERE geometry IS NULL", [str(path)]
        ).fetchone()
    finally:
        con.close()
    assert count == manifest(built)["counts"]["facilities"]
    assert described["geometry"].startswith("GEOMETRY")
    assert described["bbox"] == "STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE)"
    for name in ("aliases", "parties", "status_history", "phases", "buildings", "sources"):
        assert described[name] == "VARCHAR"
    assert nulls == (1,)  # the state-precision record has no point


def test_csv_header_rows_and_lf(built: BuildResult) -> None:
    data = (vdir(built) / "facilities.csv").read_bytes()
    assert b"\r" not in data
    assert data.endswith(b"\n")
    rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
    assert tuple(rows[0]) == CSV_COLUMNS
    assert len(rows) == 8
    ids = [r[0] for r in rows[1:]]
    assert ids == sorted(ids)
    url = rows[1][CSV_COLUMNS.index("url")]
    assert url == f"https://moonspells.dev/atlas/facility/{rows[1][0]}/"


def test_records_jsonl_gz_is_reproducible_and_sorted(built: BuildResult) -> None:
    data = (vdir(built) / "records.jsonl.gz").read_bytes()
    assert data[:3] == b"\x1f\x8b\x08"
    assert data[3] & 0x08 == 0  # no FNAME
    assert data[4:8] == b"\x00\x00\x00\x00"  # mtime 0
    ids = jsonl_ids(built)
    assert ids == sorted(ids)
    for line in gzip.decompress(data).decode("utf-8").splitlines():
        FacilityRecord.model_validate_json(line)
    entry = next(f for f in manifest(built)["files"] if f["path"] == "records.jsonl.gz")
    assert entry["content_type"] == "application/gzip"
    assert "content_encoding" not in entry


def test_summary_and_feed(built: BuildResult) -> None:
    summary = read_json(vdir(built) / "summary.json")
    assert isinstance(summary, dict)
    assert summary["facilities"] == 7
    assert summary["merged"] == 1
    assert summary["unmappable"] == 1
    assert summary["without_mw"] == 1
    assert summary["gw_total"] == 2.052
    assert summary["gw_utility_request_basis"] == 1.3
    feed = read_json(vdir(built) / "feed.json")
    assert feed == {
        "release": RELEASE,
        "generated_at": "2026-10-12T12:00:00Z",
        "window_days": 90,
        "items": [],
    }


def test_static_files_are_copies(built: BuildResult) -> None:
    root = publish.REPO_ROOT
    for name in ("LICENSE-ODbL-1.0.txt", "ATTRIBUTION.md", "CHANGELOG.md"):
        assert (vdir(built) / name).read_bytes() == (root / name).read_bytes()
    schema = (vdir(built) / "schema" / "facility.v1.json").read_bytes()
    assert schema == (root / "schema" / "facility.v1.json").read_bytes()
    readme = (vdir(built) / "README.md").read_text(encoding="utf-8")
    assert RELEASE in readme
    assert "share alike" in readme.lower()
    assert manifest(built)["attribution"] in readme
    for name in V_FILES:
        assert f"`{name}`" in readme


def test_attribution_line_is_the_copy_ready_line_of_attribution_md(built: BuildResult) -> None:
    # The manifest and README line is the one ATTRIBUTION.md tells people to copy, with the
    # license URIs and the "adapted" notice CC BY 4.0 §3(a)(1) asks for (RD4, B9).
    text = (publish.REPO_ROOT / "ATTRIBUTION.md").read_text(encoding="utf-8")
    # Only the first blockquote is the copy-ready line; later ones quote source notices verbatim.
    first: list[str] = []
    for line in text.splitlines():
        if line.startswith("> "):
            first.append(line[2:])
        elif first:
            break
    quoted = " ".join(first)
    assert quoted == publish.ATTRIBUTION
    assert "https://creativecommons.org/licenses/by/4.0/" in manifest(built)["attribution"]
    readme = " ".join((vdir(built) / "README.md").read_text(encoding="utf-8").split())
    assert "not the share-alike" not in readme
    assert "ODbL 4.6" in readme


@pytest.fixture
def tippecanoe_build(tmp_path: Path, counties: CountyIndex) -> Iterator[BuildResult]:
    yield build_release(options(tmp_path / "out", skip_pmtiles=False), counties=counties, log=quiet)


@pytest.mark.tippecanoe
def test_pmtiles_from_mappable_features(tippecanoe_build: BuildResult) -> None:
    path = vdir(tippecanoe_build) / "facilities.pmtiles"
    summary = publish.pmtiles_summary(path)
    assert (summary["min_zoom"], summary["max_zoom"]) == (0, 12)
    meta = summary["metadata"]
    assert meta["name"] == "Gigawatt Atlas facilities"
    assert meta["attribution"] == publish.TILES_ATTRIBUTION
    (layer,) = meta["tilestats"]["layers"]
    assert layer["layer"] == "facilities"
    assert layer["count"] == 6  # the state-precision record is not mappable
    files = [f["path"] for f in manifest(tippecanoe_build)["files"]]
    assert "facilities.pmtiles" in files
