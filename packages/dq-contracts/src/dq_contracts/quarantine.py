"""Zamiana błędu walidacji na rekord kwarantanny.

Kwarantanna odpowiada na trzy pytania naraz: co przyszło, dlaczego zostało odrzucone
i gdzie to wyszło. Surowy `ValidationError` odpowiada tylko na drugie, i to w formie
technicznej. Ten moduł tłumaczy go na rekord, który da się zapisać do BigQuery
i pogrupować w raporcie.

Zasada, która rządzi całym modułem: rekord odrzucony nie znika. Pipeline, który przy
złym wejściu wypisuje ostrzeżenie do logu i leci dalej, po tygodniu ma dziurę w danych
o nieznanym rozmiarze i bez możliwości odtworzenia.
"""

import json
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dq_contracts.enums import PipelineStage, QuarantineReason
from dq_contracts.errors import ERROR_FUTURE_TIMESTAMP, ERROR_VALUE_MISMATCH, BusinessRuleError
from dq_contracts.version import CONTRACT_VERSION

# Mapowanie technicznego typu błędu pydantic na kategorię raportową.
#
# Mapujemy po `type`, nigdy po treści komunikatu - `type` jest częścią publicznego
# API pydantic i nie zmienia się przy zmianie tekstu błędu ani przy tłumaczeniu.
_ERROR_TYPE_TO_REASON: dict[str, QuarantineReason] = {
    "missing": QuarantineReason.MISSING_FIELD,
    "extra_forbidden": QuarantineReason.UNEXPECTED_FIELD,
    # Naruszenie dolnej granicy dla kwot i ilości.
    "greater_than": QuarantineReason.NON_POSITIVE_AMOUNT,
    "greater_than_equal": QuarantineReason.NON_POSITIVE_AMOUNT,
    # Naruszenie górnej granicy, długości albo wzorca - wartość jest poprawnego typu,
    # ale poza tym, co kontrakt dopuszcza.
    "less_than": QuarantineReason.OUT_OF_RANGE,
    "less_than_equal": QuarantineReason.OUT_OF_RANGE,
    "too_long": QuarantineReason.OUT_OF_RANGE,
    "too_short": QuarantineReason.OUT_OF_RANGE,
    "string_too_long": QuarantineReason.OUT_OF_RANGE,
    "string_too_short": QuarantineReason.OUT_OF_RANGE,
    "string_pattern_mismatch": QuarantineReason.OUT_OF_RANGE,
    "decimal_max_digits": QuarantineReason.OUT_OF_RANGE,
    "decimal_max_places": QuarantineReason.OUT_OF_RANGE,
    # Kody z walidatorów własnych - stąd bierze się wartość trzymania ich w stałych.
    ERROR_FUTURE_TIMESTAMP: QuarantineReason.FUTURE_TIMESTAMP,
    ERROR_VALUE_MISMATCH: QuarantineReason.VALUE_MISMATCH,
}

# Kolejność ważności powodów. Rekord potrafi złamać kilka reguł naraz, a raport
# potrzebuje jednej etykiety na wiersz.
#
# Kolejność nie jest dowolna: najwyżej stoją błędy strukturalne, bo one świadczą
# o rozjeździe kontraktu między producentem a konsumentem i dotyczą zwykle całego
# strumienia. Niżej są błędy wartości - te dotyczą pojedynczego rekordu. Gdy zdarzenie
# ma naraz pole nadmiarowe i złą sumę pozycji, ważniejsza jest informacja, że producent
# wysyła inny schemat niż ten, którego się spodziewamy.
_REASON_PRIORITY: tuple[QuarantineReason, ...] = (
    QuarantineReason.MALFORMED_PAYLOAD,
    QuarantineReason.UNEXPECTED_FIELD,
    QuarantineReason.MISSING_FIELD,
    QuarantineReason.TYPE_MISMATCH,
    QuarantineReason.UNSUPPORTED_CURRENCY,
    QuarantineReason.NON_POSITIVE_AMOUNT,
    QuarantineReason.OUT_OF_RANGE,
    QuarantineReason.FUTURE_TIMESTAMP,
    QuarantineReason.VALUE_MISMATCH,
    QuarantineReason.DUPLICATE_TRANSACTION,
    QuarantineReason.SCHEMA_VIOLATION,
)


def reason_for(error_type: str, field_path: str) -> QuarantineReason:
    """Tłumaczy typ błędu pydantic na kategorię kwarantanny.

    Kolejność sprawdzania ma znaczenie. Najpierw przypadek szczególny (waluta), potem
    tabela dokładnych trafień, na końcu heurystyka po kształcie nazwy. Odwrotna
    kolejność sprawiłaby, że `enum` na polu `currency` wpadłby w regułę ogólną.
    """
    if error_type == "enum":
        # `enum` to ten sam kod dla waluty i dla kanału ruchu, więc dopiero ścieżka
        # pola mówi, o co chodzi. Raport z powodem „zła waluta" jest użyteczny;
        # raport z powodem „zła wartość enum" wymaga drugiego zapytania.
        return (
            QuarantineReason.UNSUPPORTED_CURRENCY
            if "currency" in field_path
            else QuarantineReason.OUT_OF_RANGE
        )

    if error_type in _ERROR_TYPE_TO_REASON:
        return _ERROR_TYPE_TO_REASON[error_type]

    # Heurystyka zamiast wyliczania wszystkich kodów typów. Pydantic ma osobny kod dla
    # każdego typu (`int_type`, `string_type`, `uuid_type`, `list_type`, `decimal_parsing`,
    # `timezone_aware`...) i lista tych kodów rośnie z wersjami biblioteki. Dopisywanie
    # ich ręcznie kończy się tym, że nowy kod po cichu wpada do worka „inne".
    if (
        error_type.endswith(("_type", "_parsing"))
        or error_type == "is_instance_of"
        or error_type == "timezone_aware"
    ):
        return QuarantineReason.TYPE_MISMATCH

    return QuarantineReason.SCHEMA_VIOLATION


class ValidationIssue(BaseModel):
    """Pojedyncze naruszenie kontraktu, w formie nadającej się do zapisu w hurtowni."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_path: Annotated[str, Field(max_length=200)]
    """Ścieżka do pola, np. `items.0.price`. Pusta dla błędów całego modelu."""

    error_type: Annotated[str, Field(max_length=100)]
    """Techniczny kod błędu z pydantic - stabilny identyfikator do grupowania."""

    message: Annotated[str, Field(max_length=1000)]
    input_value: Annotated[str | None, Field(max_length=1000)] = None
    """Wartość, która wywołała błąd, zapisana jako tekst.

    Jako tekst, bo kolumna w BigQuery musi mieć jeden typ, a tu może przyjechać
    cokolwiek - liczba, słownik, `None`. Obcięta, bo pojedyncza pozycja z długim
    opisem nie ma prawa wysadzić zapisu do kwarantanny.
    """


class RejectedRecord(BaseModel):
    """Rekord kwarantanny - jeden odrzucony payload z pełnym kontekstem.

    Model NIE jest `strict`, w odróżnieniu od `PurchaseEvent`, i to jest celowe:
    rekord kwarantanny budujemy w kodzie z danych, które już wiemy, że są zepsute.
    Surowość w tym miejscu oznaczałaby, że zapis do kwarantanny może się nie udać
    z powodu tych samych danych, które do kwarantanny trafiają - czyli że pipeline
    gubi rekord dokładnie wtedy, gdy najbardziej zależy nam na jego zachowaniu.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rejected_at: datetime
    stage: PipelineStage
    reason: QuarantineReason
    contract_version: str
    transaction_id: str | None = None
    """Wyciągnięty z payloadu, jeśli dało się go odczytać - inaczej `None`.

    Bez tego pola ścieżka od raportu („mamy 40 odrzuceń z powodu złej sumy") do
    konkretnej transakcji prowadzi przez parsowanie surowego JSON-a w SQL.
    """

    raw_payload: str
    """Oryginalna treść, dokładnie taka, jaka przyszła.

    Przechowujemy ją, bo kwarantanna ma umożliwiać naprawę i ponowne wgranie.
    Rekord odrzucony bez oryginału nadaje się tylko do policzenia.
    """

    issues: list[ValidationIssue]


def _extract_transaction_id(raw_payload: str) -> str | None:
    """Próbuje wyciągnąć identyfikator transakcji z surowego payloadu.

    Operacja z definicji zawodna - payload mógł być uszkodzony. Dlatego każdy błąd
    kończy się `None`, a nie wyjątkiem: to funkcja pomocnicza w ścieżce obsługi błędu
    i nie wolno jej tej ścieżki wywrócić.
    """
    try:
        parsed: Any = json.loads(raw_payload)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if isinstance(parsed, dict):
        candidate = parsed.get("transaction_id")
        if isinstance(candidate, str):
            return candidate
    return None


def _field_path(location: tuple[int | str, ...]) -> str:
    """Zamienia krotkę `loc` z pydantic na czytelną ścieżkę, np. `items.0.price`."""
    return ".".join(str(part) for part in location)


def to_rejected_record(
    raw_payload: str,
    error: ValidationError,
    stage: PipelineStage,
) -> RejectedRecord:
    """Buduje rekord kwarantanny z błędu walidacji pydantic."""
    issues = [
        ValidationIssue(
            field_path=_field_path(item["loc"]),
            error_type=item["type"],
            message=item["msg"],
            # `input` bywa dowolnym obiektem, łącznie z takim, którego repr jest
            # ogromny - stąd twarde obcięcie do limitu pola.
            input_value=str(item.get("input"))[:1000],
        )
        for item in error.errors()
    ]
    reasons = {reason_for(issue.error_type, issue.field_path) for issue in issues}
    return RejectedRecord(
        rejected_at=datetime.now(UTC),
        stage=stage,
        reason=_highest_priority(reasons),
        contract_version=CONTRACT_VERSION,
        transaction_id=_extract_transaction_id(raw_payload),
        raw_payload=raw_payload,
        issues=issues,
    )


def to_rejected_record_from_business_error(
    raw_payload: str,
    error: BusinessRuleError,
    stage: PipelineStage,
) -> RejectedRecord:
    """Buduje rekord kwarantanny z błędu biznesowego.

    Osobna funkcja, bo błąd biznesowy nie ma listy `errors()` ani ścieżek pól -
    ma jedną przyczynę i jeden komunikat. Wymuszenie wspólnej sygnatury dla obu
    przypadków oznaczałoby udawanie struktury, której tu nie ma.
    """
    return RejectedRecord(
        rejected_at=datetime.now(UTC),
        stage=stage,
        reason=error.reason,
        contract_version=CONTRACT_VERSION,
        transaction_id=_extract_transaction_id(raw_payload),
        raw_payload=raw_payload,
        issues=[
            ValidationIssue(
                field_path="",
                error_type=type(error).__name__,
                message=str(error),
            )
        ],
    )


def to_rejected_record_from_malformed(raw_payload: str, stage: PipelineStage) -> RejectedRecord:
    """Buduje rekord kwarantanny dla payloadu, którego nie dało się odczytać.

    Ten przypadek jest inny od pozostałych: nie ma tu żadnej wiedzy o zawartości,
    bo zawartości nie udało się sparsować. Zapisujemy surowy tekst i tyle - to
    jedyny materiał dowodowy, jaki został.
    """
    return RejectedRecord(
        rejected_at=datetime.now(UTC),
        stage=stage,
        reason=QuarantineReason.MALFORMED_PAYLOAD,
        contract_version=CONTRACT_VERSION,
        transaction_id=None,
        raw_payload=raw_payload,
        issues=[
            ValidationIssue(
                field_path="",
                error_type="malformed_payload",
                message="Payload could not be parsed as JSON",
            )
        ],
    )


def _highest_priority(reasons: set[QuarantineReason]) -> QuarantineReason:
    """Wybiera jeden powód spośród kilku, zgodnie z ustaloną ważnością.

    Deterministycznie - to nie jest szczegół. Gdyby wybór zależał od kolejności
    błędów zwróconych przez pydantic, ten sam rekord mógłby trafić do raportu raz
    z jedną etykietą, raz z inną, a zliczenia przestałyby się sumować.
    """
    for reason in _REASON_PRIORITY:
        if reason in reasons:
            return reason
    return QuarantineReason.SCHEMA_VIOLATION
