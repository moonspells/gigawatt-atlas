"""The record store: one canonical JSON file per record in data/records/{id}.json (07 §3.1)."""

from __future__ import annotations

from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from atlas.jsonio import dumps_pretty, read_json, record_json, write_text
from atlas.schema.org import Org
from atlas.schema.record import PLACEHOLDER_ID, FacilityRecord

RECORDS_DIR = Path("data/records")
ORGS_PATH = Path("data/orgs.json")
# The only entry of a records directory that is not a record file.
KEEP_FILE = ".gitkeep"

_ORGS = TypeAdapter(list[Org])


def list_records_dir(root: Path) -> tuple[list[Path], list[Path]]:
    """(record files, stray entries) of a records directory, each sorted by name.

    Record files are the *.json files whose names do not start with "."; .gitkeep is ignored.
    Everything else is stray: directories, other suffixes, and dot-files such as .x.json or
    macOS ._{id}.json, which Path.glob("*.json") matches. atlas validate and RecordStore both use
    this listing, so a file one of them skips cannot break the other.
    """
    if not root.is_dir():
        return [], []
    files: list[Path] = []
    stray: list[Path] = []
    for path in sorted(root.iterdir()):
        if path.name == KEEP_FILE:
            continue
        if path.name.startswith(".") or path.suffix != ".json" or not path.is_file():
            stray.append(path)
        else:
            files.append(path)
    return files, stray


class StoreError(Exception):
    """One or more record files could not be loaded; problems lists each one."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class RecordStore:
    def __init__(self, root: Path = RECORDS_DIR) -> None:
        self.root = root

    def iter_files(self) -> list[Path]:
        """Record files, sorted by name (see list_records_dir)."""
        return list_records_dir(self.root)[0]

    def path_for(self, record_id: str) -> Path:
        return self.root / f"{record_id}.json"

    def load(self) -> dict[str, FacilityRecord]:
        """Every record by id. Raises StoreError listing every file that fails and every stray
        entry (see list_records_dir)."""
        records: dict[str, FacilityRecord] = {}
        files, stray = list_records_dir(self.root)
        problems = [f"{path}: only {{id}}.json files belong in {self.root}" for path in stray]
        for path in files:
            try:
                record = FacilityRecord.model_validate(read_json(path))
            except (OSError, ValueError, ValidationError) as e:
                problems.append(f"{path}: {e}")
                continue
            if path.name != f"{record.id}.json":
                problems.append(f"{path}: file name does not match id {record.id}")
                continue
            records[record.id] = record
        if problems:
            raise StoreError(problems)
        return records

    def write(self, record: FacilityRecord) -> Path:
        """Write the canonical file for record (atomically) and return its path."""
        if record.id == PLACEHOLDER_ID:
            raise ValueError(f"{PLACEHOLDER_ID} is the importer placeholder, not a record id")
        path = self.path_for(record.id)
        write_text(path, dumps_pretty(record_json(record)))
        return path


def load_orgs(path: Path = ORGS_PATH) -> list[Org]:
    """data/orgs.json: a JSON array of Org."""
    return _ORGS.validate_python(read_json(path))
