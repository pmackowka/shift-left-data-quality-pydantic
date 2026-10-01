"""Pomocniki testowe dla generatora."""

from pydantic import ValidationError

from dq_contracts import (
    BusinessRuleError,
    PipelineStage,
    PurchaseEvent,
    QuarantineReason,
    TransactionRegistry,
    to_rejected_record,
    to_rejected_record_from_business_error,
)


def verdict(line: str, registry: TransactionRegistry) -> QuarantineReason | None:
    """Werdykt kontraktu dla jednej linii NDJSON: `None` dla rekordu przyjętego.

    To pipeline w miniaturze - ta sama kolejność co w produkcji: najpierw schemat
    (bez stanu), potem rejestr transakcji (ze stanem). Odwrotna kolejność zapamiętałaby
    identyfikatory rekordów, które i tak polecą do kwarantanny, i każdy późniejszy
    poprawny rekord z tym identyfikatorem zostałby uznany za duplikat.

    Powód bierzemy z rekordu kwarantanny, a nie z surowego `ValidationError`, bo
    to rekord kwarantanny jest tym, co zobaczy raport - testujemy cały łańcuch.
    """
    try:
        event = PurchaseEvent.model_validate_json(line)
    except ValidationError as exc:
        return to_rejected_record(line, exc, PipelineStage.BATCH).reason
    try:
        registry.register(event.transaction_id)
    except BusinessRuleError as exc:
        return to_rejected_record_from_business_error(line, exc, PipelineStage.BATCH).reason
    return None
