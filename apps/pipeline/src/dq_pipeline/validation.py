"""Werdykt kontraktu dla pojedynczej wiadomości - wspólny dla wszystkich etapów pipeline'u.

Kolejność sprawdzeń jest częścią logiki, nie szczegółem implementacji:

1. Czy to w ogóle JSON? Nie - kwarantanna z powodem `malformed_payload`.
2. Czy spełnia schemat kontraktu? Nie - kwarantanna z powodem z `ValidationError`.
3. Czy transakcja była już przyjęta?
   - z identyczną treścią - to powtórka (replay): pomijamy bez zapisu,
   - z inną treścią - to duplikat: kwarantanna.

Rejestr transakcji widzi WYŁĄCZNIE rekordy, które przeszły punkt 2. Gdyby rejestrować
identyfikator przed walidacją schematu, rekord odrzucony za złą walutę „zająłby"
swój `transaction_id`, a jego poprawiona wersja wysłana chwilę później zostałaby
uznana za duplikat - czyli naprawienie danych byłoby karane.

Rozróżnienie powtórki od duplikatu wynika z gwarancji „co najmniej raz". Pub/Sub może
dostarczyć tę samą wiadomość drugi raz, redrive może ją opublikować ponownie, a batch
może dostać ten sam plik jeszcze raz. To są zdarzenia transportu, nie błędy danych -
gdyby lądowały w kwarantannie, raport jakości rósłby od samych ponowień. Duplikat to
co innego: producent wysłał DWA różne rekordy z tym samym identyfikatorem transakcji
i jeden z nich jest fałszywy. To już jest problem danych i trafia do kwarantanny.

Uszkodzony JSON trafia do kwarantanny, a nie na dead-letter. Ponowne dostarczenie tych
samych bajtów da ten sam błąd, więc retry tylko opóźnia decyzję i zajmuje kolejkę.
Dead-letter jest dla awarii przetwarzania, po których ponowienie MA szansę się udać.
"""

import hashlib
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from pydantic import ValidationError

from dq_contracts import (
    DuplicateTransactionError,
    PipelineStage,
    PurchaseEvent,
    RejectedRecord,
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
class Replayed:
    """Powtórka rekordu już przyjętego - poprawna, ale nie do ponownego zapisu."""

    event: PurchaseEvent


@dataclass(frozen=True, slots=True)
class Rejected:
    """Rekord odrzucony - gotowy wiersz kwarantanny z powodem i surowym payloadem."""

    record: RejectedRecord


Verdict = Accepted | Replayed | Rejected


class Seen(Enum):
    """Wynik sprawdzenia transakcji w pamięci pipeline'u."""

    NEW = "new"
    REPLAY = "replay"
    DUPLICATE = "duplicate"


def fingerprint(canonical_json: str | bytes) -> bytes:
    """Odcisk treści zdarzenia w postaci kanonicznej (`PurchaseEvent.model_dump_json()`).

    Postać kanoniczna, a nie surowe bajty z wejścia: ten sam rekord wysłany raz z czasem
    w strefie +02:00, a raz w UTC, to po walidacji to samo zdarzenie (walidator
    normalizuje czas do UTC) i ma ten sam odcisk. Różnica w formatowaniu nie robi
    z powtórki duplikatu.

    BLAKE2b ze 128-bitowym skrótem: szybszy od SHA-256 i z prawdopodobieństwem kolizji
    pomijalnym przy dowolnej realnej liczbie transakcji. To nie jest zabezpieczenie
    kryptograficzne - nikt tu nie podrabia rekordów - tylko tani identyfikator treści.
    """
    data = canonical_json.encode() if isinstance(canonical_json, str) else canonical_json
    return hashlib.blake2b(data, digest_size=16).digest()


class TransactionLedger:
    """Pamięć przyjętych transakcji: identyfikator -> odcisk treści.

    W odróżnieniu od `TransactionRegistry` z kontraktu, który pamięta same identyfikatory,
    ta pamięć odróżnia powtórkę od duplikatu. Żyje w pipelinie, a nie w kontrakcie, bo to
    stan przetwarzania (co już przyjęliśmy), a nie reguła danych (czym jest poprawny
    rekord). Zmiana tego mechanizmu nie zmienia kontraktu, więc nie podbija jego wersji.

    Zakres pamięci: jeden proces. Batch zasila ją identyfikatorami z wcześniejszych
    loadów (`seed`), co daje idempotentność między uruchomieniami; usługa ingest
    startuje z pustą pamięcią - trwałą deduplikację w chmurze robi MERGE w BigQuery.
    """

    def __init__(self) -> None:
        self._seen: dict[str, bytes] = {}
        self.replays = 0
        self.duplicates = 0

    def seed(self, entries: Iterable[tuple[str, bytes]]) -> None:
        """Wczytuje transakcje przyjęte wcześniej, np. w poprzednich loadach batchowych."""
        self._seen.update(entries)

    def __len__(self) -> int:
        return len(self._seen)

    def classify(self, transaction_id: str, digest: bytes) -> Seen:
        """Klasyfikuje transakcję i zapamiętuje ją, jeśli jest nowa.

        Pierwszy rekord z danym identyfikatorem wygrywa. Dla strumienia to jedyna
        sensowna reguła - nie wiemy, czy przyjdzie trzeci - a dla batcha daje wynik
        zgodny z kolejnością w pliku, więc powtarzalny.
        """
        known = self._seen.get(transaction_id)
        if known is None:
            self._seen[transaction_id] = digest
            return Seen.NEW
        if known == digest:
            self.replays += 1
            return Seen.REPLAY
        self.duplicates += 1
        return Seen.DUPLICATE


def _decode(raw: bytes) -> str:
    # `replace` zamiast `strict`: payload z błędnym kodowaniem też musi dać się zapisać
    # w kwarantannie. Utracone bajty zamieniają się w U+FFFD - to i tak więcej, niż
    # zostałoby po wyjątku UnicodeDecodeError, który wywróciłby obsługę błędu.
    return raw.decode("utf-8", errors="replace")


class RecordValidator:
    """Waliduje wiadomości dla jednego etapu pipeline'u, z pamięcią o transakcjach.

    Jedna instancja na proces i etap. Usługa ingest obsługuje żądania równolegle
    (FastAPI uruchamia synchroniczne endpointy w puli wątków), a para „sprawdź, czy
    było - zapamiętaj" nie jest atomowa. Bez blokady dwa równoległe żądania z tym samym
    `transaction_id` mogłyby oba przejść jako pierwsze. Blokada obejmuje tylko pamięć
    transakcji - walidacja schematu jest bezstanowa i może działać równolegle.
    """

    def __init__(self, stage: PipelineStage, ledger: TransactionLedger | None = None) -> None:
        self.stage = stage
        self.ledger = ledger if ledger is not None else TransactionLedger()
        self._lock = threading.Lock()

    def validate(self, raw: bytes) -> Verdict:
        try:
            event = PurchaseEvent.model_validate_json(raw)
        except ValidationError as exc:
            text = _decode(raw)
            if any(error["type"] == _JSON_INVALID for error in exc.errors()):
                return Rejected(to_rejected_record_from_malformed(text, self.stage))
            return Rejected(to_rejected_record(text, exc, self.stage))

        # Odcisk liczony poza blokadą - serializacja to najdroższa część tego kroku,
        # a nie potrzebuje wyłączności.
        digest = fingerprint(event.model_dump_json())
        with self._lock:
            seen = self.ledger.classify(event.transaction_id, digest)
        if seen is Seen.NEW:
            return Accepted(event)
        if seen is Seen.REPLAY:
            return Replayed(event)
        error = DuplicateTransactionError(event.transaction_id)
        return Rejected(to_rejected_record_from_business_error(_decode(raw), error, self.stage))
