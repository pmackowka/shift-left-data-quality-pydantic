"""Zapis wyników walidacji: zdarzenia przyjęte i rekordy kwarantanny.

`Sink` to protokół, a nie klasa bazowa. Implementacja nie musi niczego dziedziczyć,
wystarczy, że ma te dwie metody - mypy sprawdzi zgodność strukturalnie. Dzięki temu
sink BigQuery (etap 6) nie będzie zależał od tego modułu, a testy mogą podstawić
dowolny obiekt z tymi metodami.

Implementacja lokalna pisze JSONL - po jednym pliku na tabelę docelową. Pliki mają
dokładnie te kolumny, co przyszłe tabele BigQuery, więc raport DuckDB na plikach
lokalnych to ten sam SQL, który później pójdzie na hurtownię. Uzasadnienie wyboru
zamiast emulatora BigQuery: docs/adr/0001-local-sink-instead-of-bigquery-emulator.md.
"""

import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Protocol

from dq_contracts import PurchaseEvent, RejectedRecord

EVENTS_FILE: Final = "events.jsonl"
QUARANTINE_FILE: Final = "quarantine.jsonl"


class Sink(Protocol):
    """Miejsce docelowe pipeline'u - tabele `events` i `quarantine`."""

    def write_events(self, events: Sequence[PurchaseEvent]) -> None: ...

    def write_rejected(self, records: Sequence[RejectedRecord]) -> None: ...


class LocalJsonlSink:
    """Sink na lokalny dysk: dwa pliki JSONL w jednym katalogu.

    Każdy proces pipeline'u dostaje WŁASNY katalog (np. `data/stream/source/`
    i `data/stream/ingest/`). Dopisywanie do wspólnego pliku z dwóch procesów - a tu
    jeden z nich żyje w kontenerze, za bind mountem - nie ma gwarancji atomowości
    linii, więc dwa zapisy mogłyby się przepleść w środku rekordu. Wewnątrz procesu
    porządek trzyma blokada, bo ingest pisze z wielu wątków naraz.

    Tryb „a" (dopisywanie) zamiast nadpisywania: restart usługi nie kasuje tego,
    co zapisała poprzednia instancja.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write_events(self, events: Sequence[PurchaseEvent]) -> None:
        self._append(EVENTS_FILE, [event.model_dump_json() for event in events])

    def write_rejected(self, records: Sequence[RejectedRecord]) -> None:
        self._append(QUARANTINE_FILE, [record.model_dump_json() for record in records])

    def _append(self, filename: str, lines: list[str]) -> None:
        if not lines:
            return
        # Serializacja przed blokadą, zapis pod blokadą: wątki nie czekają na siebie
        # w czasie, gdy pydantic zamienia model na JSON.
        payload = "".join(line + "\n" for line in lines)
        with self._lock, (self.directory / filename).open("a", encoding="utf-8") as out:
            out.write(payload)
