"""Demo etapu 4: streaming na emulatorze Pub/Sub, sprawdzony odpowiedzią wzorcową.

Uruchamiane przez `make local-stream`, które wcześniej stawia `compose.yaml` (emulator
i usługę ingest). Skrypt odgrywa trzy scenariusze i na końcu porównuje to, co pipeline
zapisał, z tym, co generator zadeklarował. Rozjazd kończy się kodem wyjścia 1 - demo
jest jednocześnie testem end-to-end, a nie tylko pokazem.

1. Producent shift-left: waliduje przed publikacją, odrzuty zostają u źródła
   (`stage=source`), na temat idą wyłącznie poprawne zdarzenia.
2. Producent legacy: publikuje bez walidacji. Te same rodzaje błędów łapie dopiero
   usługa ingest (`stage=ingest`) - po to konsument nie ufa producentowi.
3. Redrive z dead-letter: wiadomości leżące na temacie dead-letter wracają na temat
   główny i zostają przyjęte. Żadne zdarzenie nie ginie i żadne nie trafia do
   kwarantanny jako „złe dane".

Scenariusz 3 kładzie wiadomości na dead-letter SAM, publikując je wprost na ten temat.
Naturalna droga (ingest leży, Pub/Sub po 5 próbach przenosi wiadomość) nie działa
w emulatorze niezawodnie: przy serii wiadomości emulator wstrzymuje push po ok. 3
nieudanych rundach i niczego nie przenosi - sprawdzone na świeżej instancji, zarówno
przy odmowie połączenia, jak i przy HTTP 500. Polityka dead-letter jest skonfigurowana
jak na produkcji, ale jej działanie zweryfikowałby dopiero prawdziwy Pub/Sub.

Skrypt leży w `scripts/`, a nie w pakiecie, bo importuje i generator, i pipeline -
a pipeline celowo nie zależy od generatora.
"""

import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from dq_contracts import PipelineStage
from dq_datagen import FAULT_CATALOG, GeneratedRecord, GeneratorConfig, generate
from dq_pipeline.pubsub import (
    Topology,
    collect_dead_letters,
    ensure_topology,
    google_dead_letters,
    google_send,
    publish_lines,
    read_ndjson,
    republish,
)
from dq_pipeline.report import build_report, format_report
from dq_pipeline.sinks import LocalJsonlSink
from dq_pipeline.validation import RecordValidator

ROOT = Path("data/stream")
# Adres usługi z punktu widzenia emulatora - w sieci compose, nie z hosta.
PUSH_ENDPOINT = "http://ingest:8080/"


def step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def generate_file(name: str, *, count: int, error_rate: float, seed: int) -> list[GeneratedRecord]:
    """Zapisuje plik NDJSON i zwraca rekordy razem z odpowiedzią wzorcową."""
    config = GeneratorConfig(
        count=count, error_rate=error_rate, seed=seed, reference_time=datetime.now(UTC)
    )
    records = list(generate(config))
    path = ROOT / "input" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(r.line + "\n" for r in records), encoding="utf-8")
    return records


def expected_reasons(records: list[GeneratedRecord]) -> Counter[str]:
    return Counter(
        str(FAULT_CATALOG[r.fault].expected_reason) for r in records if r.fault is not None
    )


def valid_count(records: list[GeneratedRecord]) -> int:
    return sum(r.fault is None for r in records)


def ingest_line_count() -> int:
    """Ile wiadomości usługa ingest już rozstrzygnęła (przyjęte + kwarantanna)."""
    return sum(len(path.read_bytes().splitlines()) for path in (ROOT / "ingest").glob("*.jsonl"))


def wait_for(condition: Callable[[], bool], what: str, timeout_s: float = 90.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            sys.exit(f"timeout after {timeout_s:.0f}s waiting for: {what}")
        time.sleep(0.5)


def main() -> int:
    topology = Topology(project="local-demo")
    topic = topology.topic_path(topology.topic)
    send = google_send(topic)
    source_sink = LocalJsonlSink(ROOT / "source")

    step("Pub/Sub topology: topic, push subscription with dead-letter policy, DLQ pull")
    # Backoff 1-2 s zamiast produkcyjnych 10-600 s, żeby scenariusz awarii trwał
    # sekundy, a nie kwadrans. Liczba prób (5) jak na produkcji - to minimum Pub/Sub.
    ensure_topology(topology, PUSH_ENDPOINT, min_backoff_s=1, max_backoff_s=2)

    step("Scenario 1: shift-left producer validates before publishing")
    producer = generate_file("producer", count=600, error_rate=0.2, seed=101)
    first = publish_lines(
        read_ndjson(ROOT / "input" / "producer.jsonl"),
        send,
        validator=RecordValidator(PipelineStage.SOURCE),
        sink=source_sink,
    )
    print(f"published {first.published}, stopped at source {first.quarantined.total()}")

    step("Scenario 2: legacy producer publishes without validation")
    legacy = generate_file("legacy", count=400, error_rate=0.2, seed=202)
    second = publish_lines(
        read_ndjson(ROOT / "input" / "legacy.jsonl"), send, validator=None, sink=source_sink
    )
    print(f"published {second.published} unchecked messages")

    in_flight = first.published + second.published
    wait_for(lambda: ingest_line_count() >= in_flight, f"ingest to settle {in_flight} messages")
    print(f"ingest settled {ingest_line_count()} messages")

    step("Scenario 3: redrive from the dead-letter topic (messages placed there directly)")
    outage = generate_file("outage", count=20, error_rate=0.0, seed=303)
    third = publish_lines(
        read_ndjson(ROOT / "input" / "outage.jsonl"),
        google_send(topology.topic_path(topology.dead_letter_topic)),
        validator=RecordValidator(PipelineStage.SOURCE),
        sink=source_sink,
    )
    pull, ack = google_dead_letters(topology.subscription_path(topology.dead_letter_subscription))
    dead = collect_dead_letters(pull, expected=third.published, timeout_s=30)
    print(f"{len(dead)} messages waiting on {topology.dead_letter_topic}")
    moved = republish(dead, send, ack)
    print(f"redrove {moved} messages back to {topology.topic}")
    total = in_flight + third.published
    wait_for(lambda: ingest_line_count() >= total, f"ingest to settle {total} messages")

    step("Report (DuckDB over data/stream/*/*.jsonl)")
    report = build_report(ROOT)
    print(format_report(report))

    step("Check against the generator's oracle")
    checks = {
        "source quarantine": (report.quarantine_by_stage("source"), expected_reasons(producer)),
        "ingest quarantine": (report.quarantine_by_stage("ingest"), expected_reasons(legacy)),
        "accepted events": (
            report.events,
            valid_count(producer) + valid_count(legacy) + valid_count(outage),
        ),
        "no duplicate rows": (report.distinct_transactions, report.events),
    }
    failed = False
    for name, (actual, expected) in checks.items():
        ok = actual == expected
        failed |= not ok
        print(f"{'OK  ' if ok else 'FAIL'} {name}: {actual}" + ("" if ok else f" != {expected}"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
