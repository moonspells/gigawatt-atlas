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
