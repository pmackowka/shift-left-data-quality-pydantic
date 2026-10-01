"""Generator strumienia zdarzeń: ile rekordów, ile zepsutych, jakimi błędami.

Generator nie losuje dla każdego rekordu z osobna, czy go zepsuć. Najpierw układa plan:
dokładnie `round(count * error_rate)` pozycji dostaje błąd, a rodzaje błędów rozkładają
się po tych pozycjach po równo. Losowanie rekord po rekordzie dawałoby odsetek błędów
„mniej więcej" zgodny z parametrem, a przy małym N część rodzajów błędów nie pojawiłaby
się wcale - raport z przebiegu nie miałby wtedy z czym się zgadzać.

Wynik jest strumieniem (`Iterator`), nie listą. Przy 100 tys. rekordów lista trzymałaby
w pamięci cały plik, zanim pierwsza linia trafi na dysk; iterator zapisuje na bieżąco.
"""

import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic_core import to_json

from dq_datagen.builder import build_valid_event
from dq_datagen.faults import FaultContext, FaultKind, inject_fault


class GeneratorConfig(BaseModel):
    """Parametry przebiegu generatora.

    Ten model jest celowo w trybie lax, odwrotnie niż kontrakt. Parametry przychodzą
    z linii poleceń jako tekst, więc koercja `"0.2"` na `0.2` i `"missing_field"` na
    `FaultKind.MISSING_FIELD` jest tu dokładnie tym, czego chcemy. Strict ma sens tam,
    gdzie zły typ oznacza błąd producenta danych; tutaj oznaczałby tylko, że parametr
    został wpisany w terminalu.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Górna granica chroni przed literówką (dodatkowe zero) zapełniającą dysk.
    count: Annotated[int, Field(gt=0, le=10_000_000)]
    error_rate: Annotated[float, Field(ge=0, le=1)] = 0.0
    # Ziarno nieujemne, bo trafia do `transaction_id`, a wzorzec identyfikatora wymaga,
    # żeby zaczynał się od litery lub cyfry - minus po prefiksie dałby `T--5-...`.
    seed: Annotated[int, Field(ge=0, le=2**32)] = 0
    faults: Annotated[frozenset[FaultKind], Field(min_length=1)] = frozenset(FaultKind)
    # Czas odniesienia jest parametrem, a nie ukrytym `now()` w środku generatora.
    # Bez tego to samo ziarno dawałoby inne znaczniki czasu przy każdym uruchomieniu,
    # więc „powtarzalny zbiór danych" byłby powtarzalny tylko do pełnej sekundy.
    reference_time: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class GeneratedRecord:
    """Jedna linia NDJSON razem z odpowiedzią wzorcową.

    `fault is None` znaczy: kontrakt musi ten rekord przyjąć. Każda inna wartość mówi,
    z jakim powodem rekord ma trafić do kwarantanny (`FAULT_CATALOG[fault]`).
    """

    index: int
    line: str
    fault: FaultKind | None


def _fault_plan(config: GeneratorConfig, rng: random.Random) -> dict[int, FaultKind]:
    """Rozpisuje, które pozycje dostaną jaki błąd.

    Duplikat potrzebuje oryginału, czyli wcześniejszego poprawnego rekordu. Gdy ten
    rodzaj błędu jest włączony, pozycja 0 jest zawsze poprawna i służy za kotwicę -
    stąd przy `error_rate=1.0` liczba błędnych rekordów wynosi `count - 1`, nie `count`.
    """
    anchor = 1 if FaultKind.DUPLICATE_TRANSACTION in config.faults else 0
    faulty = min(round(config.count * config.error_rate), config.count - anchor)

    # Sortowanie rodzajów jest konieczne dla powtarzalności: kolejność iteracji po
    # `frozenset` zależy od hashy łańcuchów, a te w Pythonie są losowane per proces
    # (PYTHONHASHSEED). Bez `sorted` to samo ziarno dawałoby inny plan w każdym procesie.
    kinds = sorted(config.faults)
    # Przydział po kolei (round-robin), potem tasowanie: każdy rodzaj pojawia się
    # tyle samo razy (±1), a pozycje i tak są losowe.
    plan = [kinds[i % len(kinds)] for i in range(faulty)]
    rng.shuffle(plan)
    positions = sorted(rng.sample(range(anchor, config.count), faulty))
    return dict(zip(positions, plan, strict=True))


def _to_line(payload: object) -> str:
    # Ta sama serializacja dla rekordów poprawnych i zepsutych - `to_json` z pydantic-core
    # to silnik, na którym stoi `model_dump_json`. Gdyby zepsute linie szły przez
    # `json.dumps`, różniłyby się formatem (spacje po przecinkach) i dało się je
    # odróżnić od poprawnych gołym okiem, bez walidacji.
    return to_json(payload).decode()


def generate(config: GeneratorConfig) -> Iterator[GeneratedRecord]:
    """Produkuje `config.count` rekordów zgodnie z planem błędów.

    Jedno źródło losowości (`random.Random(seed)`) dla wszystkiego: planu, treści
    rekordów i wariantów błędów. Konsekwencja: zmiana `error_rate` zmienia też treść
    poprawnych rekordów, bo plan zużywa inną liczbę losowań. Powtarzalność obowiązuje
    dla identycznego zestawu parametrów - i tylko tego wymaga benchmark z etapu 5.
    """
    rng = random.Random(config.seed)
    plan = _fault_plan(config, rng)
    seen_transaction_ids: list[str] = []

    for index in range(config.count):
        # Identyfikator z ziarna i pozycji: unikalny w przebiegu z konstrukcji, a nie
        # z prawdopodobieństwa. Ziarno w środku sprawia, że pliki z różnych ziaren
        # wgrane do jednej tabeli nie zderzą się identyfikatorami.
        transaction_id = f"T-{config.seed}-{index:07d}"
        event = build_valid_event(
            rng, transaction_id=transaction_id, reference_time=config.reference_time
        )
        fault = plan.get(index)

        if fault is None:
            seen_transaction_ids.append(transaction_id)
            yield GeneratedRecord(index=index, line=event.model_dump_json(), fault=None)
            continue

        # mode="json" zamienia Decimal, UUID i datetime na tekst - dostajemy słownik
        # w tej samej postaci, w jakiej rekord przyjechałby z Pub/Sub, i dopiero ten
        # słownik psujemy.
        payload = event.model_dump(mode="json")
        ctx = FaultContext(
            rng=rng,
            reference_time=config.reference_time,
            seen_transaction_ids=seen_transaction_ids,
        )
        inject_fault(fault, payload, ctx)
        yield GeneratedRecord(index=index, line=_to_line(payload), fault=fault)
