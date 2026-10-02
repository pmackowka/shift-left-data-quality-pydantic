"""Schematy tabel BigQuery wyprowadzane z modeli kontraktu.

To jest ostatnie ogniwo zasady jednego źródła prawdy: definicja tabeli w hurtowni nie
powstaje ręcznie, tylko z tych samych modeli, które walidują dane. `make schemas` zapisuje
wynik do `infra/terraform/schemas/`, Terraform czyta pliki przez `file()`, a test
`test_committed_schemas_are_up_to_date` pada, gdy ktoś zmieni model i zapomni
przegenerować schemat. Rozjazd model-tabela jest więc wykrywany przy pull requeście,
a nie przy pierwszym nieudanym zapisie na produkcji.

Moduł nie zależy od żadnej biblioteki Google - produkuje zwykły JSON w formacie
`TableFieldSchema` z REST API BigQuery. Pakiet kontraktu zostaje czystą warstwą domenową.

Tłumaczenie typów jest jawną tabelą, a nieznany typ kończy się wyjątkiem. Ciche
„domyślnie STRING" byłoby wygodne, ale oznaczałoby, że nowe pole typu `float` trafi do
hurtowni jako tekst i nikt tego nie zauważy aż do pierwszej agregacji.
"""

import argparse
import json
import sys
import types
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Union, get_args, get_origin
from uuid import UUID

from pydantic import AwareDatetime, BaseModel
from pydantic.fields import FieldInfo

from dq_contracts.events import PurchaseEvent
from dq_contracts.quarantine import RejectedRecord

Column = dict[str, Any]

# Tabela docelowa -> model, który opisuje jej wiersz. Nazwy tabel muszą się zgadzać
# z `infra/terraform/bigquery.tf`.
TABLES: dict[str, type[BaseModel]] = {
    "events": PurchaseEvent,
    "quarantine": RejectedRecord,
}

_SCALARS: dict[type, str] = {
    bool: "BOOL",
    int: "INT64",
    float: "FLOAT64",
    str: "STRING",
    # UUID jako STRING, bo BigQuery nie ma typu UUID. Tekst w formie kanonicznej
    # (małe litery, myślniki) - dokładnie to, co pydantic zapisuje w JSON-ie.
    UUID: "STRING",
    datetime: "TIMESTAMP",
    # AwareDatetime to w czasie działania osobna klasa-znacznik pydantic, a nie podklasa
    # datetime - stąd osobny wpis. TIMESTAMP w BigQuery i tak przechowuje czas w UTC.
    AwareDatetime: "TIMESTAMP",
    date: "DATE",
}


def _decimal_column(field: FieldInfo) -> Column:
    """NUMERIC z precyzją i skalą wziętą z ograniczeń pola, jeśli są.

    `Money` ma `max_digits=12, decimal_places=2`, więc kolumna dostaje NUMERIC(12, 2).
    Hurtownia odrzuci wtedy wartość, której kontrakt by nie przepuścił - ta sama granica
    po obu stronach. Precyzja i skala jako tekst, bo tak (int64 w JSON-ie) zwraca je
    API BigQuery; liczba zamiast tekstu dawałaby w Terraformie wieczny diff.
    """
    column: Column = {"type": "NUMERIC"}
    for meta in field.metadata:
        digits = getattr(meta, "max_digits", None)
        places = getattr(meta, "decimal_places", None)
        if digits is not None:
            column["precision"] = str(digits)
        if places is not None:
            column["scale"] = str(places)
    return column


def _column_type(annotation: Any, field: FieldInfo) -> Column:
    """Typ BigQuery dla adnotacji bez `None` i bez listy (te obsługuje `_column`)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return {"type": "RECORD", "fields": bigquery_schema(annotation)}
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        # Enum jako STRING z listą wartości w opisie kolumny. Lista w opisie to
        # dokumentacja dla analityka, który pisze WHERE currency = '...', bez zaglądania
        # do kodu - generowana, więc nie zestarzeje się względem enuma.
        allowed = ", ".join(str(member.value) for member in annotation)
        return {"type": "STRING", "description": f"One of: {allowed}"}
    if annotation is Decimal:
        return _decimal_column(field)
    if annotation in _SCALARS:
        return {"type": _SCALARS[annotation]}
    msg = f"no BigQuery mapping for {annotation!r} - add it to dq_contracts.bigquery explicitly"
    raise TypeError(msg)


def _column(name: str, field: FieldInfo) -> Column:
    """Pełna definicja kolumny: nazwa, typ i tryb.

    Tryb opisuje ZAPISANY wiersz, nie wejście. Pole z wartością domyślną (`shipping`,
    `contract_version`) jest opcjonalne dla producenta, ale `model_dump` zawsze je
    zapisuje - więc w tabeli jest REQUIRED. NULLABLE dostaje tylko pole, którego typ
    dopuszcza `None`.
    """
    annotation: Any = field.annotation
    mode = "REQUIRED"

    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        members = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(members) != 1:
            msg = f"field {name!r}: unions other than `X | None` have no BigQuery mapping"
            raise TypeError(msg)
        annotation, mode = members[0], "NULLABLE"
    elif origin is list:
        (annotation,) = get_args(annotation)
        mode = "REPEATED"

    return {"name": name, **_column_type(annotation, field), "mode": mode}


def bigquery_schema(model: type[BaseModel]) -> list[Column]:
    """Lista kolumn w kolejności pól modelu - tej samej, w jakiej pydantic zapisuje JSON."""
    return [_column(name, field) for name, field in model.model_fields.items()]


def write_schemas(directory: Path) -> list[Path]:
    """Zapisuje `<tabela>.json` dla każdej tabeli z `TABLES`; zwraca ścieżki."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for table, model in TABLES.items():
        path = directory / f"{table}.json"
        # indent=2 i końcowy znak nowej linii: pliki są commitowane, więc diff przy
        # zmianie modelu ma pokazywać zmienioną kolumnę, a nie jedną długą linię.
        path.write_text(json.dumps(bigquery_schema(model), indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate BigQuery table schemas from models.")
    parser.add_argument("--out", type=Path, default=Path("infra/terraform/schemas"))
    args = parser.parse_args(argv)
    for path in write_schemas(args.out):
        print(f"wrote {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - wejście przez `python -m`
    sys.exit(main())
