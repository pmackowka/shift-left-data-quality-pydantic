"""Schematy BigQuery z modeli: typy, tryby, zagnieżdżenia i pilnowanie rozjazdu."""

from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, Field

from dq_contracts import bigquery
from dq_contracts.bigquery import TABLES, bigquery_schema, main, write_schemas

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "infra" / "terraform" / "schemas"


def _by_name(columns: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {column["name"]: column for column in columns}


def test_money_is_numeric_with_contract_precision() -> None:
    """Ta sama granica co w kontrakcie: kwota z 3 miejscami po przecinku nie wejdzie i tu."""
    events = _by_name(bigquery_schema(TABLES["events"]))
    assert events["value"] == {
        "name": "value",
        "type": "NUMERIC",
        "precision": "12",
        "scale": "2",
        "mode": "REQUIRED",
    }


def test_fields_with_defaults_are_required_in_the_table() -> None:
    """Domyślna wartość jest opcjonalna dla producenta, ale w zapisanym wierszu zawsze jest."""
    events = _by_name(bigquery_schema(TABLES["events"]))
    assert events["shipping"]["mode"] == "REQUIRED"
    assert events["contract_version"]["mode"] == "REQUIRED"


def test_nested_models_and_lists() -> None:
    events = _by_name(bigquery_schema(TABLES["events"]))
    items = events["items"]
    assert (items["type"], items["mode"]) == ("RECORD", "REPEATED")
    assert _by_name(items["fields"])["quantity"]["type"] == "INT64"
    assert events["traffic_source"]["type"] == "RECORD"
    assert _by_name(events["traffic_source"]["fields"])["campaign"]["mode"] == "NULLABLE"


def test_types_without_bigquery_equivalent_follow_the_mapping() -> None:
    events = _by_name(bigquery_schema(TABLES["events"]))
    assert events["event_id"]["type"] == "STRING"
    assert events["event_timestamp"]["type"] == "TIMESTAMP"
    assert events["currency"]["type"] == "STRING"
    assert events["currency"]["description"] == "One of: PLN, EUR, USD, GBP, CZK"


def test_quarantine_keeps_optional_transaction_id_nullable() -> None:
    quarantine = _by_name(bigquery_schema(TABLES["quarantine"]))
    assert quarantine["transaction_id"]["mode"] == "NULLABLE"
    assert quarantine["rejected_at"]["type"] == "TIMESTAMP"
    assert quarantine["issues"]["mode"] == "REPEATED"


def test_scalar_mapping_and_decimal_without_constraints() -> None:
    class Color(StrEnum):
        RED = "red"

    class Sample(BaseModel):
        flag: bool
        ratio: float
        color: Color | None = None
        amount: Decimal
        bounded: Annotated[Decimal, Field(max_digits=5)]

    columns = _by_name(bigquery_schema(Sample))
    assert columns["flag"]["type"] == "BOOL"
    assert columns["ratio"]["type"] == "FLOAT64"
    assert columns["color"]["mode"] == "NULLABLE"
    assert columns["amount"] == {"name": "amount", "type": "NUMERIC", "mode": "REQUIRED"}
    assert columns["bounded"]["precision"] == "5"
    assert "scale" not in columns["bounded"]


def test_unmapped_type_is_an_error_not_a_silent_string() -> None:
    class Unmapped(BaseModel):
        payload: bytes

    with pytest.raises(TypeError, match="no BigQuery mapping"):
        bigquery_schema(Unmapped)


def test_wide_union_is_rejected() -> None:
    class Wide(BaseModel):
        either: int | str

    with pytest.raises(TypeError, match="unions"):
        bigquery_schema(Wide)


def test_committed_schemas_are_up_to_date(tmp_path: Path) -> None:
    """Strażnik rozjazdu: zmiana modelu bez `make schemas` wywala testy i CI."""
    for generated in write_schemas(tmp_path):
        committed = SCHEMAS_DIR / generated.name
        assert committed.exists(), f"{committed} missing - run `make schemas`"
        assert committed.read_text() == generated.read_text(), (
            f"{committed.name} is stale - run `make schemas` and commit the result"
        )


def test_cli_writes_one_file_per_table(tmp_path: Path) -> None:
    assert main(["--out", str(tmp_path)]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(f"{t}.json" for t in TABLES)
    assert bigquery.TABLES is TABLES
