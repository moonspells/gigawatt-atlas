from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from atlas.jsonio import dumps_pretty, record_json
from atlas.schema.record import FacilityRecord
from atlas.store import RecordStore, StoreError, load_orgs

MakeRecord = Callable[..., FacilityRecord]


def test_write_load_and_paths(tmp_repo: Path, make_record: MakeRecord) -> None:
    store = RecordStore(tmp_repo / "data" / "records")
    assert store.load() == {}
    a, b = make_record(), make_record()
    path = store.write(b)
    store.write(a)
    assert path == store.path_for(b.id) == tmp_repo / "data" / "records" / f"{b.id}.json"
    assert path.read_text(encoding="utf-8") == dumps_pretty(record_json(b))
    assert [p.name for p in store.iter_files()] == sorted([f"{a.id}.json", f"{b.id}.json"])
    assert store.load() == {a.id: a, b.id: b}


def test_missing_root_is_empty(tmp_path: Path) -> None:
    assert RecordStore(tmp_path / "nope").iter_files() == []


def test_load_reports_every_bad_file(tmp_repo: Path, make_record: MakeRecord) -> None:
    root = tmp_repo / "data" / "records"
    store = RecordStore(root)
    good = make_record()
    store.write(good)
    (root / "broken.json").write_text("{", encoding="utf-8")
    (root / "wrong-name.json").write_text(json.dumps(record_json(make_record())), encoding="utf-8")
    (root / "invalid.json").write_text(json.dumps({"id": "x"}), encoding="utf-8")
    with pytest.raises(StoreError) as info:
        store.load()
    problems = info.value.problems
    assert len(problems) == 3
    assert any("broken.json" in p for p in problems)
    assert any("does not match id" in p for p in problems)
    assert any("invalid.json" in p for p in problems)


def test_load_orgs(tmp_repo: Path, fixture_orgs_path: Path) -> None:
    assert load_orgs(tmp_repo / "data" / "orgs.json") == []
    orgs = load_orgs(fixture_orgs_path)
    assert [o.kind for o in orgs] == ["colocation"]


def test_repo_data_is_empty_until_the_seed_pr(repo_root: Path) -> None:
    assert load_orgs(repo_root / "data" / "orgs.json") == []
    assert (repo_root / "data" / "orgs.json").read_text(encoding="utf-8") == "[]\n"


def test_failed_write_keeps_the_stored_record(tmp_repo: Path, make_record: MakeRecord) -> None:
    """SV-4: a record that cannot be written (lone surrogate, NaN) leaves the old file intact."""
    root = tmp_repo / "data" / "records"
    store = RecordStore(root)
    good = make_record()
    path = store.write(good)
    before = path.read_bytes()
    lone = json.loads('"Example \\ud83d Campus"')
    for bad in (
        good.model_copy(update={"canonical_name": lone}),
        good.model_copy(update={"site": good.site.model_copy(update={"acreage": float("nan")})}),
    ):
        with pytest.raises(ValueError):  # UnicodeEncodeError is a ValueError
            store.write(bad)
        assert path.read_bytes() == before
    assert [p.name for p in root.iterdir()] == [path.name]


def test_load_reports_unparsable_files(tmp_repo: Path, make_record: MakeRecord) -> None:
    """SV-13 and SV-3: deep nesting and NaN are problems, not a crash or a NaN record."""
    root = tmp_repo / "data" / "records"
    store = RecordStore(root)
    good = make_record()
    store.write(good)
    (root / "deep.json").write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
    data = record_json(make_record())
    text = json.dumps(data, sort_keys=True, indent=2).replace('"acreage": null', '"acreage": NaN')
    assert "NaN" in text
    (root / f"{data['id']}.json").write_text(text, encoding="utf-8")
    with pytest.raises(StoreError) as info:
        store.load()
    problems = info.value.problems
    assert len(problems) == 2
    assert any("deep.json" in p and "nested too deeply" in p for p in problems)
    assert any("NaN is not valid JSON" in p for p in problems)
