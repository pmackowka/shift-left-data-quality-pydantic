"""Reguła 5: duplikat identyfikatora transakcji.

Reguła stoi osobno od modelu, bo jako jedyna wymaga pamięci o innych rekordach.
Testy pilnują tej granicy: duplikat kończy się `BusinessRuleError`, nigdy
`ValidationError` - i to rozróżnienie jest tu przedmiotem testu, nie szczegółem.
"""

import pytest
from pydantic import ValidationError

from dq_contracts import (
    BusinessRuleError,
    DuplicateTransactionError,
    QuarantineReason,
    TransactionRegistry,
)


def test_first_occurrence_is_accepted() -> None:
    """Wariant pozytywny: pierwsze wystąpienie identyfikatora przechodzi."""
    registry = TransactionRegistry()

    registry.register("T-1")

    assert "T-1" in registry
    assert len(registry) == 1
    assert registry.duplicate_count == 0


def test_distinct_identifiers_are_accepted() -> None:
    """Wariant pozytywny: różne identyfikatory nie kolidują ze sobą."""
    registry = TransactionRegistry()

    for transaction_id in ("T-1", "T-2", "T-3"):
        registry.register(transaction_id)

    assert len(registry) == 3
    assert registry.duplicate_count == 0


def test_duplicate_is_rejected() -> None:
    """Wariant negatywny: powtórzony identyfikator kończy się błędem biznesowym."""
    registry = TransactionRegistry()
    registry.register("T-1")

    with pytest.raises(DuplicateTransactionError) as exc_info:
        registry.register("T-1")

    assert exc_info.value.transaction_id == "T-1"
    assert exc_info.value.reason is QuarantineReason.DUPLICATE_TRANSACTION


def test_duplicate_is_a_business_error_not_a_validation_error() -> None:
    """Sedno rozróżnienia: duplikat NIE jest błędem walidacji.

    Gdyby był, trafiłby w tę samą obsługę co zła waluta - a to dwie różne sytuacje.
    Zła waluta oznacza zepsuty rekord i wymaga poprawki po stronie producenta.
    Duplikat oznacza rekord poprawny, który już mamy; w systemie z gwarancją
    „at least once" jest stanem normalnym i wymaga wyłącznie pominięcia.
    """
    registry = TransactionRegistry()
    registry.register("T-1")

    with pytest.raises(BusinessRuleError):
        registry.register("T-1")

    # Ta sama sytuacja NIE daje się złapać jako błąd walidacji pydantic.
    registry_2 = TransactionRegistry()
    registry_2.register("T-2")
    with pytest.raises(DuplicateTransactionError):
        try:
            registry_2.register("T-2")
        except ValidationError:  # pragma: no cover - gałąź nie ma prawa się wykonać
            pytest.fail("Duplikat nie powinien być zgłaszany jako ValidationError")


def test_duplicate_counter_counts_every_repetition() -> None:
    """Trzecie wystąpienie to drugi duplikat, nie pierwszy.

    Licznik zasila raport z przebiegu, więc musi liczyć powtórzenia, a nie liczbę
    identyfikatorów, które kiedykolwiek się powtórzyły.
    """
    registry = TransactionRegistry()
    registry.register("T-1")

    for _ in range(3):
        with pytest.raises(DuplicateTransactionError):
            registry.register("T-1")

    assert registry.duplicate_count == 3
    assert len(registry) == 1


def test_registry_state_is_per_instance() -> None:
    """Dwa rejestry nie widzą się nawzajem.

    Test dokumentuje ograniczenie, nie funkcję: pamięć jest lokalna dla procesu.
    Druga instancja konsumenta na Cloud Run ma własny rejestr, więc deduplikacja
    trwała musi się odbyć warstwę niżej, w hurtowni.
    """
    first = TransactionRegistry()
    second = TransactionRegistry()

    first.register("T-1")
    second.register("T-1")

    assert len(first) == 1
    assert len(second) == 1
