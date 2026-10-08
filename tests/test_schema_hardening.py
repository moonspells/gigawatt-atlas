"""SV-3, SV-4, SV-9 and SV-12: what the models refuse, and that the JSON Schema agrees."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from atlas.jsonio import record_json
from atlas.schema.export import facility_json_schema, render
from atlas.schema.org import Org
from atlas.schema.record import FacilityRecord, FuzzyDate

MakeRecord = Callable[..., FacilityRecord]
FULLWIDTH_2026 = "\uff12\uff10\uff12\uff16"
ARABIC_INDIC_1 = "\u0661"


def changed(make_record: MakeRecord, path: str, value: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(json.dumps(record_json(make_record())))
    node: Any = data
    keys = path.split("/")
    for key in keys[:-1]:
        node = node[int(key)] if key.isdigit() else node[key]
    node[keys[-1]] = value
    return data


@pytest.mark.parametrize(
    "path",
    [
        "site/building_sqft",
        "capacity/it_mw",
        "money/investment_usd",
        "cooling/water_use_mgd",
        "location/lat",
        "site/acreage",
    ],
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nan_and_infinity_are_refused(make_record: MakeRecord, path: str, value: float) -> None:
    with pytest.raises(ValidationError, match="finite"):
        FacilityRecord.model_validate(changed(make_record, path, value))


def test_nan_is_refused_in_nested_models(make_record: MakeRecord) -> None:
    data = changed(make_record, "phases", [{"phase_id": "p1", "name": "P1", "building_sqft": None}])
    data["phases"][0]["building_sqft"] = float("nan")
    with pytest.raises(ValidationError, match="finite"):
        FacilityRecord.model_validate(data)
    data = changed(make_record, "buildings", [{"ref": "osm:way/1", "sqft": float("inf")}])
    with pytest.raises(ValidationError, match="finite"):
        FacilityRecord.model_validate(data)


@pytest.mark.parametrize(
    "path",
    [
        "canonical_name",
        "location/city",
        "sources/0/publisher",
        "status_history/0/note",
    ],
)
def test_lone_surrogates_are_refused(make_record: MakeRecord, path: str) -> None:
    """SV-4: a truncated emoji in upstream JSON ("\\ud83d") cannot reach a record file."""
    lone = json.loads('"Example \\ud83d Campus"')
    assert len(lone) == 16
    with pytest.raises(ValidationError, match=r"lone surrogate U\+D83D|unicode string"):
        FacilityRecord.model_validate(changed(make_record, path, lone))


def test_lone_surrogates_are_refused_in_keys_and_json_values(make_record: MakeRecord) -> None:
    lone = json.loads('"\\udfff"')
    with pytest.raises(ValidationError, match="lone surrogate"):
        FacilityRecord.model_validate(changed(make_record, "external_ids", {lone: ["1"]}))
    meta = {"/x": {"confidence": 0.5, "method": "stated", "conflicts": [{"value": lone}]}}
    with pytest.raises(ValidationError, match="lone surrogate"):
        FacilityRecord.model_validate(changed(make_record, "field_meta", meta))
    # A surrogate pair (an emoji) is ordinary text.
    assert FacilityRecord.model_validate(
        changed(make_record, "location/city", "Ashburn \U0001f600")
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("status_history/0/as_of", {"value": FULLWIDTH_2026, "precision": "year"}),
        ("status_history/0/as_of", {"value": "2026-0" + ARABIC_INDIC_1, "precision": "month"}),
        ("location/county_fips", "5110" + ARABIC_INDIC_1),
        ("sources/0/id", "s" + ARABIC_INDIC_1),
        ("status_history/0/source_ids", ["s" + ARABIC_INDIC_1]),
    ],
)
def test_only_ascii_digits(make_record: MakeRecord, path: str, value: Any) -> None:
    """SV-9: \\d in Python and pydantic-core also matches other scripts' digits."""
    with pytest.raises(ValidationError):
        FacilityRecord.model_validate(changed(make_record, path, value))


def test_fuzzy_date_rejects_fullwidth_digits() -> None:
    with pytest.raises(ValidationError):
        FuzzyDate(value=FULLWIDTH_2026, precision="year")


@pytest.mark.parametrize(
    ("field", "value"),
    [("sec_cik", "000000000" + ARABIC_INDIC_1), ("wikidata_qid", "Q" + ARABIC_INDIC_1)],
)
def test_org_ids_take_ascii_digits_only(field: str, value: str) -> None:
    base = {"id": "gwo-01m4785400yqzp3nxn6f6q6wfh", "name": "Example", "kind": "colocation"}
    with pytest.raises(ValidationError):
        Org.model_validate({**base, field: value})
    Org.model_validate({**base, "sec_cik": "0000000001", "wikidata_qid": "Q42"})


def test_exported_patterns_use_ascii_digit_classes() -> None:
    assert "\\\\d" not in render()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("status_history/0/source_ids", ["s1\n::error::x"]),
        ("status_history/0/source_ids", ["source-1"]),
        ("parties", {"operator": [{"name": "X", "org_id": "../../etc"}]}),
        ("parties", {"operator": [{"name": "X", "org_id": "gwo-01m4785400yqzp3nxn6f6q6wfh\n"}]}),
        ("merged_into", "gwa-01m47854008j5vt37xkj5ag72d\n::error::x"),
        ("merged_into", "not-a-record"),
    ],
)
def test_reference_fields_carry_their_id_patterns(
    make_record: MakeRecord, path: str, value: Any
) -> None:
    """SV-12: references cannot carry control characters or arbitrary text."""
    data = changed(make_record, path, value)
    with pytest.raises(ValidationError):
        FacilityRecord.model_validate(data)
    assert list(Draft202012Validator(facility_json_schema()).iter_errors(data))


def test_the_json_schema_rejects_what_the_model_rejects(make_record: MakeRecord) -> None:
    validator = Draft202012Validator(facility_json_schema())
    for path, value in [
        ("status_history/0/as_of", {"value": FULLWIDTH_2026, "precision": "year"}),
        ("location/county_fips", "5110" + ARABIC_INDIC_1),
        ("sources/0/id", "s" + ARABIC_INDIC_1),
    ]:
        assert list(validator.iter_errors(changed(make_record, path, value))), path
    assert list(validator.iter_errors(record_json(make_record()))) == []
