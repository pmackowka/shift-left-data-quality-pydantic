"""Benchmark etapu 5: ile kosztuje walidacja kontraktu i co daje jej pominięcie.

Uruchamiane przez `make bench`. Porównuje sposoby zamiany linii NDJSON na model
`PurchaseEvent` na tym samym zbiorze poprawnych rekordów:

- `json.loads`            - sam parser JSON, dolna granica kosztu, bez modelu,
- `model_validate_json`   - ścieżka produkcyjna pipeline'u: parsowanie i walidacja w Ruście,
- `TypeAdapter(...)`      - ten sam silnik przez adapter, per rekord i całą partią naraz,
- `json.loads + model_validate` - parsowanie w Pythonie, walidacja słownika; w trybie
  strict odrzuca wszystko, więc mierzymy też wariant z `strict=False`,
- `json.loads + model_construct` - zero walidacji.

Czas to tylko połowa odpowiedzi. Druga kolumna mówi, ile celowo zepsutych rekordów
(z generatora, 10% błędów) dany wariant przepuścił - `model_construct` jest najszybszy
właśnie dlatego, że przepuszcza wszystko, także ujemne kwoty i walutę BTC.
"""

import argparse
import gc
import json
import platform
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import pydantic
from pydantic import TypeAdapter, ValidationError

from dq_contracts import PurchaseEvent
from dq_datagen import GeneratorConfig, generate

Variant = Callable[[list[str]], int]

EVENT = TypeAdapter(PurchaseEvent)
EVENTS = TypeAdapter(list[PurchaseEvent])


def _count_valid(parse: Callable[[str], object]) -> Variant:
    """Zamienia funkcję jednego rekordu w wariant: liczy rekordy przyjęte bez wyjątku."""

    def run(lines: list[str]) -> int:
        accepted = 0
        for line in lines:
            try:
                parse(line)
            except ValidationError:
                continue
            accepted += 1
        return accepted

    return run


def _whole_batch(lines: list[str]) -> int:
    """Cała partia jednym wywołaniem: jedna tablica JSON, jeden przebieg silnika w Ruście.

    Haczyk: jeden zły rekord unieważnia wynik całej partii (wyjątek zamiast listy), więc
    ten wariant nadaje się do danych już zwalidowanych, nie do sortowania na dobre i złe.
    """
    try:
        return len(EVENTS.validate_json("[" + ",".join(lines) + "]"))
    except ValidationError:
        return 0


VARIANTS: dict[str, Variant] = {
    "json.loads (no model)": lambda lines: len([json.loads(line) for line in lines]),
    "model_validate_json": _count_valid(PurchaseEvent.model_validate_json),
    "TypeAdapter.validate_json": _count_valid(EVENT.validate_json),
    "TypeAdapter(list).validate_json": _whole_batch,
    "json.loads + model_validate": _count_valid(
        lambda line: PurchaseEvent.model_validate(json.loads(line))
    ),
    "json.loads + model_validate(strict=False)": _count_valid(
        lambda line: PurchaseEvent.model_validate(json.loads(line), strict=False)
    ),
    "json.loads + model_construct": _count_valid(
        lambda line: PurchaseEvent.model_construct(**json.loads(line))
    ),
}


def _lines(count: int, error_rate: float, seed: int) -> list[str]:
    config = GeneratorConfig(
        count=count, error_rate=error_rate, seed=seed, reference_time=datetime.now(UTC)
    )
    return [record.line for record in generate(config)]


def _best_of(variant: Variant, lines: list[str], repeats: int) -> tuple[float, int]:
    """Najlepszy czas z kilku powtórzeń.

    Minimum, nie średnia: zakłócenia (inny proces, przełączenie rdzenia) mogą czas
    tylko wydłużyć, nigdy skrócić - więc minimum jest najbliżej kosztu samego kodu.
    GC wyłączony na czas pomiaru, żeby sprzątanie po poprzednim wariancie nie
    obciążało następnego.
    """
    best, accepted = float("inf"), 0
    for _ in range(repeats):
        gc.collect()
        gc.disable()
        try:
            start = time.perf_counter()
            accepted = variant(lines)
            best = min(best, time.perf_counter() - start)
        finally:
            gc.enable()
    return best, accepted


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark contract validation strategies.")
    parser.add_argument("--count", type=int, default=100_000)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    print(f"generating {args.count} valid records + 10k sample with 10% faults...", flush=True)
    clean = _lines(args.count, 0.0, seed=7)
    dirty = _lines(10_000, 0.1, seed=8)
    faulty = 1_000

    construct_sample = PurchaseEvent.model_construct(**json.loads(clean[0]))
    print(
        f"\nPython {platform.python_version()}, pydantic {pydantic.VERSION}, "
        f"{platform.machine()} {platform.system()}, {args.count} records, best of {args.repeats}\n"
    )
    print(
        "| variant | total s | µs / record | records / s | accepted (clean) | faulty let through |"
    )
    print("| --- | ---: | ---: | ---: | ---: | ---: |")
    for name, variant in VARIANTS.items():
        seconds, accepted = _best_of(variant, clean, args.repeats)
        # Zepsute rekordy przepuszczone = przyjęte w próbce z błędami ponad liczbę poprawnych.
        # Dla `json.loads` kolumna nie ma sensu - nie ma modelu, który mógłby cokolwiek odrzucić.
        let_through = "—" if "no model" in name else str(max(variant(dirty) - (10_000 - faulty), 0))
        print(
            f"| `{name}` | {seconds:.2f} | {seconds / args.count * 1e6:.1f} | "
            f"{args.count / seconds:,.0f} | {accepted:,} | {let_through} |"
        )

    print(
        "\nmodel_construct keeps JSON types: "
        f"value is {type(construct_sample.value).__name__} (expected Decimal: "
        f"{isinstance(construct_sample.value, Decimal)}), "
        f"items[0] is {type(construct_sample.items[0]).__name__}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
