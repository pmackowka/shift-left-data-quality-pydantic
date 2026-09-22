"""Generowanie JSON Schema z modelu - co się przenosi, a co zostaje w kodzie.

Po co to komu, skoro kontrakt już jest w Pythonie:

1. **Producent w innym języku.** Aplikacja frontendowa albo usługa w Go nie zaimportuje
   modelu pydantic, ale JSON Schema przeczyta każda biblioteka walidacyjna. To sposób
   na wysłanie kontraktu do zespołu, który nie pracuje w Pythonie.
2. **Dokumentacja, która nie kłamie.** Schemat generuje się z kodu, więc nie da się go
   zapomnieć zaktualizować. Opis kontraktu w Confluence rozjeżdża się z rzeczywistością
   w tygodniu, w którym ktoś doda pole.
3. **Schemat rejestru wiadomości.** Pub/Sub potrafi wymuszać schemat na temacie, a jego
   definicję da się wyprowadzić z tego samego źródła.
4. **Punkt wyjścia dla schematów BigQuery** - dokładnie to robimy w etapie 6.

I rzecz równie ważna: czego JSON Schema NIE wyraża. Reguła „wartość musi się zgadzać
z sumą pozycji" i reguła „znacznik czasu nie może być z przyszłości" nie mają
odpowiednika w schemacie - to walidatory imperatywne. Producent walidujący wyłącznie
schematem przepuści rekord, który nasz pipeline odrzuci. Test niżej to dokumentuje,
bo jest to najczęstsze nieporozumienie wokół kontraktów danych.
"""

from typing import Any

from dq_contracts import PurchaseEvent


def schema() -> dict[str, Any]:
    return PurchaseEvent.model_json_schema()


def test_schema_marks_required_fields() -> None:
    """Pola bez wartości domyślnej trafiają do `required`."""
    required = set(schema()["required"])

    assert {"transaction_id", "currency", "value", "items", "event_timestamp"} <= required
    # Wysyłka i rabat mają wartości domyślne, więc wymagane nie są.
    assert "shipping" not in required
    assert "discount" not in required


def test_schema_forbids_extra_properties() -> None:
    """`extra="forbid"` przenosi się do schematu jako `additionalProperties: false`.

    Dzięki temu producent w innym języku odrzuci pole nadmiarowe tak samo jak my.
    """
    assert schema()["additionalProperties"] is False


def test_schema_carries_enum_values() -> None:
    """Zbiór dozwolonych walut jedzie w schemacie, więc nie trzeba go opisywać osobno."""
    currencies = schema()["$defs"]["Currency"]["enum"]

    assert set(currencies) == {"PLN", "EUR", "USD", "GBP", "CZK"}


def test_schema_carries_numeric_and_length_constraints() -> None:
    """Ograniczenia z `Field` przenoszą się do schematu.

    Kwota opisana jest jako `anyOf`: liczba albo łańcuch znaków pasujący do wzorca -
    bo JSON nie ma typu dziesiętnego i obie reprezentacje są poprawne.
    """
    properties = schema()["properties"]

    assert properties["items"]["minItems"] == 1
    assert properties["items"]["maxItems"] == 200
    numeric_variant = properties["value"]["anyOf"][0]
    assert numeric_variant["exclusiveMinimum"] == 0.0


def test_schema_does_not_express_cross_field_rules() -> None:
    """Walidatorów imperatywnych w schemacie nie ma i nie da się ich tam wyrazić.

    To jest granica przydatności JSON Schema jako kontraktu. Producent, który waliduje
    wyłącznie schematem, wyśle zdarzenie z wartością niezgodną z sumą pozycji
    i z datą z przyszłości - oba przejdą. Dlatego walidacja po stronie konsumenta
    zostaje, nawet gdy producent deklaruje, że sprawdza schemat.
    """
    serialized = str(schema())

    assert "value_mismatch" not in serialized
    assert "future_timestamp" not in serialized


def test_nested_models_are_referenced_not_inlined() -> None:
    """Modele zagnieżdżone trafiają do `$defs` i są wskazywane przez `$ref`.

    Dzięki temu definicja pozycji występuje raz, a nie w każdym miejscu, w którym
    jest używana - i przy zmianie nie ma czego rozjechać.
    """
    document = schema()

    assert set(document["$defs"]) == {"Channel", "Currency", "LineItem", "TrafficSource"}
    assert document["properties"]["items"]["items"]["$ref"] == "#/$defs/LineItem"
