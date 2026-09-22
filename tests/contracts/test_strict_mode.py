"""Reguła 7: tryb strict kontra koercja typów.

To najmniej oczywista część kontraktu, więc testy są tu równocześnie dokumentacją.

Domyślnie pydantic koercuje typy: `"3"` staje się `3`, `1` staje się `Decimal("1")`.
Wygodne przy formularzach, szkodliwe przy danych finansowych - koercja ukrywa fakt,
że producent wysyła zły typ, a im dłużej to trwa, tym droższa jest korekta.

Drugi, jeszcze mniej oczywisty fakt: `strict` znaczy co innego przy walidacji obiektu
Pythona, a co innego przy walidacji JSON-a. JSON zna tylko łańcuchy, liczby, wartości
logiczne, listy i obiekty - nie ma typu `Decimal`, `datetime` ani `UUID`. Tryb strict
nie ma więc czego egzekwować dla tych typów i przyjmuje ich tekstową reprezentację.
Dla typów, które JSON zna (liczba całkowita), strict działa w obu trybach tak samo.
"""

import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from dq_contracts import PurchaseEvent
from tests.contracts.helpers import as_json


def error_types(exc: ValidationError) -> list[str]:
    return [item["type"] for item in exc.errors()]


# --- typ, który JSON zna: liczba całkowita ----------------------------------


def test_strict_rejects_string_where_int_expected(valid_payload: dict[str, Any]) -> None:
    """Wariant negatywny: ilość przysłana jako łańcuch znaków.

    JSON potrafi wyrazić liczbę całkowitą, więc `"2"` zamiast `2` to realny błąd
    producenta, a nie ograniczenie formatu. Strict go odrzuca.
    """
    valid_payload["items"][0]["quantity"] = "2"

    with pytest.raises(ValidationError) as exc_info:
        PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert "int_type" in error_types(exc_info.value)


def test_lax_mode_coerces_string_to_int(valid_payload: dict[str, Any]) -> None:
    """Wariant pozytywny dla trybu łagodnego - ten sam rekord przechodzi.

    `strict=False` w wywołaniu nadpisuje ustawienie z `model_config`, więc nie trzeba
    utrzymywać drugiego modelu tylko po to, żeby obsłużyć mniej rygorystyczne źródło.
    Jeden kontrakt, dwa tryby czytania.

    Kiedy to ma sens: migracja ze starego producenta, którego nie da się poprawić
    z dnia na dzień. Wtedy dane wchodzą w trybie łagodnym, ale z adnotacją w rekordzie,
    że przeszły koercję - i widać, ile ich jest.
    """
    valid_payload["items"][0]["quantity"] = "2"

    event = PurchaseEvent.model_validate_json(as_json(valid_payload), strict=False)

    assert event.items[0].quantity == 2


# --- typ, którego JSON nie zna: Decimal -------------------------------------


def test_strict_json_accepts_decimal_as_string(valid_payload: dict[str, Any]) -> None:
    """Wariant pozytywny: kwota jako łańcuch znaków przechodzi w trybie strict.

    I tak właśnie powinien wyglądać poprawny producent. Kwota wysłana jako liczba
    zmiennoprzecinkowa JSON-a przechodzi przez reprezentację binarną, która nie
    wyraża dokładnie ułamków dziesiętnych.
    """
    event = PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert event.value == Decimal("49.99")
    assert event.items[0].price == Decimal("19.99")


def test_strict_python_rejects_decimal_as_string(valid_native_payload: dict[str, Any]) -> None:
    """Wariant negatywny: ta sama wartość odrzucona przy walidacji obiektu Pythona.

    Test dokumentuje asymetrię, która potrafi zaskoczyć: `"19.99"` jest poprawne
    w `model_validate_json` i niepoprawne w `model_validate`. Nie jest to niespójność
    biblioteki, tylko konsekwencja tego, że w obiekcie Pythona istnieje typ `Decimal`,
    więc łańcuch znaków jest tam realnym błędem typu.
    """
    valid_native_payload["items"][0]["price"] = "19.99"

    with pytest.raises(ValidationError) as exc_info:
        PurchaseEvent.model_validate(valid_native_payload)

    assert "is_instance_of" in error_types(exc_info.value)


def test_strict_python_accepts_native_types(valid_native_payload: dict[str, Any]) -> None:
    """Wariant pozytywny dla ścieżki obiektowej: natywne typy przechodzą.

    Tą ścieżką idą dane budowane w kodzie - na przykład przez generator z etapu 3.
    """
    event = PurchaseEvent.model_validate(valid_native_payload)

    assert event.value == Decimal("49.99")


# --- precyzja liczb ---------------------------------------------------------


def test_decimal_from_json_number_keeps_precision() -> None:
    """Liczba JSON-a trafia do `Decimal` bez pośrednictwa float.

    To jest powód, dla którego kwoty są typu `Decimal`, a nie `float`. Na floatach
    0.1 * 3 daje 0.30000000000000004, więc walidator sumy odrzuciłby poprawny rekord.
    Pydantic parsuje liczbę z tekstu JSON-a wprost do `Decimal`, więc suma się zgadza.
    """
    payload = {
        "event_id": "3f6c2e1a-8b4d-4f2a-9c3e-7d1b5a9e0c42",
        "transaction_id": "T-PRECISION-1",
        "customer_id": "CUST-1",
        "currency": "PLN",
        "value": "0.30",
        "shipping": "0.00",
        "discount": "0.00",
        "items": [{"item_id": "S", "item_name": "Drobiazg", "price": 0.1, "quantity": 3}],
        "event_timestamp": "2026-06-15T12:00:00+00:00",
        "traffic_source": {"source": "direct", "medium": "direct"},
    }

    event = PurchaseEvent.model_validate_json(json.dumps(payload))

    assert event.items[0].price == Decimal("0.1")
    assert event.value == Decimal("0.30")
    # Kontrola negatywna: na floatach ta sama operacja daje inny wynik.
    assert 0.1 * 3 != 0.3


# --- co tryb łagodny wyłącza, a czego nie -----------------------------------


def test_lax_mode_does_not_disable_field_constraints(valid_payload: dict[str, Any]) -> None:
    """Tryb łagodny wyłącza koercję typów, nie ograniczenia wartości.

    Rozróżnienie warte zapamiętania: `strict=False` pozwala przysłać `"2"` zamiast `2`,
    ale nie pozwala przysłać kwoty z czterema miejscami po przecinku ani ceny ujemnej.
    Ograniczenia z `Field` obowiązują w obu trybach.
    """
    valid_payload["items"][0]["price"] = "19.9999"

    with pytest.raises(ValidationError) as exc_info:
        PurchaseEvent.model_validate_json(as_json(valid_payload), strict=False)

    assert "decimal_max_places" in error_types(exc_info.value)


def test_lax_mode_does_not_disable_cross_field_validation(valid_payload: dict[str, Any]) -> None:
    """Walidator spójności działa także w trybie łagodnym.

    Koercja dotyczy pojedynczych pól. Reguła „wartość musi się zgadzać z pozycjami"
    jest regułą modelu i nie ma trybu, w którym zostaje pominięta.
    """
    valid_payload["value"] = "99.99"

    with pytest.raises(ValidationError) as exc_info:
        PurchaseEvent.model_validate_json(as_json(valid_payload), strict=False)

    assert "value_mismatch" in error_types(exc_info.value)
