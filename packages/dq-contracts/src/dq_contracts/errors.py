"""Błędy biznesowe i kody błędów walidacji.

Ten moduł pilnuje granicy, która w projektach o jakości danych zaciera się najczęściej:
różnicy między naruszeniem schematu a naruszeniem reguły biznesowej.

`ValidationError` (z pydantic) mówi: ten rekord sam w sobie nie spełnia kontraktu.
Da się to stwierdzić, patrząc wyłącznie na rekord - cena jest ujemna, waluty nie ma
na liście, brakuje pola.

`BusinessRuleError` (stąd) mówi: rekord jest poprawny, ale nie wolno go przyjąć
w tym kontekście. Duplikat transakcji jest tego przykładem podręcznikowym - żeby
go wykryć, trzeba wiedzieć, co przyszło wcześniej. Model pojedynczego zdarzenia
takiej wiedzy nie ma i mieć nie powinien.

Konsekwencja praktyczna: walidację schematu da się uruchomić na pojedynczym rekordzie,
równolegle, bez stanu. Reguły biznesowe wymagają stanu, więc mają inny koszt, inne
miejsce w pipelinie i inne możliwości skalowania.
"""

from dq_contracts.enums import QuarantineReason

# Kody błędów dla walidatorów własnych.
#
# Walidatory rzucają `PydanticCustomError` z tymi kodami, a nie gołe `ValueError`.
# Powód jest konkretny: kod trafia do pola `type` w `ValidationError.errors()`,
# dzięki czemu mapowanie błędu na powód kwarantanny opiera się na stabilnym
# identyfikatorze, a nie na treści komunikatu. Komunikat można przetłumaczyć albo
# poprawić literówkę - i mapowanie po tekście pada w dniu, w którym ktoś to zrobi.
ERROR_FUTURE_TIMESTAMP = "future_timestamp"
ERROR_VALUE_MISMATCH = "value_mismatch"


class BusinessRuleError(Exception):
    """Rekord przechodzi walidację schematu, ale łamie regułę biznesową.

    Klasa bazowa. Nie rzucamy jej bezpośrednio - każda reguła ma własny podtyp,
    żeby wywołujący mógł ją obsłużyć osobno.
    """

    reason: QuarantineReason = QuarantineReason.SCHEMA_VIOLATION


class DuplicateTransactionError(BusinessRuleError):
    """Identyfikator transakcji wystąpił już wcześniej w tym przebiegu.

    Duplikat nie jest awarią - w systemie z gwarancją „at least once" jest stanem
    normalnym. Pub/Sub może dostarczyć tę samą wiadomość dwa razy, a ponowne wgranie
    pliku w trybie batch to codzienność. Dlatego duplikat trafia do kwarantanny
    z własnym powodem, a nie na dead-letter.
    """

    reason = QuarantineReason.DUPLICATE_TRANSACTION

    def __init__(self, transaction_id: str) -> None:
        self.transaction_id = transaction_id
        super().__init__(f"Transaction '{transaction_id}' has already been seen in this run")
