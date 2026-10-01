"""Strona Pub/Sub pipeline'u: topologia, publisher z walidacją u źródła i redrive z dead-letter.

Logika (co publikować, kiedy potwierdzić, ile przenieść) jest oddzielona od klienta
Google: funkcje `publish_lines` i `redrive` dostają zwykłe funkcje `send`, `pull`, `ack`,
a dopiero `main` podpina pod nie `PublisherClient` i `SubscriberClient`. Dzięki temu
logikę testują szybkie testy jednostkowe, a z prawdziwym Pub/Sub (emulatorem) styka się
wyłącznie `make local-stream`.

Topologia w skrócie:

    purchase-events ──push──> usługa ingest
          │ (po max_delivery_attempts nieudanych próbach)
          v
    purchase-events-dlq ──pull──> redrive ──publish──> purchase-events

Dead-letter NIE prowadzi do kwarantanny. Trafiają tam wiadomości, których nie udało się
przetworzyć z powodów technicznych (usługa leżała, sink nie odpowiadał), czyli zwykle
całkiem poprawne zdarzenia. Oznaczenie ich jako złych danych zafałszowałoby raport
jakości; właściwa reakcja to naprawić przyczynę i przepuścić je jeszcze raz.
"""

import argparse
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from google.api_core.exceptions import AlreadyExists, DeadlineExceeded
from google.cloud import pubsub_v1

from dq_contracts import PipelineStage
from dq_pipeline.sinks import LocalJsonlSink, Sink
from dq_pipeline.validation import Accepted, RecordValidator


class PublishResult(Protocol):
    """To, co zwraca publikacja: obiekt z blokującym `result()` (future z klienta Google)."""

    def result(self, timeout: float | None = None) -> object: ...


Send = Callable[[bytes, Mapping[str, str]], PublishResult]

# Ile sekund czekamy na potwierdzenie pojedynczej publikacji. Klient Google sam ponawia
# błędy przejściowe; ten limit ucina sytuację, w której broker w ogóle nie odpowiada.
_PUBLISH_TIMEOUT_S = 30.0


@dataclass(frozen=True, slots=True)
class Topology:
    """Nazwy zasobów Pub/Sub - jedno miejsce zamiast łańcuchów rozsianych po kodzie.

    Te same nazwy pojawią się w Terraformie (etap 6). Rozjazd między kodem a infrastrukturą
    skończyłby się publikowaniem do tematu, którego nikt nie subskrybuje - bez błędu.
    """

    project: str
    topic: str = "purchase-events"
    dead_letter_topic: str = "purchase-events-dlq"
    push_subscription: str = "purchase-events-push"
    dead_letter_subscription: str = "purchase-events-dlq-pull"

    def topic_path(self, name: str) -> str:
        return f"projects/{self.project}/topics/{name}"

    def subscription_path(self, name: str) -> str:
        return f"projects/{self.project}/subscriptions/{name}"


# --- publisher ---------------------------------------------------------------


@dataclass(slots=True)
class PublishSummary:
    """Wynik publikacji pliku: ile poszło na temat, ile zatrzymało się u źródła i dlaczego."""

    published: int = 0
    quarantined: Counter[str] = field(default_factory=Counter)


def publish_lines(
    lines: Iterable[bytes],
    send: Send,
    *,
    validator: RecordValidator | None,
    sink: Sink,
) -> PublishSummary:
    """Publikuje linie NDJSON, walidując je przed wysłaniem (walidacja #1).

    `validator=None` oznacza producenta, który kontraktu nie sprawdza - w demo to
    „producent legacy", na którym widać, po co konsument waliduje drugi raz.

    Publikujemy oryginalne bajty, a nie `event.model_dump_json()`. Publisher ma
    decydować, czy rekord wypuścić, a nie po cichu go poprawiać (np. normalizując strefę
    czasową) - inaczej konsument dostawałby co innego, niż producent wyprodukował,
    i rozjazd byłby niewidoczny w kwarantannie.
    """
    summary = PublishSummary()
    pending: list[PublishResult] = []
    for line in lines:
        if validator is not None:
            verdict = validator.validate(line)
            if not isinstance(verdict, Accepted):
                sink.write_rejected([verdict.record])
                summary.quarantined[verdict.record.reason] += 1
                continue
        pending.append(send(line, {}))
        summary.published += 1

    # `publish` jest asynchroniczne i grupuje wiadomości w paczki. Czekamy na wszystkie
    # potwierdzenia na końcu, a nie po każdej wiadomości - czekanie po każdej wyłączałoby
    # batching i publikacja tysiąca rekordów trwałaby tysiąc podróży do brokera.
    for result in pending:
        result.result(timeout=_PUBLISH_TIMEOUT_S)
    return summary


def read_ndjson(path: Path) -> Iterable[bytes]:
    """Linie pliku NDJSON jako bajty; puste linie pomijamy jako formatowanie, nie dane."""
    with path.open("rb") as source:
        for raw in source:
            line = raw.rstrip(b"\r\n")
            if line:
                yield line


# --- redrive -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """Wiadomość pobrana z dead-letter: identyfikator potwierdzenia i oryginalna treść."""

    ack_id: str
    data: bytes
    attributes: Mapping[str, str]


def collect_dead_letters(
    pull: Callable[[], Sequence[DeadLetter]],
    *,
    expected: int = 0,
    timeout_s: float = 60.0,
    clock: Callable[[], float] = time.monotonic,
) -> list[DeadLetter]:
    """Pobiera wiadomości z dead-letter BEZ potwierdzania ich.

    `expected` mówi, ile wiadomości ma się pojawić. Wiadomość trafia na dead-letter
    dopiero po wyczerpaniu prób, więc pusta odpowiedź z `pull` nie znaczy jeszcze, że
    wszystkie już tam są - stąd czekanie do `timeout_s`. Przy `expected=0` zbieramy to,
    co jest, do pierwszej pustej odpowiedzi.

    Pobrane, a niepotwierdzone wiadomości są „wypożyczone" na czas `ack_deadline`
    subskrypcji. Jeśli redrive padnie przed potwierdzeniem, po tym czasie wrócą na
    dead-letter same - nic nie ginie.
    """
    deadline = clock() + timeout_s
    collected: list[DeadLetter] = []
    while True:
        batch = pull()
        collected.extend(batch)
        if batch:
            continue
        if len(collected) >= expected:
            return collected
        if clock() > deadline:
            msg = f"collected {len(collected)} of {expected} expected dead letters before timeout"
            raise TimeoutError(msg)


def republish(
    items: Sequence[DeadLetter],
    send: Send,
    ack: Callable[[Sequence[str]], None],
) -> int:
    """Publikuje zebrane wiadomości na temat główny, a dopiero potem je potwierdza.

    Kolejność kroków to cała gwarancja niezawodności: najpierw publikacja i czekanie na
    jej potwierdzenie, dopiero potem ack na dead-letter. Odwrotnie - przy awarii między
    krokami - wiadomość zniknęłaby z obu miejsc. W tej kolejności najgorszy przypadek to
    podwójna publikacja, którą rejestr transakcji w ingest oznaczy jako duplikat.

    Atrybut `redriven` zostaje na wiadomości - w logach ingest widać, które rekordy
    przeszły przez dead-letter, bez szukania ich po identyfikatorach.
    """
    results = [send(item.data, {**item.attributes, "redriven": "true"}) for item in items]
    for result in results:
        result.result(timeout=_PUBLISH_TIMEOUT_S)
    if items:
        ack([item.ack_id for item in items])
    return len(items)


# --- adaptery klienta Google i CLI ---------------------------------------------


def ensure_topology(
    topology: Topology,
    push_endpoint: str,
    *,
    max_delivery_attempts: int = 5,
    min_backoff_s: int = 10,
    max_backoff_s: int = 600,
) -> None:  # pragma: no cover - styk z Pub/Sub, sprawdzany przez make local-stream
    """Tworzy tematy i subskrypcje; istniejące zasoby zostawia bez zmian.

    Na GCP ten sam układ tworzy Terraform. Ta funkcja istnieje dla emulatora, który
    startuje pusty i nie ma API do Terraforma. Dwa uprawnienia, których emulator nie
    sprawdza, a chmura tak: agent usługi Pub/Sub musi móc publikować na temat
    dead-letter i subskrybować subskrypcję źródłową - bez nich wiadomości nigdy nie
    trafią na dead-letter, i to bez żadnego błędu.
    """
    publisher = pubsub_v1.PublisherClient()
    subscriber = pubsub_v1.SubscriberClient()
    topic = topology.topic_path(topology.topic)
    dead_letter_topic = topology.topic_path(topology.dead_letter_topic)

    requests: list[Callable[[], object]] = [
        lambda: publisher.create_topic(name=topic),
        lambda: publisher.create_topic(name=dead_letter_topic),
        lambda: subscriber.create_subscription(
            request={
                "name": topology.subscription_path(topology.push_subscription),
                "topic": topic,
                "push_config": {"push_endpoint": push_endpoint},
                # Liczba prób przed przeniesieniem na dead-letter. 5 to minimum
                # dopuszczalne przez Pub/Sub.
                "dead_letter_policy": {
                    "dead_letter_topic": dead_letter_topic,
                    "max_delivery_attempts": max_delivery_attempts,
                },
                # Odstępy między ponowieniami rosną wykładniczo od min do max.
                # Lokalnie skracamy je do sekund, żeby demo nie trwało kwadransa.
                "retry_policy": {
                    "minimum_backoff": {"seconds": min_backoff_s},
                    "maximum_backoff": {"seconds": max_backoff_s},
                },
                "ack_deadline_seconds": 30,
            }
        ),
        lambda: subscriber.create_subscription(
            request={
                "name": topology.subscription_path(topology.dead_letter_subscription),
                "topic": dead_letter_topic,
                # Redrive najpierw zbiera wiadomości, potem je publikuje. Minuta daje
                # zapas na ten odstęp, zanim niepotwierdzone wiadomości wrócą do kolejki.
                "ack_deadline_seconds": 60,
            }
        ),
    ]
    for create in requests:
        try:
            create()
        except AlreadyExists:
            pass


def google_send(topic_path: str) -> Send:  # pragma: no cover - styk z Pub/Sub
    """`send` dla `publish_lines` i `republish` oparty na `PublisherClient`."""
    client = pubsub_v1.PublisherClient()

    def send(data: bytes, attributes: Mapping[str, str]) -> PublishResult:
        # Klient nie ma typów (patrz override w pyproject.toml), więc wynik to `Any`.
        # Jawna adnotacja zamienia go w nasz protokół w jednym, widocznym miejscu.
        future: PublishResult = client.publish(topic_path, data, **attributes)
        return future

    return send


def google_dead_letters(
    subscription_path: str,
) -> tuple[Callable[[], Sequence[DeadLetter]], Callable[[Sequence[str]], None]]:  # pragma: no cover
    """Para `pull`/`ack` dla subskrypcji dead-letter oparta na `SubscriberClient`."""
    client = pubsub_v1.SubscriberClient()

    def pull() -> Sequence[DeadLetter]:
        try:
            response = client.pull(
                request={"subscription": subscription_path, "max_messages": 100}, timeout=5
            )
        except DeadlineExceeded:
            # Emulator przy pustej subskrypcji trzyma połączenie do upływu limitu.
            return []
        return [
            DeadLetter(m.ack_id, m.message.data, dict(m.message.attributes))
            for m in response.received_messages
        ]

    def ack(ack_ids: Sequence[str]) -> None:
        client.acknowledge(request={"subscription": subscription_path, "ack_ids": list(ack_ids)})

    return pull, ack


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dq-pubsub", description="Pub/Sub side of the pipeline.")
    parser.add_argument(
        "--project",
        default=os.environ.get("DQ_GCP_PROJECT", "local-demo"),
        help="GCP project id (env DQ_GCP_PROJECT)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    setup = commands.add_parser("setup", help="create topics and subscriptions")
    setup.add_argument("--push-endpoint", required=True)
    setup.add_argument("--min-backoff", type=int, default=10)
    setup.add_argument("--max-backoff", type=int, default=600)

    publish = commands.add_parser("publish", help="validate and publish an NDJSON file")
    publish.add_argument("input", type=Path)
    publish.add_argument("--quarantine-dir", type=Path, default=Path("data/stream/source"))
    publish.add_argument(
        "--skip-validation",
        action="store_true",
        help="publish without the contract check, like a legacy producer",
    )

    drive = commands.add_parser("redrive", help="move dead-lettered messages back to the topic")
    drive.add_argument("--expected", type=int, default=0)
    drive.add_argument("--timeout", type=float, default=60.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - styk z Pub/Sub
    args = _parser().parse_args(argv)
    topology = Topology(project=args.project)
    topic_path = topology.topic_path(topology.topic)

    if args.command == "setup":
        ensure_topology(
            topology,
            args.push_endpoint,
            min_backoff_s=args.min_backoff,
            max_backoff_s=args.max_backoff,
        )
        print(f"dq-pubsub: topology ready in project {args.project}", file=sys.stderr)
    elif args.command == "publish":
        validator = None if args.skip_validation else RecordValidator(PipelineStage.SOURCE)
        summary = publish_lines(
            read_ndjson(args.input),
            google_send(topic_path),
            validator=validator,
            sink=LocalJsonlSink(args.quarantine_dir),
        )
        print(
            f"dq-pubsub: published {summary.published}, "
            f"quarantined at source {summary.quarantined.total()}",
            file=sys.stderr,
        )
    else:
        pull, ack = google_dead_letters(
            topology.subscription_path(topology.dead_letter_subscription)
        )
        items = collect_dead_letters(pull, expected=args.expected, timeout_s=args.timeout)
        moved = republish(items, google_send(topic_path), ack)
        print(f"dq-pubsub: redrove {moved} messages", file=sys.stderr)
    return 0
