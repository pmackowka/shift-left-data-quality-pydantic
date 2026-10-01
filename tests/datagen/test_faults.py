"""Katalog błędów: każdy wstrzyknięty błąd ląduje w kwarantannie z oczekiwanym powodem.

To są testy wyroczni. Jeśli któryś pada, generator kłamie o tym, co zepsuł - a raport
z etapu 5, porównywany z odpowiedzią wzorcową generatora, kłamałby razem z nim.
"""

import json
import random
from datetime import UTC, datetime

import pytest

from dq_contracts import QuarantineReason, TransactionRegistry
from dq_datagen.builder import build_valid_event
from dq_datagen.faults import FAULT_CATALOG, FaultContext, FaultKind, inject_fault
from tests.datagen.helpers import verdict

REFERENCE_TIME = datetime.now(UTC)
ANCHOR_ID = "T-ANCHOR-000001"

# Powody kwarantanny, których generator świadomie NIE produkuje. Lista jest jawna,
# żeby nowy powód dodany do kontraktu wywalił test kompletności poniżej - wtedy trzeba
# albo dopisać błąd do katalogu, albo dopisać powód tutaj, z uzasadnieniem.
NOT_GENERATED = {
    # Uszkodzony JSON to sprawa transportu, nie kontraktu - wejdzie razem z dead-letter
    # w etapie 4.
    QuarantineReason.MALFORMED_PAYLOAD,
    # Granice górne, długości i wzorce są ograniczeniami typów, nie osobną regułą
    # z listy reguł kontraktu.
    QuarantineReason.OUT_OF_RANGE,
    # Worek „pozostałe" - z definicji nie ma błędu, który celowo by do niego trafiał.
    QuarantineReason.SCHEMA_VIOLATION,
}


def test_catalog_covers_every_contract_rule() -> None:
    expected = {spec.expected_reason for spec in FAULT_CATALOG.values()}
    assert expected == set(QuarantineReason) - NOT_GENERATED


def test_catalog_has_entry_for_every_fault_kind() -> None:
    assert set(FAULT_CATALOG) == set(FaultKind)


@pytest.mark.parametrize("kind", list(FaultKind))
def test_fault_kind_name_matches_expected_reason(kind: FaultKind) -> None:
    """Etykieta błędu i etykieta kwarantanny są takie same - raport czyta się bez słownika."""
    assert kind.value == FAULT_CATALOG[kind].expected_reason.value


@pytest.mark.parametrize("kind", list(FaultKind))
def test_injected_fault_is_quarantined_with_expected_reason(kind: FaultKind) -> None:
    """Każdy błąd, na wielu ziarnach - wstrzykiwacze mają warianty losowe.

    Rejestr dostaje najpierw rekord-kotwicę, czyli poprawną transakcję, którą
    `duplicate_transaction` powtarza. Dla pozostałych błędów kotwica nie ma wpływu.
    """
    for seed in range(100):
        rng = random.Random(seed)
        registry = TransactionRegistry()
        registry.register(ANCHOR_ID)

        event = build_valid_event(
            rng, transaction_id=f"T-FAULT-{seed:06d}", reference_time=REFERENCE_TIME
        )
        payload = json.loads(event.model_dump_json())
        ctx = FaultContext(rng=rng, reference_time=REFERENCE_TIME, seen_transaction_ids=[ANCHOR_ID])
        inject_fault(kind, payload, ctx)
        line = json.dumps(payload)

        assert verdict(line, registry) is FAULT_CATALOG[kind].expected_reason, line


def test_duplicate_without_earlier_record_is_an_error() -> None:
    """Duplikat bez oryginału nie istnieje - generator musi to zgłosić, a nie zgadywać."""
    ctx = FaultContext(rng=random.Random(0), reference_time=REFERENCE_TIME)
    with pytest.raises(ValueError, match="earlier valid record"):
        inject_fault(FaultKind.DUPLICATE_TRANSACTION, {"transaction_id": "T-1"}, ctx)
