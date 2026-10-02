"""Kontrakty danych dla zdarzeń ecommerce.

Ten pakiet jest jedynym źródłem prawdy o schemacie. Oba pipeline'y (streaming
i batch) oraz generator schematów BigQuery importują stąd te same modele.

Dlaczego to ma znaczenie: gdy schemat istnieje w dwóch miejscach - raz jako
model w kodzie, raz jako definicja tabeli w hurtowni - rozjeżdża się przy
pierwszej zmianie, a rozjazd wychodzi na jaw dopiero przy błędnym raporcie.
Tutaj definicja tabeli BigQuery powstaje z modelu, więc rozjazd jest niemożliwy.

Podział na moduły odpowiada podziałowi odpowiedzialności:

- `events`     - czym jest poprawne zdarzenie (kontrakt),
- `enums`      - jakie wartości są dopuszczalne,
- `errors`     - granica między błędem schematu a błędem biznesowym,
- `dedup`      - jedyna reguła wymagająca pamięci o innych rekordach,
- `quarantine` - co się dzieje z rekordem, który kontraktu nie spełnił,
- `version`    - numer wersji kontraktu, bez żadnych zależności,
- `bigquery`   - schematy tabel BigQuery wyprowadzone z modeli (`make schemas`).

Wersja pakietu zmienia się zgodnie z SemVer; zmiana łamiąca kontrakt to major.
Numer wersji jedzie w polu `contract_version` każdego zdarzenia, dzięki czemu
w hurtowni widać, które reguły obowiązywały przy przyjęciu rekordu.
"""

from dq_contracts.dedup import TransactionRegistry
from dq_contracts.enums import Channel, Currency, PipelineStage, QuarantineReason
from dq_contracts.errors import BusinessRuleError, DuplicateTransactionError
from dq_contracts.events import MAX_CLOCK_SKEW, LineItem, PurchaseEvent, TrafficSource
from dq_contracts.quarantine import (
    RejectedRecord,
    ValidationIssue,
    reason_for,
    to_rejected_record,
    to_rejected_record_from_business_error,
    to_rejected_record_from_malformed,
)
from dq_contracts.version import CONTRACT_VERSION

__version__ = CONTRACT_VERSION

# __all__ wyznacza publiczne API pakietu. To nie jest formalność: wszystko spoza tej
# listy wolno przenieść, przemianować albo usunąć bez podbijania wersji major,
# bo kontrakt obejmuje tylko to, co jest tu wymienione.
__all__ = [
    "CONTRACT_VERSION",
    "MAX_CLOCK_SKEW",
    "BusinessRuleError",
    "Channel",
    "Currency",
    "DuplicateTransactionError",
    "LineItem",
    "PipelineStage",
    "PurchaseEvent",
    "QuarantineReason",
    "RejectedRecord",
    "TrafficSource",
    "TransactionRegistry",
    "ValidationIssue",
    "__version__",
    "reason_for",
    "to_rejected_record",
    "to_rejected_record_from_business_error",
    "to_rejected_record_from_malformed",
]
