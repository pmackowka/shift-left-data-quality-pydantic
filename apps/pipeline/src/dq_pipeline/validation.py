"""Werdykt kontraktu dla pojedynczej wiadomości - wspólny dla wszystkich etapów pipeline'u.

Kolejność sprawdzeń jest częścią logiki, nie szczegółem implementacji:

1. Czy to w ogóle JSON? Nie - kwarantanna z powodem `malformed_payload`.
2. Czy spełnia schemat kontraktu? Nie - kwarantanna z powodem z `ValidationError`.
3. Czy transakcja nie była już przyjęta? Była - kwarantanna jako duplikat.

Rejestr transakcji widzi WYŁĄCZNIE rekordy, które przeszły punkt 2. Gdyby rejestrować
identyfikator przed walidacją schematu, rekord odrzucony za złą walutę „zająłby"
swój `transaction_id`, a jego poprawiona wersja wysłana chwilę później zostałaby
uznana za duplikat - czyli naprawienie danych byłoby karane.

Uszkodzony JSON trafia do kwarantanny, a nie na dead-letter. Ponowne dostarczenie tych
samych bajtów da ten sam błąd, więc retry tylko opóźnia decyzję i zajmuje kolejkę.
Dead-letter jest dla awarii przetwarzania, po których ponowienie MA szansę się udać.
"""

import threading
from dataclasses import dataclass

from pydantic import ValidationError

from dq_contracts import (
    BusinessRuleError,
    PipelineStage,
    PurchaseEvent,
    RejectedRecord,
    TransactionRegistry,
    to_rejected_record,
    to_rejected_record_from_business_error,
    to_rejected_record_from_malformed,
)

# Kod błędu pydantic dla tekstu, który nie jest poprawnym JSON-em. Obejmuje też bajty
# spoza UTF-8 i pustą wiadomość - sprawdzone empirycznie, nie założone.
_JSON_INVALID = "json_invalid"


@dataclass(frozen=True, slots=True)
class Accepted:
    """Rekord przyjęty - zwalidowany model gotowy do zapisu w tabeli `events`."""

    event: PurchaseEvent


@dataclass(frozen=True, slots=True)
class Rejected:
    """Rekord odrzucony - gotowy wiersz kwarantanny z powodem i surowym payloadem."""

    record: RejectedRecord


Verdict = Accepted | Rejected


def _decode(raw: bytes) -> str:
    # `replace` zamiast `strict`: payload z błędnym kodowaniem też musi dać się zapisać
    # w kwarantannie. Utracone bajty zamieniają się w U+FFFD - to i tak więcej, niż
    # zostałoby po wyjątku UnicodeDecodeError, który wywróciłby obsługę błędu.
    return raw.decode("utf-8", errors="replace")


class RecordValidator:
    """Waliduje wiadomości dla jednego etapu pipeline'u, z pamięcią o transakcjach.

    Jedna instancja na proces i etap. Usługa ingest obsługuje żądania równolegle
    (FastAPI uruchamia synchroniczne endpointy w puli wątków), a para „sprawdź, czy
    było - zapamiętaj" w rejestrze nie jest atomowa. Bez blokady dwa równoległe
    żądania z tym samym `transaction_id` mogłyby oba przejść jako pierwsze.
    Blokada obejmuje tylko rejestr - walidacja schematu jest bezstanowa i może
    działać równolegle.
    """

    def __init__(self, stage: PipelineStage, registry: TransactionRegistry | None = None) -> None:
        self.stage = stage
        self.registry = registry if registry is not None else TransactionRegistry()
        self._lock = threading.Lock()

    def validate(self, raw: bytes) -> Verdict:
        try:
            event = PurchaseEvent.model_validate_json(raw)
        except ValidationError as exc:
            text = _decode(raw)
            if any(error["type"] == _JSON_INVALID for error in exc.errors()):
                return Rejected(to_rejected_record_from_malformed(text, self.stage))
            return Rejected(to_rejected_record(text, exc, self.stage))

        try:
            with self._lock:
                self.registry.register(event.transaction_id)
        except BusinessRuleError as exc:
            return Rejected(to_rejected_record_from_business_error(_decode(raw), exc, self.stage))
        return Accepted(event)
