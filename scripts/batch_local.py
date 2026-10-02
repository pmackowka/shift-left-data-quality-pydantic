"""Demo etapu 5: loader batchowy, idempotentność i zgodność z wyrocznią generatora.

Uruchamiane przez `make batch-local`. Trzy kroki, każdy z kontrolą wyniku - rozjazd
kończy się kodem wyjścia 1, więc demo jest jednocześnie testem end-to-end.

1. Pierwszy load pliku z błędami: przyjęte i kwarantanna zgodne z odpowiedzią wzorcową.
2. Ten sam plik drugi raz: no-op, w danych nic nie przybywa.
3. Plik nakładający się na pierwszy: wiersze już przyjęte są pomijane jako powtórki,
   rekord z tym samym `transaction_id` i inną treścią trafia do kwarantanny jako
   duplikat, nowe rekordy są przyjmowane.
"""

import json
import os
import sys
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from dq_datagen import FAULT_CATALOG, GeneratedRecord, GeneratorConfig, generate
from dq_pipeline.batch import LoadResult, format_result, load_file
from dq_pipeline.report import build_report, format_report

ROOT = Path("data/batch")
COUNT = int(os.environ.get("BATCH_N", "10000"))


def step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def write(name: str, lines: list[str]) -> Path:
    path = ROOT / "input" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def generated(count: int, error_rate: float, seed: int) -> list[GeneratedRecord]:
    config = GeneratorConfig(
        count=count, error_rate=error_rate, seed=seed, reference_time=datetime.now(UTC)
    )
    return list(generate(config))


def load(path: Path) -> LoadResult:
    result = load_file(path, ROOT)
    print(format_result(result))
    return result


def main() -> int:
    checks: dict[str, tuple[object, object]] = {}

    step(f"Load 1: {COUNT} records, 10% deliberately broken")
    first = generated(COUNT, 0.1, seed=501)
    valid_first = sum(r.fault is None for r in first)
    expected = Counter(
        str(FAULT_CATALOG[r.fault].expected_reason) for r in first if r.fault is not None
    )
    a = load(write("a", [r.line for r in first]))
    checks["load 1 accepted"] = (a.manifest.accepted, valid_first)
    checks["load 1 quarantine"] = (a.manifest.quarantined, dict(expected))

    step("Load 2: the same file again")
    again = load(ROOT / "input" / "a.jsonl")
    checks["load 2 is a no-op"] = (again.status, "skipped")

    step("Load 3: overlapping file - 2000 replays, 1 conflicting duplicate, 1000 new")
    replays = [r.line for r in first if r.fault is None][:2000]
    conflict = json.loads(replays[0])
    conflict["event_id"] = str(uuid.UUID(int=7, version=4))
    fresh = [r.line for r in generated(1000, 0.0, seed=502)]
    b = load(write("b", [*replays, json.dumps(conflict, separators=(",", ":")), *fresh]))
    checks["load 3 accepted only new"] = (b.manifest.accepted, 1000)
    checks["load 3 replays skipped"] = (b.manifest.replayed, 2000)
    checks["load 3 conflict quarantined"] = (b.manifest.quarantined, {"duplicate_transaction": 1})

    step("Report (DuckDB over data/batch/loads/*/*.jsonl)")
    report = build_report(ROOT / "loads")
    print(format_report(report))
    checks["total events"] = (report.events, valid_first + 1000)
    checks["no duplicate rows"] = (report.distinct_transactions, report.events)

    step("Checks")
    failed = False
    for name, (actual, wanted) in checks.items():
        ok = actual == wanted
        failed |= not ok
        print(f"{'OK  ' if ok else 'FAIL'} {name}: {actual}" + ("" if ok else f" != {wanted}"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
