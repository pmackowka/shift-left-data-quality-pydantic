"""Fabryki danych testowych dla kontraktu.

Zasada: testy negatywne różnią się od pozytywnego dokładnie jednym polem. Gdyby każdy
test budował własny payload od zera, przestałoby być jasne, czy rekord został odrzucony
z powodu badanej reguły, czy z powodu literówki w polu obok.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from dq_contracts import Channel, Currency

EVENT_ID = "3f6c2e1a-8b4d-4f2a-9c3e-7d1b5a9e0c42"


def _default_timestamp() -> datetime:
    """Znacznik czasu z niedalekiej przeszłości.

    Godzina wstecz, a nie `now()`: przy `now()` test stawałby się wrażliwy na własny
    czas wykonania, gdyby tolerancja zegara kiedykolwiek zeszła do zera.
    """
    return datetime.now(UTC) - timedelta(hours=1)


@pytest.fixture
def valid_payload() -> dict[str, Any]:
    """Poprawny payload w postaci, w jakiej przychodzi z zewnątrz - same typy JSON-owe.

    Kwoty jako łańcuchy znaków, bo tak wygląda poprawnie zbudowany producent: liczba
    zmiennoprzecinkowa w JSON-ie nie ma gwarancji precyzji dziesiętnej, więc kwoty
    przesyła się tekstem. Pydantic zamieni je na `Decimal` bez straty.

    Suma kontrolna: 2 x 19.99 + 1 x 5.02 = 45.00, plus wysyłka 9.99, minus rabat 5.00,
    czyli value = 49.99.
    """
    return {
        "event_id": EVENT_ID,
        "transaction_id": "T-2026-000123",
        "customer_id": "CUST-98211",
        "currency": "PLN",
        "value": "49.99",
        "shipping": "9.99",
        "discount": "5.00",
        "items": [
            {
                "item_id": "SKU-1",
                "item_name": "Bawelniana koszulka",
                "price": "19.99",
                "quantity": 2,
            },
            {
                "item_id": "SKU-2",
                "item_name": "Skarpetki",
                "price": "5.02",
                "quantity": 1,
            },
        ],
        "event_timestamp": _default_timestamp().isoformat(),
        "traffic_source": {"source": "google", "medium": "cpc", "campaign": "summer-sale"},
    }


@pytest.fixture
def valid_native_payload() -> dict[str, Any]:
    """Ten sam rekord, ale w natywnych typach Pythona.

    Potrzebny osobno, bo w trybie `strict` walidacja obiektu Pythona wymaga instancji
    dokładnie tych typów, które deklaruje model: `UUID`, `Decimal`, `Currency`, a nie
    ich tekstowych odpowiedników. Ten payload obsługuje ścieżkę, którą w pipelinie idą
    dane zbudowane w kodzie - na przykład przez generator.
    """
    return {
        "event_id": UUID(EVENT_ID),
        "transaction_id": "T-2026-000123",
        "customer_id": "CUST-98211",
        "currency": Currency.PLN,
        "value": Decimal("49.99"),
        "shipping": Decimal("9.99"),
        "discount": Decimal("5.00"),
        "items": [
            {
                "item_id": "SKU-1",
                "item_name": "Bawelniana koszulka",
                "price": Decimal("19.99"),
                "quantity": 2,
            },
            {
                "item_id": "SKU-2",
                "item_name": "Skarpetki",
                "price": Decimal("5.02"),
                "quantity": 1,
            },
        ],
        "event_timestamp": _default_timestamp(),
        "traffic_source": {
            "source": "google",
            "medium": Channel.CPC,
            "campaign": "summer-sale",
        },
    }
