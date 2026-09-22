"""Reguły kontraktu egzekwowane przez model `PurchaseEvent`.

Każda reguła ma test pozytywny i negatywny. Testy negatywne sprawdzają KOD błędu
(`type`), a nie jego treść - komunikat wolno poprawić bez podbijania wersji kontraktu,
kod błędu nie.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from freezegun import freeze_time
from pydantic import ValidationError

from dq_contracts import MAX_CLOCK_SKEW, Currency, PurchaseEvent
from dq_contracts.errors import ERROR_FUTURE_TIMESTAMP, ERROR_VALUE_MISMATCH
from tests.contracts.helpers import as_json


def error_types(exc: ValidationError) -> list[str]:
    """Wyciąga kody błędów - pomocnik, żeby asercje nie powtarzały tej samej składni."""
    return [item["type"] for item in exc.errors()]


# --- przypadek odniesienia --------------------------------------------------


def test_valid_event_passes(valid_payload: dict[str, Any]) -> None:
    """Payload z fabryki musi przechodzić - inaczej wszystkie testy negatywne kłamią."""
    event = PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert event.transaction_id == "T-2026-000123"
    assert event.currency is Currency.PLN
    assert event.value == Decimal("49.99")
    assert len(event.items) == 2
    # Wersja kontraktu wypełnia się sama i jedzie z rekordem do hurtowni.
    assert event.contract_version == "0.1.0"


# --- reguła 1: wartość transakcji kontra suma pozycji -----------------------


def test_value_matching_line_items_passes(valid_payload: dict[str, Any]) -> None:
    """Wariant pozytywny: 45.00 + 9.99 - 5.00 = 49.99."""
    event = PurchaseEvent.model_validate_json(as_json(valid_payload))
    items_total = sum(item.line_total for item in event.items)

    assert items_total == Decimal("45.00")
    assert items_total + event.shipping - event.discount == event.value


def test_value_not_matching_line_items_is_rejected(valid_payload: dict[str, Any]) -> None:
    """Wariant negatywny: klasyczny błąd cross-field.

    Rekord jest poprawny pod każdym innym względem - każde pole z osobna przechodzi
    walidację. Dopiero zestawienie ich razem pokazuje, że dane są niespójne. Tego
    rodzaju błędu nie wyłapie żaden schemat opisujący pojedyncze pola, w tym JSON Schema
    bez rozszerzeń.
    """
    valid_payload["value"] = "99.99"

    with pytest.raises(ValidationError) as exc_info:
        PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert ERROR_VALUE_MISMATCH in error_types(exc_info.value)
    # Komunikat zawiera obie liczby, więc da się naprawić rekord bez liczenia w głowie.
    assert "99.99" in str(exc_info.value)


def test_value_mismatch_by_one_grosz_is_rejected(valid_payload: dict[str, Any]) -> None:
    """Wariant negatywny graniczny: różnica jednego grosza też jest błędem.

    Test istnieje po to, żeby udokumentować brak tolerancji. Gdyby ktoś kiedyś dodał
    próg „różnice poniżej grosza ignorujemy", ten test padnie i wymusi świadomą decyzję,
    zamiast po cichu przepuścić błąd zaokrąglenia.
    """
    valid_payload["value"] = "50.00"

    with pytest.raises(ValidationError) as exc_info:
        PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert ERROR_VALUE_MISMATCH in error_types(exc_info.value)


# --- reguła 2: znacznik czasu z przyszłości ---------------------------------


def test_past_timestamp_passes(valid_payload: dict[str, Any]) -> None:
    """Wariant pozytywny: zdarzenie sprzed godziny, normalizowane do UTC."""
    event = PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert event.event_timestamp.tzinfo is not None
    assert event.event_timestamp < datetime.now(UTC)


def test_future_timestamp_is_rejected(valid_payload: dict[str, Any]) -> None:
    """Wariant negatywny: zdarzenie z jutra.

    W praktyce oznacza zegar przestawiony o strefę czasową albo producenta, który
    wysyła datę zamówienia zamiast daty zdarzenia.
    """
    valid_payload["event_timestamp"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()

    with pytest.raises(ValidationError) as exc_info:
        PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert ERROR_FUTURE_TIMESTAMP in error_types(exc_info.value)


@freeze_time("2026-06-15 12:00:00+00:00")
def test_timestamp_within_clock_skew_passes(valid_payload: dict[str, Any]) -> None:
    """Wariant pozytywny graniczny: sekunda przed końcem tolerancji zegara.

    Czas zamrożony, bo test bada granicę - bez zamrożenia wynik zależałby od tego,
    ile milisekund upłynęło między zbudowaniem payloadu a walidacją.
    """
    now = datetime.now(UTC)
    valid_payload["event_timestamp"] = (now + MAX_CLOCK_SKEW - timedelta(seconds=1)).isoformat()

    event = PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert event.event_timestamp > now


@freeze_time("2026-06-15 12:00:00+00:00")
def test_timestamp_beyond_clock_skew_is_rejected(valid_payload: dict[str, Any]) -> None:
    """Wariant negatywny graniczny: sekunda za granicą tolerancji."""
    valid_payload["event_timestamp"] = (
        datetime.now(UTC) + MAX_CLOCK_SKEW + timedelta(seconds=1)
    ).isoformat()

    with pytest.raises(ValidationError) as exc_info:
        PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert ERROR_FUTURE_TIMESTAMP in error_types(exc_info.value)


def test_naive_timestamp_is_rejected(valid_payload: dict[str, Any]) -> None:
    """Wariant negatywny: data bez strefy czasowej.

    `AwareDatetime` odrzuca ją zanim dojdzie do walidatora przyszłości - i dobrze,
    bo porównanie daty naiwnej ze świadomą rzuciłoby TypeError zamiast błędu walidacji.
    """
    valid_payload["event_timestamp"] = "2026-06-15T12:00:00"

    with pytest.raises(ValidationError) as exc_info:
        PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert "timezone_aware" in error_types(exc_info.value)


# --- reguła 3: dodatnie ceny i ilości ---------------------------------------


@pytest.mark.parametrize("bad_price", ["0.00", "-19.99"])
def test_non_positive_price_is_rejected(valid_payload: dict[str, Any], bad_price: str) -> None:
    """Wariant negatywny: cena zerowa i ujemna.

    Zero jest tu równie niepoprawne jak wartość ujemna: pozycja za darmo to albo błąd
    integracji, albo rabat wpisany nie w to pole.
    """
    valid_payload["items"][0]["price"] = bad_price

    with pytest.raises(ValidationError) as exc_info:
        PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert "greater_than" in error_types(exc_info.value)


@pytest.mark.parametrize("bad_quantity", [0, -3])
def test_non_positive_quantity_is_rejected(
    valid_payload: dict[str, Any], bad_quantity: int
) -> None:
    """Wariant negatywny: ilość zerowa i ujemna."""
    valid_payload["items"][0]["quantity"] = bad_quantity

    with pytest.raises(ValidationError) as exc_info:
        PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert "greater_than" in error_types(exc_info.value)


def test_quantity_above_limit_is_rejected(valid_payload: dict[str, Any]) -> None:
    """Wariant negatywny: ilość ponad górny limit kontraktu.

    Ochrona przed literówką w integracji - 100000 sztuk koszulki w jednym zamówieniu
    zniekształci każdy raport sprzedaży, zanim ktokolwiek to zauważy.
    """
    valid_payload["items"][0]["quantity"] = 100_000

    with pytest.raises(ValidationError) as exc_info:
        PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert "less_than_equal" in error_types(exc_info.value)


def test_too_many_decimal_places_is_rejected(valid_payload: dict[str, Any]) -> None:
    """Wariant negatywny: cena z czterema miejscami po przecinku.

    Bez tej reguły kwota zostałaby zaokrąglona przy zapisie do kolumny NUMERIC
    w BigQuery - po cichu, bez śladu w logach.
    """
    valid_payload["items"][0]["price"] = "19.9999"

    with pytest.raises(ValidationError) as exc_info:
        PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert "decimal_max_places" in error_types(exc_info.value)


def test_empty_items_list_is_rejected(valid_payload: dict[str, Any]) -> None:
    """Wariant negatywny: zakup bez pozycji."""
    valid_payload["items"] = []
    valid_payload["value"] = "4.99"

    with pytest.raises(ValidationError) as exc_info:
        PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert "too_short" in error_types(exc_info.value)


# --- reguła 4: waluta ze zbioru ---------------------------------------------


@pytest.mark.parametrize("currency", ["PLN", "EUR", "USD", "GBP", "CZK"])
def test_supported_currency_passes(valid_payload: dict[str, Any], currency: str) -> None:
    """Wariant pozytywny: każda waluta ze zbioru przechodzi."""
    valid_payload["currency"] = currency

    event = PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert event.currency == Currency(currency)


@pytest.mark.parametrize("currency", ["XYZ", "pln", "PLZ", ""])
def test_unsupported_currency_is_rejected(valid_payload: dict[str, Any], currency: str) -> None:
    """Wariant negatywny: waluta spoza zbioru, w tym poprawna waluta małymi literami.

    `"pln"` jest tu celowo: enum nie normalizuje wielkości liter, więc producent
    wysyłający małe litery zostanie odrzucony. To świadoma decyzja - cicha normalizacja
    ukrywa fakt, że dwa systemy mają różne konwencje.
    """
    valid_payload["currency"] = currency

    with pytest.raises(ValidationError) as exc_info:
        PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert "enum" in error_types(exc_info.value)


# --- reguła 6: pola wymagane i nadmiarowe -----------------------------------


@pytest.mark.parametrize("field", ["transaction_id", "currency", "value", "items"])
def test_missing_required_field_is_rejected(valid_payload: dict[str, Any], field: str) -> None:
    """Wariant negatywny: brak pola wymaganego."""
    del valid_payload[field]

    with pytest.raises(ValidationError) as exc_info:
        PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert "missing" in error_types(exc_info.value)


def test_optional_fields_use_defaults(valid_payload: dict[str, Any]) -> None:
    """Wariant pozytywny: pola opcjonalne mają sensowne wartości domyślne.

    Brak wysyłki i rabatu to nie brak danych, tylko zero - i kontrakt tak to traktuje.
    """
    del valid_payload["shipping"]
    del valid_payload["discount"]
    valid_payload["value"] = "45.00"

    event = PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert event.shipping == Decimal("0.00")
    assert event.discount == Decimal("0.00")
    assert event.traffic_source.campaign == "summer-sale"


def test_extra_field_is_rejected(valid_payload: dict[str, Any]) -> None:
    """Wariant negatywny: pole spoza kontraktu.

    To jest ta reguła, która najczęściej ratuje projekt. Domyślne zachowanie pydantic
    (`extra="ignore"`) sprawia, że producent dodaje pole, konsument je milcząco wyrzuca,
    a rozmowa „przecież wysyłamy tę daną od miesiąca" odbywa się kwartał później.
    """
    valid_payload["user_agent"] = "Mozilla/5.0"

    with pytest.raises(ValidationError) as exc_info:
        PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert "extra_forbidden" in error_types(exc_info.value)


def test_extra_field_in_nested_model_is_rejected(valid_payload: dict[str, Any]) -> None:
    """Wariant negatywny: pole nadmiarowe w modelu zagnieżdżonym.

    Osobny test, bo `model_config` NIE dziedziczy się do modeli zagnieżdżonych -
    gdyby `LineItem` nie miał własnego `extra="forbid"`, ten przypadek przeszedłby
    bez słowa, mimo że model nadrzędny jest surowy.
    """
    valid_payload["items"][0]["discount_code"] = "SUMMER"

    with pytest.raises(ValidationError) as exc_info:
        PurchaseEvent.model_validate_json(as_json(valid_payload))

    assert "extra_forbidden" in error_types(exc_info.value)


def test_event_is_immutable(valid_payload: dict[str, Any]) -> None:
    """Zwalidowane zdarzenie jest faktem historycznym i nie da się go podmienić."""
    event = PurchaseEvent.model_validate_json(as_json(valid_payload))

    with pytest.raises(ValidationError) as exc_info:
        # mypy słusznie zgłasza tu błąd: pole modelu `frozen` jest tylko do odczytu
        # i wie o tym statycznie. Wyciszamy punktowo, bo celem testu jest sprawdzenie,
        # że ta sama zasada obowiązuje w czasie wykonania, nie tylko w analizie typów.
        event.value = Decimal("1.00")  # type: ignore[misc]

    assert "frozen_instance" in error_types(exc_info.value)
