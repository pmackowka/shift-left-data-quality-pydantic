"""Budowa poprawnych zdarzeń zakupu z losowych, ale wiarygodnych danych.

Poprawne zdarzenie powstaje przez konstruktor `PurchaseEvent`, czyli przechodzi pełną
walidację kontraktu. To świadomy koszt: generator, który składałby słowniki „na oko",
mógłby po cichu produkować rekordy niezgodne z kontraktem i wtedy każdy rekord
oznaczony jako poprawny byłby w rzeczywistości niewiadomą. Tutaj kontrakt sam
potwierdza, że rekord jest poprawny, zanim generator go wypuści.

Konstruktor dostaje natywne typy Pythona (`UUID`, `Decimal`, `Currency`), bo model ma
`strict=True`, a w trybie strict walidacja obiektów Pythona nie zamienia łańcucha
`"PLN"` na `Currency.PLN`. Ścieżka tekstowa (JSON) należy do pipeline'u, nie do
generatora.

Cała losowość idzie przez przekazany `random.Random`. Moduł nie dotyka globalnego
stanu `random` ani zegara systemowego - stąd powtarzalność: to samo ziarno i ten sam
czas odniesienia dają bajt w bajt ten sam rekord.
"""

import random
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Final
from uuid import UUID

from dq_contracts import Channel, Currency, LineItem, PurchaseEvent, TrafficSource

_CENT: Final = Decimal("0.01")

# Mały, stały katalog produktów zamiast losowych nazw i cen. Ceny z dwoma miejscami
# po przecinku i końcówkami .99 / .49 wyglądają jak w prawdziwym sklepie, a rekord
# w kwarantannie da się przeczytać bez dekodowania przypadkowych łańcuchów.
_PRODUCTS: Final[tuple[tuple[str, str, Decimal], ...]] = (
    ("SKU-1001", "Bawelniana koszulka", Decimal("49.99")),
    ("SKU-1002", "Skarpetki 3-pak", Decimal("19.99")),
    ("SKU-1003", "Bluza z kapturem", Decimal("189.00")),
    ("SKU-1004", "Jeansy slim", Decimal("229.90")),
    ("SKU-1005", "Czapka zimowa", Decimal("59.49")),
    ("SKU-1006", "Kurtka przejsciowa", Decimal("449.00")),
    ("SKU-1007", "Pasek skorzany", Decimal("89.99")),
    ("SKU-1008", "Torba na ramie", Decimal("259.00")),
)

# Kombinacje source / medium / campaign wzorowane na raportach GA4. Kampania jest
# `None` tam, gdzie w realnym ruchu jej nie ma (organic, direct, referral).
_TRAFFIC: Final[tuple[tuple[str, Channel, str | None], ...]] = (
    ("google", Channel.CPC, "brand-search"),
    ("google", Channel.ORGANIC, None),
    ("newsletter", Channel.EMAIL, "weekly-digest"),
    ("facebook", Channel.SOCIAL, "retargeting"),
    ("(direct)", Channel.DIRECT, None),
    ("partner-blog", Channel.AFFILIATE, "affiliate-q4"),
    ("wp.pl", Channel.REFERRAL, None),
)

# Rozkład walut przypomina polski sklep z niewielką sprzedażą zagraniczną. Wagi nie
# mają wpływu na walidację - są po to, żeby raport z przebiegu wyglądał jak z produkcji.
_CURRENCIES: Final = (Currency.PLN, Currency.EUR, Currency.CZK, Currency.USD, Currency.GBP)
_CURRENCY_WEIGHTS: Final = (80, 10, 5, 3, 2)

_SHIPPING_OPTIONS: Final = (Decimal("0.00"), Decimal("9.99"), Decimal("14.99"))
_DISCOUNT_RATES: Final = (Decimal("0.05"), Decimal("0.10"), Decimal("0.15"))

# Okno, z którego losujemy znacznik czasu poprawnego zdarzenia: ostatni tydzień przed
# czasem odniesienia, ale nie bliżej niż minutę. Margines minuty oddziela poprawne
# zdarzenia od granicy reguły „nie z przyszłości", więc żaden poprawny rekord nie
# balansuje na krawędzi tolerancji zegara.
_EVENT_WINDOW: Final = timedelta(days=7)
_MIN_EVENT_AGE: Final = timedelta(minutes=1)


def random_uuid(rng: random.Random) -> UUID:
    """UUID w wersji 4 z podanego generatora liczb losowych.

    `uuid.uuid4()` czyta z `os.urandom`, więc zrywałby powtarzalność. UUID z 128 bitów
    `rng` ma ten sam format, ale zależy wyłącznie od ziarna.
    """
    return UUID(int=rng.getrandbits(128), version=4)


def _line_items(rng: random.Random) -> list[LineItem]:
    # `sample`, nie `choices`: ta sama pozycja nie powinna pojawić się w koszyku dwa
    # razy w osobnych liniach - sklep zwiększyłby ilość, a nie dodał drugą linię.
    products = rng.sample(_PRODUCTS, k=rng.randint(1, 4))
    return [
        LineItem(item_id=item_id, item_name=name, price=price, quantity=rng.randint(1, 3))
        for item_id, name, price in products
    ]


def build_valid_event(
    rng: random.Random,
    *,
    transaction_id: str,
    reference_time: datetime,
) -> PurchaseEvent:
    """Buduje jedno poprawne zdarzenie zakupu.

    `transaction_id` przychodzi z zewnątrz, bo unikalność w obrębie przebiegu to
    odpowiedzialność generatora, a nie tej funkcji. Losowy identyfikator dawałby
    niezerową szansę kolizji, czyli przypadkowy duplikat w zbiorze, który miał być
    czysty - i rozjazd między tym, co generator zadeklarował, a tym, co zobaczy raport.
    """
    items = _line_items(rng)
    items_total = sum((item.line_total for item in items), start=Decimal("0.00"))
    shipping = rng.choice(_SHIPPING_OPTIONS)

    # Rabat procentowy zaokrąglony W DÓŁ do grosza. Zaokrąglenie jest konieczne, bo
    # 15% z 49.99 ma więcej miejsc po przecinku, niż dopuszcza typ `Money`; kierunek
    # w dół gwarantuje, że rabat nigdy nie zrówna się z sumą pozycji, więc `value`
    # zostaje dodatnie.
    discount = Decimal("0.00")
    if rng.random() < 0.3:
        discount = (items_total * rng.choice(_DISCOUNT_RATES)).quantize(_CENT, ROUND_DOWN)

    source, medium, campaign = rng.choice(_TRAFFIC)
    age = _MIN_EVENT_AGE + timedelta(
        seconds=rng.randint(0, int((_EVENT_WINDOW - _MIN_EVENT_AGE).total_seconds()))
    )

    return PurchaseEvent(
        event_id=random_uuid(rng),
        transaction_id=transaction_id,
        customer_id=f"CUST-{rng.randint(1, 50_000):05d}",
        currency=rng.choices(_CURRENCIES, weights=_CURRENCY_WEIGHTS)[0],
        # Wartość liczona z pozycji, a nie losowana - poprawne zdarzenie z definicji
        # spełnia regułę spójności. Psuciem tej reguły zajmuje się katalog błędów.
        value=items_total + shipping - discount,
        shipping=shipping,
        discount=discount,
        items=items,
        event_timestamp=reference_time - age,
        traffic_source=TrafficSource(source=source, medium=medium, campaign=campaign),
    )
