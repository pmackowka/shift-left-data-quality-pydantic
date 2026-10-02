"""Loader batchowy: plik NDJSON -> zdarzenia i kwarantanna, idempotentnie.

Idempotentność działa tu na dwóch poziomach, bo chroni przed dwoma różnymi błędami.

1. **Plik.** Load jest identyfikowany skrótem SHA-256 treści pliku, nie nazwą. Ten sam
   plik wgrany drugi raz - pod tą samą albo inną nazwą - to no-op. Wynik loadu powstaje
   w katalogu tymczasowym i dopiero na końcu jest przenoszony na miejsce jednym
   `os.replace`. Zmiana nazwy katalogu jest atomowa, więc load przerwany w połowie
   (wyjątek, `kill`, brak miejsca) nie zostawia połowy danych: albo jest cały katalog
   z manifestem, albo nie ma nic i następne uruchomienie zaczyna od zera.

2. **Wiersz.** Różne pliki potrafią się nakładać - eksport „ostatnie 7 dni" uruchamiany
   codziennie zawiera sześć dni, które już przyszły. Pamięć transakcji zasilana z
   wcześniejszych loadów rozpoznaje takie wiersze jako powtórki i ich nie zapisuje,
   a rekord z tym samym `transaction_id`, ale inną treścią, odrzuca jako duplikat.
   To lokalny odpowiednik `MERGE ... ON transaction_id` w BigQuery.

Koszt drugiego poziomu: każdy load czyta identyfikatory wszystkich wcześniejszych
zdarzeń. Lokalnie, przy setkach tysięcy wierszy, to ułamki sekundy; w chmurze tę
robotę wykonuje hurtownia, a loader nie trzyma historii w pamięci.

Układ katalogów:

    <root>/loads/<load_id>/events.jsonl      zdarzenia przyjęte w tym loadzie
    <root>/loads/<load_id>/quarantine.jsonl  odrzucone, z powodem
    <root>/loads/<load_id>/_load.json        manifest - jego obecność = load zakończony
    <root>/staging/                          katalogi robocze trwających loadów
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from collections import Counter
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, assert_never

from pydantic import AwareDatetime, BaseModel, ConfigDict

from dq_contracts import CONTRACT_VERSION, PipelineStage, PurchaseEvent, RejectedRecord
from dq_pipeline.files import read_ndjson
from dq_pipeline.sinks import EVENTS_FILE, LocalJsonlSink
from dq_pipeline.validation import (
    Accepted,
    RecordValidator,
    Rejected,
    Replayed,
    TransactionLedger,
    fingerprint,
)

MANIFEST_FILE = "_load.json"

# Ile rekordów zbieramy w pamięci przed zapisem do pliku. Zapis rekord po rekordzie
# otwierałby plik sto tysięcy razy; zapis całości na końcu trzymałby cały plik w pamięci.
_FLUSH_EVERY = 5_000


class LoadManifest(BaseModel):
    """Opis zakończonego loadu - zapisywany obok danych, odczytywany przy kolejnym uruchomieniu.

    Model pydantic zamiast słownika, bo manifest jest czytany z dysku przez późniejsze
    uruchomienia: plik ręcznie poprawiony albo zapisany przez starszą wersję loadera
    powinien skończyć się czytelnym błędem walidacji, a nie `KeyError` w połowie raportu.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    load_id: str
    source: str
    sha256: str
    contract_version: str
    started_at: AwareDatetime
    finished_at: AwareDatetime
    lines: int
    accepted: int
    replayed: int
    quarantined: dict[str, int]

    @property
    def quarantined_total(self) -> int:
        return sum(self.quarantined.values())


class LoadResult(BaseModel):
    """Wynik wywołania loadera: manifest i informacja, czy load naprawdę się wykonał."""

    model_config = ConfigDict(frozen=True)

    status: Literal["loaded", "skipped"]
    manifest: LoadManifest
    seconds: float


def file_sha256(path: Path) -> str:
    """Skrót treści pliku liczony strumieniowo - plik nie musi mieścić się w pamięci."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _previous_events(loads: Path) -> Iterator[tuple[str, bytes]]:
    """Pary (transaction_id, odcisk) ze wszystkich zakończonych loadów.

    Linie w `events.jsonl` są w postaci kanonicznej (`LocalJsonlSink` zapisuje
    `model_dump_json()`), więc odcisk liczony z linii jest identyczny z odciskiem, który
    walidator policzy dla tego samego zdarzenia - stąd brak ponownej walidacji tutaj.
    """
    for events in sorted(loads.glob(f"*/{EVENTS_FILE}")):
        with events.open("rb") as source:
            for raw in source:
                line = raw.rstrip(b"\n")
                yield json.loads(line)["transaction_id"], fingerprint(line)


class _Buffer:
    """Bufor zapisów do sinka, opróżniany co `_FLUSH_EVERY` rekordów."""

    def __init__(self, sink: LocalJsonlSink) -> None:
        self.sink = sink
        self.events: list[PurchaseEvent] = []
        self.rejected: list[RejectedRecord] = []

    def add_event(self, event: PurchaseEvent) -> None:
        self.events.append(event)
        if len(self.events) >= _FLUSH_EVERY:
            self.flush()

    def add_rejected(self, record: RejectedRecord) -> None:
        self.rejected.append(record)
        if len(self.rejected) >= _FLUSH_EVERY:
            self.flush()

    def flush(self) -> None:
        self.sink.write_events(self.events)
        self.sink.write_rejected(self.rejected)
        self.events, self.rejected = [], []


def load_file(path: Path, root: Path) -> LoadResult:
    """Ładuje jeden plik NDJSON; drugi raz ten sam plik to no-op."""
    started = time.perf_counter()
    sha = file_sha256(path)
    # 16 znaków skrótu jako identyfikator katalogu: czytelne w `ls`, a kolizja wymaga
    # 2^32 różnych plików, zanim stanie się prawdopodobna. Pełny skrót jest w manifeście.
    load_id = sha[:16]
    loads = root / "loads"
    final = loads / load_id

    if (final / MANIFEST_FILE).exists():
        return LoadResult(
            status="skipped",
            manifest=LoadManifest.model_validate_json((final / MANIFEST_FILE).read_bytes()),
            seconds=time.perf_counter() - started,
        )

    ledger = TransactionLedger()
    ledger.seed(_previous_events(loads))
    validator = RecordValidator(PipelineStage.BATCH, ledger)

    # Unikalny katalog roboczy: dwa równoległe loady tego samego pliku nie piszą sobie
    # nawzajem w plikach. Przegrany wyścigu dowie się o tym przy `os.replace` niżej.
    staging = root / "staging" / f"{load_id}-{uuid.uuid4().hex[:8]}"
    buffer = _Buffer(LocalJsonlSink(staging))
    started_at = datetime.now(UTC)
    lines = accepted = 0
    quarantined: Counter[str] = Counter()

    try:
        for line in read_ndjson(path):
            lines += 1
            match validator.validate(line):
                case Accepted(event=event):
                    accepted += 1
                    buffer.add_event(event)
                case Replayed():
                    pass
                case Rejected(record=record):
                    quarantined[record.reason] += 1
                    buffer.add_rejected(record)
                case unreachable:
                    assert_never(unreachable)
        buffer.flush()

        manifest = LoadManifest(
            load_id=load_id,
            source=path.name,
            sha256=sha,
            contract_version=CONTRACT_VERSION,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            lines=lines,
            accepted=accepted,
            replayed=ledger.replays,
            quarantined=dict(sorted(quarantined.items())),
        )
        # Manifest zapisywany jako ostatni plik w katalogu roboczym, a katalog przenoszony
        # jako całość. Obecność manifestu w `loads/` gwarantuje więc kompletne dane obok.
        (staging / MANIFEST_FILE).write_text(manifest.model_dump_json(indent=2), "utf-8")
        loads.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(staging, final)
        except OSError:
            # Docelowy katalog już istnieje i nie jest pusty - inny proces załadował ten
            # sam plik w międzyczasie. Jego wynik jest równie dobry, nasz wyrzucamy.
            shutil.rmtree(staging, ignore_errors=True)
            return LoadResult(
                status="skipped",
                manifest=LoadManifest.model_validate_json((final / MANIFEST_FILE).read_bytes()),
                seconds=time.perf_counter() - started,
            )
    except BaseException:
        # Każde przerwanie - także Ctrl+C - sprząta katalog roboczy. Dane w `loads/`
        # nie zostały dotknięte, więc nie ma czego wycofywać.
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return LoadResult(status="loaded", manifest=manifest, seconds=time.perf_counter() - started)


def format_result(result: LoadResult) -> str:
    m = result.manifest
    rate = m.lines / result.seconds if result.seconds and result.status == "loaded" else 0
    head = f"dq-batch: {m.source} -> load {m.load_id}: {result.status}" + (
        f" in {result.seconds:.2f} s ({rate:,.0f} records/s)" if result.status == "loaded" else ""
    )
    lines = [
        head,
        f"  lines {m.lines}  accepted {m.accepted}  replayed {m.replayed}"
        f"  quarantined {m.quarantined_total}",
    ]
    lines += [f"    {reason:<24}{n:>8}" for reason, n in m.quarantined.items()]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dq-batch", description="Load NDJSON files idempotently.")
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--root", type=Path, default=Path("data/batch"))
    args = parser.parse_args(argv)
    # Pliki ładowane po kolei, w podanej kolejności. Kolejność ma znaczenie: przy
    # konflikcie treści wygrywa transakcja z pliku załadowanego wcześniej.
    for path in args.files:
        print(format_result(load_file(path, args.root)), file=sys.stdout)
    return 0
