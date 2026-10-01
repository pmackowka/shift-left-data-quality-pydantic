"""Raport z przebiegu pipeline'u: SQL w DuckDB na lokalnych plikach JSONL.

DuckDB czyta JSONL bezpośrednio, bez ładowania do bazy, więc raport to zwykłe zapytanie
SQL na plikach z `LocalJsonlSink`. To samo zapytanie, z drobnymi zmianami dialektu,
pójdzie później na tabele BigQuery - raport nie zależy od tego, gdzie leżą dane.

Układ katalogów, który raport zakłada: jeden podkatalog na proces pipeline'u
(`source/`, `ingest/`, `batch/`), a w każdym `events.jsonl` i `quarantine.jsonl`.
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb

from dq_pipeline.sinks import EVENTS_FILE, QUARANTINE_FILE


@dataclass(frozen=True, slots=True)
class StreamReport:
    """Liczby z przebiegu: przyjęte zdarzenia i kwarantanna w podziale na etap i powód."""

    events: int
    distinct_transactions: int
    quarantine: list[tuple[str, str, int]]

    def quarantine_by_stage(self, stage: str) -> dict[str, int]:
        return {reason: n for row_stage, reason, n in self.quarantine if row_stage == stage}


def build_report(root: Path) -> StreamReport:
    """Liczy raport z plików pod `root`. Brak plików to zero, nie błąd.

    DuckDB na wzorcu bez dopasowań rzuca wyjątek, a pipeline bez ani jednego odrzucenia
    to sytuacja poprawna (wręcz pożądana) - stąd jawne sprawdzenie przed zapytaniem.
    """
    events_glob = str(root / "*" / EVENTS_FILE)
    quarantine_glob = str(root / "*" / QUARANTINE_FILE)

    events, distinct = 0, 0
    if any(root.glob(f"*/{EVENTS_FILE}")):
        # `columns` zamiast autodetekcji: czytamy tylko to, co liczymy, a schemat nie
        # zależy od tego, co DuckDB wywnioskuje z próbki pierwszych wierszy.
        row = duckdb.execute(
            "SELECT count(*), count(DISTINCT transaction_id) "
            "FROM read_json(?, format = 'newline_delimited', "
            "columns = {'transaction_id': 'VARCHAR'})",
            [events_glob],
        ).fetchone()
        assert row is not None  # agregat bez GROUP BY zawsze zwraca jeden wiersz
        events, distinct = row

    quarantine: list[tuple[str, str, int]] = []
    if any(root.glob(f"*/{QUARANTINE_FILE}")):
        quarantine = duckdb.execute(
            "SELECT stage, reason, count(*) AS n "
            "FROM read_json(?, format = 'newline_delimited', "
            "columns = {'stage': 'VARCHAR', 'reason': 'VARCHAR'}) "
            "GROUP BY ALL ORDER BY stage, n DESC, reason",
            [quarantine_glob],
        ).fetchall()
    return StreamReport(events=events, distinct_transactions=distinct, quarantine=quarantine)


def format_report(report: StreamReport) -> str:
    lines = [
        f"events accepted        {report.events:>8}",
        f"distinct transactions  {report.distinct_transactions:>8}",
        f"quarantined            {sum(n for _, _, n in report.quarantine):>8}",
    ]
    for stage, reason, n in report.quarantine:
        lines.append(f"  {stage:<8}{reason:<24}{n:>6}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dq-report", description="Summarise pipeline output.")
    parser.add_argument("root", type=Path, nargs="?", default=Path("data/stream"))
    args = parser.parse_args(argv)
    print(format_report(build_report(args.root)), file=sys.stdout)
    return 0
