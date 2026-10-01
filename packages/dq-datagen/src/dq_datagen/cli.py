"""Linia poleceń generatora: `dq-gen` zapisuje zdarzenia do pliku NDJSON.

Dane idą na wyjście (plik albo stdout), podsumowanie na stderr. Rozdzielenie strumieni
pozwala przekierować dane dalej (`dq-gen -o - | ...`) bez wycinania z nich komunikatów.

Parsowaniem argumentów zajmuje się argparse, ale ich walidacją - `GeneratorConfig`.
Gdyby reguły („odsetek błędów między 0 a 1") żyły też w argparse, istniałyby w dwóch
miejscach i rozjechały się przy pierwszej zmianie. To ta sama zasada jednego źródła
prawdy, która rządzi kontraktem, tylko w mniejszej skali.
"""

import argparse
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from pydantic import ValidationError

from dq_datagen.faults import FaultKind
from dq_datagen.generator import GeneratorConfig, generate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dq-gen",
        description="Generate synthetic purchase events with deliberate contract violations.",
    )
    parser.add_argument("-n", "--count", required=True, help="number of records")
    parser.add_argument("--error-rate", default="0", help="share of faulty records, 0-1")
    parser.add_argument("--seed", default="0", help="random seed for a reproducible dataset")
    parser.add_argument(
        "--faults",
        help=f"comma-separated fault kinds, default: all ({', '.join(FaultKind)})",
    )
    parser.add_argument(
        "--reference-time",
        help="ISO 8601 time with offset that event timestamps are relative to, default: now. "
        "future_timestamp faults are 1-14 h ahead of it, so validate the file soon after.",
    )
    parser.add_argument("-o", "--output", default="-", help="output file, '-' for stdout")
    return parser


def _write(config: GeneratorConfig, out: TextIO) -> Counter[str]:
    counts: Counter[str] = Counter()
    for record in generate(config):
        out.write(record.line + "\n")
        counts[record.fault or "valid"] += 1
    return counts


def _print_summary(config: GeneratorConfig, target: str, counts: Counter[str]) -> None:
    print(
        f"dq-gen: {config.count} records -> {target} "
        f"(seed={config.seed}, reference_time={config.reference_time.isoformat()})",
        file=sys.stderr,
    )
    # Stała kolejność wierszy (valid, potem błędy alfabetycznie), żeby dwa przebiegi
    # dało się porównać diffem.
    for label in ["valid", *sorted(FaultKind)]:
        if counts[label]:
            print(f"  {label:<24}{counts[label]:>8}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    # Słownik z samymi podanymi parametrami - brakujące biorą wartość domyślną z modelu,
    # a nie z argparse, więc domyślne wartości też są w jednym miejscu.
    raw: dict[str, object] = {"count": args.count, "error_rate": args.error_rate, "seed": args.seed}
    if args.faults:
        raw["faults"] = [kind.strip() for kind in args.faults.split(",")]
    if args.reference_time:
        raw["reference_time"] = args.reference_time

    try:
        config = GeneratorConfig.model_validate(raw)
    except ValidationError as exc:
        # `parser.error` kończy proces kodem 2, jak każdy inny błąd argumentów -
        # skrypt wołający dq-gen nie musi odróżniać błędu argparse od błędu modelu.
        parser.error(str(exc))

    if args.output == "-":
        counts = _write(config, sys.stdout)
    else:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as out:
            counts = _write(config, out)

    _print_summary(config, args.output, counts)
    return 0
