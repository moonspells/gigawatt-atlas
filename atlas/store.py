"""The record store: one canonical JSON file per record in data/records/{id}.json (07 §3.1)."""

from __future__ import annotations

from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from atlas.jsonio import dumps_pretty, read_json, record_json, write_text
from atlas.schema.org import Org
from atlas.schema.record import FacilityRecord

RECORDS_DIR = Path("data/records")
ORGS_PATH = Path("data/orgs.json")

_ORGS = TypeAdapter(list[Org])


class StoreError(Exception):
    """One or more record files could not be loaded; problems lists each one."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class RecordStore:
    def __init__(self, root: Path = RECORDS_DIR) -> None:
        self.root = root

    def iter_files(self) -> list[Path]:
        """Record files, sorted by name."""
        if not self.root.exists():
            return []
        return sorted(self.root.glob("*.json"))

    def path_for(self, record_id: str) -> Path:
        return self.root / f"{record_id}.json"

    def load(self) -> dict[str, FacilityRecord]:
        """Every record by id. Raises StoreError listing every file that fails."""
        records: dict[str, FacilityRecord] = {}
        problems: list[str] = []
        for path in self.iter_files():
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
        """Write the canonical file for record and return its path."""
        path = self.path_for(record.id)
        write_text(path, dumps_pretty(record_json(record)))
        return path


def load_orgs(path: Path = ORGS_PATH) -> list[Org]:
    """data/orgs.json: a JSON array of Org."""
    return _ORGS.validate_python(read_json(path))
