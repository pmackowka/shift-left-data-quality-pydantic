"""Poprawne zdarzenia z generatora muszą przechodzić kontrakt - zawsze, dla każdego ziarna."""

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from dq_contracts import PurchaseEvent, TransactionRegistry
from dq_datagen.builder import build_valid_event, random_uuid
from tests.datagen.helpers import verdict

REFERENCE_TIME = datetime.now(UTC)

# Wiele ziaren zamiast jednego: builder ma gałęzie losowe (rabat lub jego brak, liczba
# pozycji, wysyłka), a jedno ziarno sprawdziłoby tylko jedną ścieżkę przez nie.
SEEDS = range(300)


def _build(seed: int, transaction_id: str = "T-TEST-000001") -> PurchaseEvent:
    return build_valid_event(
        random.Random(seed), transaction_id=transaction_id, reference_time=REFERENCE_TIME
    )


def test_valid_events_pass_contract_through_json() -> None:
    """Rekord przechodzi kontrakt ścieżką produkcyjną: serializacja do JSON-a i z powrotem.

    Sam fakt, że konstruktor `PurchaseEvent` nie rzucił wyjątku, to za mało - pipeline
    nie dostaje obiektu, tylko tekst. Test łapie m.in. sytuację, w której serializacja
    zgubiłaby precyzję kwoty albo strefę czasową.
    """
    registry = TransactionRegistry()
    for seed in SEEDS:
        line = _build(seed, transaction_id=f"T-TEST-{seed:06d}").model_dump_json()
        assert verdict(line, registry) is None, line


def test_same_seed_gives_identical_event() -> None:
    assert _build(7).model_dump_json() == _build(7).model_dump_json()


def test_different_seeds_give_different_events() -> None:
    assert _build(7).model_dump_json() != _build(8).model_dump_json()


def test_value_is_positive_and_consistent_with_items() -> None:
    for seed in SEEDS:
        event = _build(seed)
        items_total = sum(item.line_total for item in event.items)
        assert event.value > 0
        assert event.value == items_total + event.shipping - event.discount


def test_discount_branch_is_exercised() -> None:
    """Strażnik testu wyżej: bez rabatów w próbce reguła zaokrąglania nie byłaby sprawdzona."""
    discounts = {_build(seed).discount for seed in SEEDS}
    assert Decimal("0.00") in discounts
    assert len(discounts) > 1


def test_timestamp_is_in_the_past_week() -> None:
    for seed in SEEDS:
        ts = _build(seed).event_timestamp
        assert REFERENCE_TIME - timedelta(days=7) <= ts <= REFERENCE_TIME - timedelta(minutes=1)


def test_random_uuid_is_deterministic_version_4() -> None:
    first = random_uuid(random.Random(1))
    assert first == random_uuid(random.Random(1))
    assert first.version == 4
