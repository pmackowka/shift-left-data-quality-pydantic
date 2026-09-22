"""Modele zdarzenia zakupu - właściwy kontrakt danych.

Zdarzenie zakupu jest tu celowo wzorowane na zdarzeniu `purchase` z GA4: ma wartość
transakcji, walutę, listę pozycji, koszt wysyłki i rabat. Dzięki temu reguły
walidacji odpowiadają problemom, które w analityce ecommerce występują naprawdę,
a nie wymyślonym na potrzeby przykładu.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from dq_contracts.enums import Channel, Currency
from dq_contracts.errors import ERROR_FUTURE_TIMESTAMP, ERROR_VALUE_MISMATCH
from dq_contracts.version import CONTRACT_VERSION

# Tolerancja na rozjazd zegarów między producentem a systemem walidującym.
#
# Bez tolerancji reguła „znacznik czasu nie może być z przyszłości" odrzucałaby
# poprawne zdarzenia wysłane z maszyny, której zegar spieszy się o dwie sekundy.
# Pięć minut to wartość dobrana pod NTP: zegar zsynchronizowany mieści się w niej
# z ogromnym zapasem, a zegar przestawiony o strefę czasową (najczęstsza realna
# przyczyna) rozjeżdża się o całe godziny i zostanie złapany.
MAX_CLOCK_SKEW = timedelta(minutes=5)

# --- typy ograniczone ------------------------------------------------------
#
# `Annotated[T, Field(...)]` to sposób na nazwanie ograniczenia raz i użycie go
# w wielu miejscach. Alternatywa - powtarzanie `Field(gt=0, decimal_places=2)`
# przy każdym polu z kwotą - prowadzi do sytuacji, w której jedno pole ma
# ograniczenie, a drugie zostało dodane później i nie ma. Nazwany alias sprawia,
# że „kwota pieniężna" jest jednym pojęciem w całym kontrakcie.

TransactionId = Annotated[
    str,
    Field(
        min_length=6,
        max_length=64,
        # Identyfikator transakcji trafia do klucza klastrującego w BigQuery i do
        # kluczy deduplikacji, więc nie może zawierać białych znaków ani znaków,
        # które wymagałyby cytowania w SQL.
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]

CustomerId = Annotated[str, Field(min_length=1, max_length=64)]

Money = Annotated[
    Decimal,
    Field(
        gt=0,
        # max_digits i decimal_places to nie kosmetyka: odpowiadają wprost typowi
        # NUMERIC w BigQuery. Kwota z czterema miejscami po przecinku przeszłaby
        # walidację bez tego ograniczenia i zostałaby po cichu zaokrąglona przy
        # zapisie - czyli raport pokazałby inną liczbę niż źródło.
        max_digits=12,
        decimal_places=2,
    ),
]

# Wysyłka i rabat mogą wynosić zero, cena pozycji nie może. Stąd osobny alias
# zamiast jednego typu z `ge=0`, który rozluźniłby regułę dla cen.
NonNegativeMoney = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]

Quantity = Annotated[
    int,
    Field(
        gt=0,
        # Górna granica to nie teoria: literówka w ilości (wpisane 100000 zamiast 1)
        # potrafi wywrócić raport sprzedaży skuteczniej niż brakujące zdarzenie.
        le=10_000,
    ),
]


class LineItem(BaseModel):
    """Pojedyncza pozycja na paragonie."""

    # Konfiguracja jest tu powtórzona świadomie, a nie dziedziczona z modelu
    # nadrzędnego: pydantic NIE przekazuje `model_config` do modeli zagnieżdżonych.
    # Gdyby `LineItem` nie miał własnego `strict=True`, pozycje wewnątrz zdarzenia
    # walidowałyby się łagodniej niż samo zdarzenie - i to jest dokładnie ten rodzaj
    # dziury, którego nikt nie zauważa, dopóki nie zacznie szukać powodu rozjazdu.
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    item_id: Annotated[str, Field(min_length=1, max_length=64)]
    item_name: Annotated[str, Field(min_length=1, max_length=200)]
    price: Money
    quantity: Quantity

    @property
    def line_total(self) -> Decimal:
        """Wartość pozycji: cena jednostkowa razy ilość.

        Property, nie pole modelu - wartość wyliczalna nie ma prawa przyjechać
        z zewnątrz. Gdyby była polem, producent mógłby przysłać `line_total`
        niezgodny z `price * quantity` i kontrakt musiałby rozstrzygać, któremu
        z dwóch źródeł wierzyć.
        """
        return self.price * self.quantity


class TrafficSource(BaseModel):
    """Źródło ruchu, które doprowadziło do zakupu."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    source: Annotated[str, Field(min_length=1, max_length=100)]
    medium: Channel
    # `None` znaczy tu „ruch bez kampanii", a nie „nie wiemy". To rozróżnienie ma
    # znaczenie w atrybucji: brak kampanii przy medium `organic` jest poprawny,
    # przy medium `cpc` jest podejrzany.
    campaign: Annotated[str | None, Field(max_length=100)] = None


class PurchaseEvent(BaseModel):
    """Zdarzenie zakupu - centralny model kontraktu.

    Reguły egzekwowane przez ten model:

    1. `value` musi się zgadzać z sumą pozycji powiększoną o wysyłkę i pomniejszoną
       o rabat (walidator spójności między polami).
    2. `event_timestamp` nie może pochodzić z przyszłości poza tolerancją zegara.
    3. Ceny i ilości muszą być dodatnie (ograniczenia typów).
    4. Waluta musi należeć do zbioru `Currency` (enum).
    5. Brak pola wymaganego lub pole nadmiarowe kończy się odrzuceniem
       (`extra="forbid"`).
    6. Typy muszą się zgadzać co do joty (`strict=True`).

    Reguła siódma - duplikat identyfikatora transakcji - świadomie NIE jest tutaj.
    Wymaga wiedzy o innych zdarzeniach, więc mieszka w `dq_contracts.dedup`.
    """

    model_config = ConfigDict(
        # strict=True wyłącza koercję typów. Domyślnie pydantic przyjąłby `"3"`
        # jako `int` i `1.5` jako `Decimal`, po cichu naprawiając dane wejściowe.
        # Dla danych finansowych to zła usługa: koercja ukrywa fakt, że producent
        # wysyła zły typ, a im dłużej to trwa, tym trudniej się z tego wycofać.
        #
        # Uwaga praktyczna, którą łatwo przeoczyć: `strict` znaczy co innego przy
        # walidacji obiektu Pythona, a co innego przy walidacji JSON-a. JSON nie ma
        # typu Decimal ani datetime, więc w `model_validate_json` łańcuch "19.99"
        # jest poprawną wartością `Decimal`, a "2026-01-01T00:00:00Z" poprawną datą.
        # Łańcuch "3" dla pola `int` zostanie odrzucony w obu trybach, bo JSON
        # potrafi wyrazić liczbę całkowitą.
        strict=True,
        # extra="forbid" zamienia nadmiarowe pole z ciszy w błąd. Domyślne
        # zachowanie pydantic (ignore) oznacza, że producent może dodać pole,
        # nikt tego nie zauważy, a dane po prostu nie dojdą do hurtowni.
        extra="forbid",
        # Zdarzenie jest faktem historycznym - raz zwalidowane, nie zmienia się.
        # frozen=True daje to gwarancją typów i sprawia, że model jest hashowalny.
        frozen=True,
    )

    event_id: UUID
    transaction_id: TransactionId
    customer_id: CustomerId
    currency: Currency
    value: Money
    shipping: NonNegativeMoney = Decimal("0.00")
    discount: NonNegativeMoney = Decimal("0.00")
    # min_length=1: zakup bez pozycji nie jest zakupem. max_length chroni przed
    # pojedynczą wiadomością, która wysadziłaby limit rozmiaru Pub/Sub (10 MB).
    items: Annotated[list[LineItem], Field(min_length=1, max_length=200)]
    # AwareDatetime zamiast datetime: data bez strefy czasowej jest w danych
    # analitycznych źródłem najdroższych pomyłek. „22:30" bez strefy to inny dzień
    # w raporcie dziennym, zależnie od tego, kto go czyta.
    event_timestamp: AwareDatetime
    traffic_source: TrafficSource
    # Wersja kontraktu jedzie razem z danymi, więc w hurtowni widać, które reguły
    # obowiązywały przy przyjęciu rekordu. Bez tego pola pytanie „czy dane sprzed
    # miesiąca przeszły przez tę samą walidację" nie ma odpowiedzi.
    contract_version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")] = CONTRACT_VERSION

    @field_validator("event_timestamp")
    @classmethod
    def reject_future_timestamps(cls, value: datetime) -> datetime:
        """Odrzuca znaczniki czasu z przyszłości i normalizuje je do UTC.

        `field_validator` działa na jednym polu i nie widzi pozostałych - to jest
        cała różnica wobec `model_validator` niżej. Tutaj wystarczy, bo do oceny
        „czy to przyszłość" potrzebny jest wyłącznie ten jeden znacznik.
        """
        now = datetime.now(UTC)
        if value > now + MAX_CLOCK_SKEW:
            # PydanticCustomError zamiast ValueError, bo pierwszy argument staje się
            # stabilnym kodem błędu w `ValidationError.errors()[i]["type"]`.
            # Trzeci argument to kontekst - trafia do komunikatu i do `ctx`, więc
            # raport w kwarantannie pokazuje konkretne wartości, a nie ogólnik.
            raise PydanticCustomError(
                ERROR_FUTURE_TIMESTAMP,
                "Event timestamp {ts} is more than {skew} ahead of now ({now})",
                {"ts": value.isoformat(), "skew": str(MAX_CLOCK_SKEW), "now": now.isoformat()},
            )
        # Normalizacja do UTC dzieje się po sprawdzeniu, nie przed. Kolejność jest
        # obojętna dla wyniku porównania (obie daty są świadome strefy), ale ważna
        # dla komunikatu błędu: użytkownik widzi w nim znacznik w takiej postaci,
        # w jakiej go wysłał.
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def value_must_match_line_items(self) -> Self:
        """Sprawdza spójność wartości transakcji z pozycjami.

        To jest walidacja cross-field: wymaga jednocześnie `items`, `shipping`,
        `discount` i `value`. `mode="after"` oznacza, że uruchamia się dopiero, gdy
        wszystkie pola przeszły własną walidację - więc tutaj `items` na pewno jest
        listą `LineItem`, a nie czymkolwiek, co przyszło na wejściu.

        Wybór `mode="after"` zamiast `mode="before"` jest tu istotny: walidator
        „before" dostałby surowy słownik i musiałby sam radzić sobie z tym, że cena
        może być łańcuchem znaków albo że `items` w ogóle nie ma.
        """
        items_total = sum((item.line_total for item in self.items), start=Decimal("0"))
        expected = items_total + self.shipping - self.discount

        # Porównanie dokładne, bez tolerancji - i to jest powód, dla którego kwoty
        # są typu Decimal, a nie float. Na floatach 0.1 + 0.2 != 0.3, więc trzeba
        # byłoby wprowadzić próg tolerancji, a próg tolerancji na pieniądzach to
        # z definicji zgoda na cichy błąd o nieznanej wielkości.
        if expected != self.value:
            raise PydanticCustomError(
                ERROR_VALUE_MISMATCH,
                "Transaction value {value} does not match line items "
                "({items_total} + shipping {shipping} - discount {discount} = {expected})",
                {
                    "value": str(self.value),
                    "items_total": str(items_total),
                    "shipping": str(self.shipping),
                    "discount": str(self.discount),
                    "expected": str(expected),
                },
            )
        return self
