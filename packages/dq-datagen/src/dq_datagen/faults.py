"""Katalog błędów wstrzykiwanych do zdarzeń - jeden błąd na jedną regułę kontraktu.

Każdy wpis katalogu łączy trzy rzeczy: rodzaj błędu, funkcję, która go wstrzykuje,
i powód kwarantanny, którym kontrakt MA na ten błąd odpowiedzieć. Trzecia część jest
najważniejsza - zamienia generator z „producenta śmieci" w wyrocznię testową. Skoro
generator wie, co zepsuł, każdy rekord ma znaną odpowiedź wzorcową, a raport
z pipeline'u da się porównać z tym, co faktycznie zostało wstrzyknięte.

Błędy wstrzykujemy do słownika w postaci JSON-owej, a nie do modelu. Model z definicji
nie pozwoli zbudować obiektu łamiącego kontrakt (walidacja w konstruktorze). Można by to
obejść przez `model_construct`, ale część błędów - pole nadmiarowe, brak pola, łańcuch
w miejscu liczby - opisuje kształt payloadu, którego obiekt modelu nie wyraża wiernie.
Słownik JSON-owy to dokładnie to, co przyjeżdża z Pub/Sub - psujemy więc dane tam,
gdzie psują się w rzeczywistości.

Każdy wstrzykiwacz zmienia JEDNĄ rzecz w poprawnym payloadzie. Rekord łamiący dwie
reguły naraz dostałby w kwarantannie jeden powód (ten o wyższym priorytecie), więc
drugi błąd byłby niewidoczny, a zgodność z odpowiedzią wzorcową - przypadkowa.
"""

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from dq_contracts import PurchaseEvent, QuarantineReason

Payload = dict[str, Any]


class FaultKind(StrEnum):
    """Rodzaj wstrzykiwanego błędu.

    Wartości pokrywają się z wartościami `QuarantineReason`, którymi kontrakt na nie
    odpowiada. To ułatwia czytanie raportu (ta sama etykieta po obu stronach), ale
    NIE jest mechanizmem mapowania - mapowanie jest jawne, w `FAULT_CATALOG`.
    """

    VALUE_MISMATCH = "value_mismatch"
    FUTURE_TIMESTAMP = "future_timestamp"
    NON_POSITIVE_AMOUNT = "non_positive_amount"
    UNSUPPORTED_CURRENCY = "unsupported_currency"
    DUPLICATE_TRANSACTION = "duplicate_transaction"
    MISSING_FIELD = "missing_field"
    UNEXPECTED_FIELD = "unexpected_field"
    TYPE_MISMATCH = "type_mismatch"


@dataclass(frozen=True, slots=True)
class FaultContext:
    """To, czego wstrzykiwacz potrzebuje poza samym payloadem.

    `seen_transaction_ids` to identyfikatory POPRAWNYCH rekordów wygenerowanych
    wcześniej w tym przebiegu. Tylko poprawnych, bo duplikat ma powtarzać transakcję,
    którą pipeline przyjął - powtórzenie identyfikatora z rekordu odrzuconego nie
    jest duplikatem, bo rejestr transakcji nigdy go nie zapamiętał.
    """

    rng: random.Random
    reference_time: datetime
    seen_transaction_ids: Sequence[str] = ()


Injector = Callable[[Payload, FaultContext], None]


@dataclass(frozen=True, slots=True)
class FaultSpec:
    """Wpis katalogu: jak zepsuć rekord i jakiej odpowiedzi oczekiwać od kontraktu."""

    inject: Injector
    expected_reason: QuarantineReason
    description: str


# --- wstrzykiwacze ----------------------------------------------------------
#
# Konwencja: każda funkcja modyfikuje payload w miejscu i nic nie zwraca. Payload
# jest zawsze świeżą kopią (powstaje z `json.loads`), więc mutacja nie ma efektów
# ubocznych poza rekordem, który właśnie psujemy.


def _value_mismatch(payload: Payload, ctx: FaultContext) -> None:
    # Delta od jednego grosza do dziesięciu złotych. Jeden grosz to najbardziej
    # podstępny przypadek - błąd zaokrąglenia po stronie producenta - i jedyny,
    # który wykryje tylko porównanie dokładne na Decimal.
    delta = ctx.rng.choice((Decimal("0.01"), Decimal("1.00"), Decimal("10.00")))
    payload["value"] = str(Decimal(payload["value"]) + delta)


def _future_timestamp(payload: Payload, ctx: FaultContext) -> None:
    # Przesunięcie o 1-14 godzin odwzorowuje najczęstszą realną przyczynę: czas
    # lokalny wysłany jako UTC albo zła strefa w konfiguracji serwera. Minimum to
    # godzina, czyli z dużym zapasem ponad tolerancję zegara (`MAX_CLOCK_SKEW`).
    #
    # Pułapka, której nie da się usunąć, tylko nazwać: „przyszłość" jest względna.
    # Rekord oznaczony tu jako błędny przestaje nim być, gdy plik zostanie
    # zwalidowany później niż przesunięcie - dlatego czas odniesienia jest
    # parametrem generatora, a nie ukrytym `now()`.
    shift = timedelta(hours=ctx.rng.randint(1, 14), minutes=ctx.rng.randint(0, 59))
    payload["event_timestamp"] = (ctx.reference_time + shift).isoformat()


def _non_positive_amount(payload: Payload, ctx: FaultContext) -> None:
    # Zero albo wartość ujemna, w cenie albo w ilości wybranej pozycji. Wartość
    # transakcji zostaje stara, więc suma też się nie zgadza - ale walidator spójności
    # (`mode="after"`) nie uruchomi się, skoro pole nie przeszło własnej walidacji.
    # Kontrakt zgłosi więc wyłącznie błąd kwoty, nie dwa błędy naraz.
    item = ctx.rng.choice(payload["items"])
    if ctx.rng.random() < 0.5:
        item["price"] = ctx.rng.choice(("0.00", f"-{item['price']}"))
    else:
        item["quantity"] = ctx.rng.choice((0, -item["quantity"]))


def _unsupported_currency(payload: Payload, ctx: FaultContext) -> None:
    # „pln" małymi literami jest tu celowo: enum porównuje wartości dokładnie, więc
    # różnica wielkości liter to inna waluta. CHF jest prawdziwą walutą, ale spoza
    # zbioru kontraktu - pokazuje, że reguła dotyczy umowy, a nie normy ISO 4217.
    payload["currency"] = ctx.rng.choice(("pln", "CHF", "BTC", "PLZ"))


def _duplicate_transaction(payload: Payload, ctx: FaultContext) -> None:
    # Nowe `event_id`, stary `transaction_id` - wzorzec podwójnego wysłania tej samej
    # transakcji przez producenta (np. ponowienie po timeoucie). Rekord jest poprawny
    # względem schematu i dopiero rejestr transakcji, który pamięta poprzednie
    # rekordy, potrafi go odrzucić.
    if not ctx.seen_transaction_ids:
        msg = "duplicate_transaction needs at least one earlier valid record to repeat"
        raise ValueError(msg)
    payload["transaction_id"] = ctx.rng.choice(ctx.seen_transaction_ids)


# Pola wymagane czytamy z kontraktu, a nie wpisujemy ręcznie. Gdy kontrakt dostanie
# nowe pole wymagane, generator zacznie je usuwać bez żadnej zmiany w tym pliku.
# Sortowanie daje stałą kolejność niezależną od implementacji `model_fields`, więc
# to samo ziarno zawsze wybiera to samo pole.
_REQUIRED_FIELDS: Final = tuple(
    sorted(name for name, field in PurchaseEvent.model_fields.items() if field.is_required())
)


def _missing_field(payload: Payload, ctx: FaultContext) -> None:
    del payload[ctx.rng.choice(_REQUIRED_FIELDS)]


# Nazwy pól, które producenci naprawdę dorzucają do zdarzeń bez uzgodnienia: kod
# rabatowy, identyfikator kliknięcia reklamy, user agent.
_EXTRA_FIELDS: Final = ("coupon_code", "gclid", "user_agent", "session_id")


def _unexpected_field(payload: Payload, ctx: FaultContext) -> None:
    # Pole nadmiarowe na poziomie zdarzenia albo wewnątrz pozycji. Wariant
    # zagnieżdżony sprawdza, że `extra="forbid"` działa też w `LineItem` - pydantic
    # nie dziedziczy konfiguracji do modeli zagnieżdżonych, więc to osobna gwarancja.
    target = payload if ctx.rng.random() < 0.5 else ctx.rng.choice(payload["items"])
    target[ctx.rng.choice(_EXTRA_FIELDS)] = "unexpected"


def _type_mismatch(payload: Payload, ctx: FaultContext) -> None:
    # Ilość jako łańcuch znaków: `"2"` zamiast `2`. W trybie lax pydantic przyjąłby to
    # bez słowa; strict odrzuca, bo JSON potrafi wyrazić liczbę całkowitą, więc
    # łańcuch oznacza, że producent wysyła zły typ. Kwot nie ruszamy - tekstowy
    # zapis Decimal jest w tym kontrakcie formą poprawną, nie błędem.
    item = ctx.rng.choice(payload["items"])
    item["quantity"] = str(item["quantity"])


# MappingProxyType czyni katalog niemutowalnym w runtime. `Final` pilnuje tylko
# przypisania nazwy (i tylko w mypy), a nie zawartości słownika - bez proxy dowolny
# moduł mógłby podmienić wpis i zmienić odpowiedź wzorcową dla całego przebiegu.
FAULT_CATALOG: Final[Mapping[FaultKind, FaultSpec]] = MappingProxyType(
    {
        FaultKind.VALUE_MISMATCH: FaultSpec(
            _value_mismatch,
            QuarantineReason.VALUE_MISMATCH,
            "transaction value differs from the line items total",
        ),
        FaultKind.FUTURE_TIMESTAMP: FaultSpec(
            _future_timestamp,
            QuarantineReason.FUTURE_TIMESTAMP,
            "event timestamp shifted 1-14 hours into the future",
        ),
        FaultKind.NON_POSITIVE_AMOUNT: FaultSpec(
            _non_positive_amount,
            QuarantineReason.NON_POSITIVE_AMOUNT,
            "zero or negative price or quantity in one line item",
        ),
        FaultKind.UNSUPPORTED_CURRENCY: FaultSpec(
            _unsupported_currency,
            QuarantineReason.UNSUPPORTED_CURRENCY,
            "currency outside the contract's allowed set",
        ),
        FaultKind.DUPLICATE_TRANSACTION: FaultSpec(
            _duplicate_transaction,
            QuarantineReason.DUPLICATE_TRANSACTION,
            "transaction id repeated from an earlier valid record",
        ),
        FaultKind.MISSING_FIELD: FaultSpec(
            _missing_field,
            QuarantineReason.MISSING_FIELD,
            "one required field removed",
        ),
        FaultKind.UNEXPECTED_FIELD: FaultSpec(
            _unexpected_field,
            QuarantineReason.UNEXPECTED_FIELD,
            "field unknown to the contract added to the event or a line item",
        ),
        FaultKind.TYPE_MISMATCH: FaultSpec(
            _type_mismatch,
            QuarantineReason.TYPE_MISMATCH,
            "quantity sent as a string instead of an integer",
        ),
    }
)


def inject_fault(kind: FaultKind, payload: Payload, ctx: FaultContext) -> None:
    """Psuje payload w miejscu zgodnie z wybranym rodzajem błędu."""
    FAULT_CATALOG[kind].inject(payload, ctx)
