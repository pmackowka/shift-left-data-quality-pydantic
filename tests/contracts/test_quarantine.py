"""Zamiana błędu na rekord kwarantanny.

Testy sprawdzają trzy rzeczy: czy powód odrzucenia jest trafnie sklasyfikowany,
czy wybór jednego powodu spośród kilku jest deterministyczny, i czy rekord zachowuje
materiał potrzebny do naprawy danych.
"""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from dq_contracts import (
    DuplicateTransactionError,
    PipelineStage,
    PurchaseEvent,
    QuarantineReason,
    reason_for,
    to_rejected_record,
    to_rejected_record_from_business_error,
    to_rejected_record_from_malformed,
)
from tests.contracts.helpers import as_json


def reject(payload: dict[str, Any], stage: PipelineStage = PipelineStage.INGEST) -> Any:
    """Waliduje payload i zamienia oczekiwany błąd na rekord kwarantanny."""
    raw = as_json(payload)
    try:
        PurchaseEvent.model_validate_json(raw)
    except ValidationError as error:
        return to_rejected_record(raw, error, stage)
    pytest.fail("Payload przeszedł walidację, a nie powinien")


# --- klasyfikacja powodów ---------------------------------------------------


@pytest.mark.parametrize(
    ("error_type", "field_path", "expected"),
    [
        ("missing", "value", QuarantineReason.MISSING_FIELD),
        ("extra_forbidden", "user_agent", QuarantineReason.UNEXPECTED_FIELD),
        ("enum", "currency", QuarantineReason.UNSUPPORTED_CURRENCY),
        ("enum", "traffic_source.medium", QuarantineReason.OUT_OF_RANGE),
        ("greater_than", "items.0.price", QuarantineReason.NON_POSITIVE_AMOUNT),
        ("less_than_equal", "items.0.quantity", QuarantineReason.OUT_OF_RANGE),
        ("future_timestamp", "event_timestamp", QuarantineReason.FUTURE_TIMESTAMP),
        ("value_mismatch", "", QuarantineReason.VALUE_MISMATCH),
        # Heurystyka po kształcie nazwy - obejmuje kody typów, których nie ma w tabeli.
        ("int_type", "items.0.quantity", QuarantineReason.TYPE_MISMATCH),
        ("decimal_parsing", "value", QuarantineReason.TYPE_MISMATCH),
        ("timezone_aware", "event_timestamp", QuarantineReason.TYPE_MISMATCH),
        ("is_instance_of", "currency", QuarantineReason.TYPE_MISMATCH),
        # Kod nieznany nie może wywrócić klasyfikacji - wpada do worka ogólnego.
        ("some_future_pydantic_code", "value", QuarantineReason.SCHEMA_VIOLATION),
    ],
)
def test_reason_classification(
    error_type: str, field_path: str, expected: QuarantineReason
) -> None:
    """Każdy techniczny kod błędu ma przewidywalną kategorię raportową."""
    assert reason_for(error_type, field_path) is expected


# --- budowa rekordu ---------------------------------------------------------


def test_rejected_record_keeps_payload_and_context(valid_payload: dict[str, Any]) -> None:
    """Wariant podstawowy: rekord kwarantanny zachowuje oryginał i kontekst.

    Surowy payload jest tu najważniejszy - bez niego rekordu nie da się naprawić
    i ponownie wgrać, a kwarantanna staje się wyłącznie licznikiem.
    """
    valid_payload["value"] = "99.99"

    record = reject(valid_payload)

    assert record.reason is QuarantineReason.VALUE_MISMATCH
    assert record.stage is PipelineStage.INGEST
    assert record.contract_version == "0.1.0"
    assert record.transaction_id == "T-2026-000123"
    assert json.loads(record.raw_payload)["value"] == "99.99"
    assert len(record.issues) == 1


def test_rejected_record_points_at_the_offending_field(valid_payload: dict[str, Any]) -> None:
    """Ścieżka pola wskazuje konkretną pozycję na paragonie, nie całe zdarzenie."""
    valid_payload["items"][1]["price"] = "-5.02"

    record = reject(valid_payload)

    paths = [issue.field_path for issue in record.issues]
    assert "items.1.price" in paths
    assert record.reason is QuarantineReason.NON_POSITIVE_AMOUNT


def test_reason_selection_is_deterministic_for_multiple_errors(
    valid_payload: dict[str, Any],
) -> None:
    """Rekord łamiący kilka reguł naraz dostaje jedną, przewidywalną etykietę.

    Tu naruszone są trzy reguły: pole nadmiarowe, zła waluta i niezgodna suma.
    Raport dostaje `UNEXPECTED_FIELD`, bo błędy strukturalne mają pierwszeństwo -
    świadczą o rozjeździe kontraktu, który dotyczy całego strumienia, a nie
    pojedynczego rekordu.
    """
    valid_payload["user_agent"] = "Mozilla/5.0"
    valid_payload["currency"] = "XYZ"
    valid_payload["value"] = "99.99"

    record = reject(valid_payload)

    assert record.reason is QuarantineReason.UNEXPECTED_FIELD
    assert len(record.issues) >= 2


def test_malformed_payload_is_quarantined_without_parsing() -> None:
    """Payload, którego nie da się sparsować, też ma swoje miejsce.

    Nie znamy identyfikatora transakcji ani żadnego pola - zostaje surowy tekst.
    Bez tej ścieżki uszkodzona wiadomość kończyłaby jako wyjątek w logu.
    """
    record = to_rejected_record_from_malformed('{"transaction_id": "T-1"', PipelineStage.INGEST)

    assert record.reason is QuarantineReason.MALFORMED_PAYLOAD
    assert record.transaction_id is None
    assert record.raw_payload.startswith('{"transaction_id"')


def test_business_error_is_quarantined_with_its_own_reason(valid_payload: dict[str, Any]) -> None:
    """Duplikat trafia do tej samej tabeli kwarantanny, ale z własnym powodem.

    Wspólna tabela jest celowa: zespół danych ma jedno miejsce, w którym widzi
    wszystko, co nie weszło. Osobny powód pozwala te przypadki rozdzielić w raporcie.
    """
    raw = as_json(valid_payload)

    record = to_rejected_record_from_business_error(
        raw, DuplicateTransactionError("T-2026-000123"), PipelineStage.BATCH
    )

    assert record.reason is QuarantineReason.DUPLICATE_TRANSACTION
    assert record.stage is PipelineStage.BATCH
    assert record.transaction_id == "T-2026-000123"
    assert record.issues[0].error_type == "DuplicateTransactionError"


def test_transaction_id_is_absent_when_payload_lacks_it() -> None:
    """Brak identyfikatora w payloadzie nie wywraca budowy rekordu kwarantanny."""
    raw = json.dumps({"currency": "PLN"})

    try:
        PurchaseEvent.model_validate_json(raw)
    except ValidationError as error:
        record = to_rejected_record(raw, error, PipelineStage.SOURCE)

    assert record.transaction_id is None
    assert record.reason is QuarantineReason.MISSING_FIELD


def test_long_input_value_is_truncated(valid_payload: dict[str, Any]) -> None:
    """Zbyt długa wartość wejściowa zostaje obcięta, a nie odrzucona.

    Zapis do kwarantanny nie ma prawa się nie udać z powodu danych, które do niej
    trafiają - to byłby jedyny moment, w którym naprawdę tracimy rekord.
    """
    valid_payload["items"][0]["item_name"] = "x" * 5000

    record = reject(valid_payload)

    assert all(
        issue.input_value is None or len(issue.input_value) <= 1000 for issue in record.issues
    )


# --- gałęzie obronne --------------------------------------------------------
#
# Poniższe testy pokrywają ścieżki, które w normalnym przebiegu się nie wykonują.
# Są tu właśnie dlatego: kod obsługi błędów, którego nikt nigdy nie uruchomił, to
# kod, który zadziała pierwszy raz w najgorszym możliwym momencie.


@pytest.mark.parametrize(
    "raw",
    [
        "to nie jest JSON",
        "[1, 2, 3]",  # poprawny JSON, ale nie obiekt
        '{"transaction_id": 12345}',  # identyfikator jest, ale nie jest tekstem
        "",
    ],
)
def test_transaction_id_extraction_never_raises(raw: str) -> None:
    """Wyciąganie identyfikatora z zepsutego payloadu kończy się `None`, nie wyjątkiem.

    Ta funkcja działa w ścieżce obsługi błędu, więc własny wyjątek oznaczałby utratę
    rekordu dokładnie wtedy, gdy próbujemy go uratować.
    """
    try:
        PurchaseEvent.model_validate_json('{"currency": "PLN"}')
    except ValidationError as error:
        record = to_rejected_record(raw, error, PipelineStage.SOURCE)

    assert record.transaction_id is None
    assert record.raw_payload == raw


def test_priority_fallback_for_unknown_reason_set() -> None:
    """Wybór powodu ma wartość domyślną także dla pustego zbioru.

    Sytuacja nie powinna wystąpić - `ValidationError` zawsze ma co najmniej jeden błąd.
    Test pilnuje, żeby przy przyszłej zmianie ta gałąź nie zaczęła zwracać `None`
    i wywracać zapisu do kwarantanny.
    """
    from dq_contracts.quarantine import _highest_priority

    assert _highest_priority(set()) is QuarantineReason.SCHEMA_VIOLATION
