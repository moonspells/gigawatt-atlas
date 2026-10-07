"""State and territory codes. The Atlas covers the 50 states and DC (07 §2.2)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class State:
    abbr: str
    fips: str
    name: str
    in_scope: bool


_ROWS = (
    ("AL", "01", "Alabama"),
    ("AK", "02", "Alaska"),
    ("AZ", "04", "Arizona"),
    ("AR", "05", "Arkansas"),
    ("CA", "06", "California"),
    ("CO", "08", "Colorado"),
    ("CT", "09", "Connecticut"),
    ("DE", "10", "Delaware"),
    ("DC", "11", "District of Columbia"),
    ("FL", "12", "Florida"),
    ("GA", "13", "Georgia"),
    ("HI", "15", "Hawaii"),
    ("ID", "16", "Idaho"),
    ("IL", "17", "Illinois"),
    ("IN", "18", "Indiana"),
    ("IA", "19", "Iowa"),
    ("KS", "20", "Kansas"),
    ("KY", "21", "Kentucky"),
    ("LA", "22", "Louisiana"),
    ("ME", "23", "Maine"),
    ("MD", "24", "Maryland"),
    ("MA", "25", "Massachusetts"),
    ("MI", "26", "Michigan"),
    ("MN", "27", "Minnesota"),
    ("MS", "28", "Mississippi"),
    ("MO", "29", "Missouri"),
    ("MT", "30", "Montana"),
    ("NE", "31", "Nebraska"),
    ("NV", "32", "Nevada"),
    ("NH", "33", "New Hampshire"),
    ("NJ", "34", "New Jersey"),
    ("NM", "35", "New Mexico"),
    ("NY", "36", "New York"),
    ("NC", "37", "North Carolina"),
    ("ND", "38", "North Dakota"),
    ("OH", "39", "Ohio"),
    ("OK", "40", "Oklahoma"),
    ("OR", "41", "Oregon"),
    ("PA", "42", "Pennsylvania"),
    ("RI", "44", "Rhode Island"),
    ("SC", "45", "South Carolina"),
    ("SD", "46", "South Dakota"),
    ("TN", "47", "Tennessee"),
    ("TX", "48", "Texas"),
    ("UT", "49", "Utah"),
    ("VT", "50", "Vermont"),
    ("VA", "51", "Virginia"),
    ("WA", "53", "Washington"),
    ("WV", "54", "West Virginia"),
    ("WI", "55", "Wisconsin"),
    ("WY", "56", "Wyoming"),
)
_TERRITORIES = (
    ("AS", "60", "American Samoa"),
    ("GU", "66", "Guam"),
    ("MP", "69", "Commonwealth of the Northern Mariana Islands"),
    ("PR", "72", "Puerto Rico"),
    ("VI", "78", "United States Virgin Islands"),
)

STATES: tuple[State, ...] = tuple(
    [State(a, f, n, True) for a, f, n in _ROWS]
    + [State(a, f, n, False) for a, f, n in _TERRITORIES]
)
IN_SCOPE: frozenset[str] = frozenset(s.abbr for s in STATES if s.in_scope)


class StateIndex(Mapping[str, State]):
    """A read-only mapping that can also be called: index["VA"] raises KeyError, index("VA") -> None."""

    def __init__(self, items: dict[str, State]) -> None:
        self._items = items

    def __getitem__(self, key: str) -> State:
        return self._items[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __call__(self, key: str) -> State | None:
        return self._items.get(key)


state_by_abbr = StateIndex({s.abbr: s for s in STATES})
state_by_fips = StateIndex({s.fips: s for s in STATES})
