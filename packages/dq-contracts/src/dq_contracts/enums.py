"""Zbiory wartości dopuszczonych przez kontrakt.

Dlaczego `StrEnum`, a nie zwykłe stałe tekstowe: enum daje pydantic zamkniętą listę
wartości, więc walidacja waluty spoza zbioru dzieje się sama, bez pisania walidatora.
`StrEnum` (Python 3.11+) dziedziczy po `str`, dzięki czemu wartość serializuje się
do JSON-a jako zwykły łańcuch i da się ją wstawić do BigQuery bez konwersji.
"""

from enum import StrEnum


class Currency(StrEnum):
    """Waluty obsługiwane przez kontrakt.

    Zbiór jest celowo mały. Rozszerzenie go o kolejną walutę to zmiana MINOR
    (stare dane nadal przechodzą), a usunięcie waluty to zmiana MAJOR - rekordy
    historyczne przestałyby spełniać kontrakt.
    """

    PLN = "PLN"
    EUR = "EUR"
    USD = "USD"
    GBP = "GBP"
    CZK = "CZK"


class Channel(StrEnum):
    """Kanał ruchu, odpowiednik wymiaru `medium` w GA4.

    Wartości trzymają się nazewnictwa GA4, żeby dało się je zestawić z danymi
    z analityki bez tabeli mapującej.
    """

    ORGANIC = "organic"
    CPC = "cpc"
    EMAIL = "email"
    REFERRAL = "referral"
    SOCIAL = "social"
    DIRECT = "direct"
    AFFILIATE = "affiliate"


class PipelineStage(StrEnum):
    """Miejsce w pipelinie, w którym rekord został odrzucony.

    Bez tego pola kwarantanna odpowiada na pytanie „co jest nie tak", ale nie na
    „gdzie to wyszło". Rekord odrzucony na etapie SOURCE nigdy nie opuścił producenta;
    ten sam błąd złapany na INGEST oznacza, że producent walidacji nie wykonał albo
    ktoś publikuje do tematu z pominięciem publishera.
    """

    SOURCE = "source"
    INGEST = "ingest"
    BATCH = "batch"


class QuarantineReason(StrEnum):
    """Powód odrzucenia rekordu, w formie nadającej się do grupowania w raporcie.

    To jest warstwa nad `ValidationError` z pydantic. Surowy błąd walidacji ma
    typ techniczny (`int_type`, `greater_than`, `extra_forbidden`), który jest
    precyzyjny, ale bezużyteczny w raporcie dla zespołu danych. Ten enum tłumaczy
    go na kategorię, którą da się policzyć i pokazać na wykresie.
    """

    MALFORMED_PAYLOAD = "malformed_payload"
    """Nie udało się odczytać wiadomości - uszkodzony JSON, zły kodek."""

    MISSING_FIELD = "missing_field"
    """Brak pola wymaganego przez kontrakt."""

    UNEXPECTED_FIELD = "unexpected_field"
    """Pole spoza kontraktu - producent wysyła coś, o czym kontrakt nie wie."""

    TYPE_MISMATCH = "type_mismatch"
    """Typ niezgodny z kontraktem, np. liczba przysłana jako łańcuch znaków."""

    NON_POSITIVE_AMOUNT = "non_positive_amount"
    """Cena, ilość lub wartość mniejsza lub równa zeru."""

    OUT_OF_RANGE = "out_of_range"
    """Wartość mieści się w typie, ale poza granicami kontraktu - ilość ponad limit,
    zbyt długa nazwa, identyfikator niezgodny ze wzorcem."""

    UNSUPPORTED_CURRENCY = "unsupported_currency"
    """Waluta spoza dozwolonego zbioru."""

    FUTURE_TIMESTAMP = "future_timestamp"
    """Znacznik czasu z przyszłości - zwykle zepsuty zegar albo podmieniona strefa."""

    VALUE_MISMATCH = "value_mismatch"
    """Wartość transakcji nie zgadza się z sumą pozycji."""

    DUPLICATE_TRANSACTION = "duplicate_transaction"
    """Identyfikator transakcji już wystąpił - błąd biznesowy, nie błąd schematu."""

    SCHEMA_VIOLATION = "schema_violation"
    """Naruszenie kontraktu, którego nie da się przypisać do kategorii powyżej."""
