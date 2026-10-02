"""Logika publishera i redrive bez Pub/Sub - klient Google podmieniony na zwykłe funkcje.

Styk z prawdziwym Pub/Sub (emulatorem) sprawdza `make local-stream`. Tutaj sprawdzamy
decyzje: co publikować, co zatrzymać u źródła, kiedy potwierdzić wiadomość z dead-letter.
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from dq_contracts import PipelineStage
from dq_pipeline.pubsub import (
    DeadLetter,
    PublishResult,
    Topology,
    _parser,
    collect_dead_letters,
    publish_lines,
    read_ndjson,
    republish,
)
from dq_pipeline.sinks import QUARANTINE_FILE, LocalJsonlSink
from dq_pipeline.validation import RecordValidator


class Done:
    """Future, który jest już rozstrzygnięty - jak potwierdzona publikacja."""

    def result(self, timeout: float | None = None) -> object:
        return "message-id"


class Failed:
    def result(self, timeout: float | None = None) -> object:
        raise RuntimeError("broker unavailable")


class FakeTopic:
    """Zapisuje, co zostało opublikowane - zamiast `PublisherClient.publish`."""

    def __init__(self, result: PublishResult | None = None) -> None:
        self.sent: list[tuple[bytes, dict[str, str]]] = []
        self._result = result or Done()

    def __call__(self, data: bytes, attributes: Mapping[str, str]) -> PublishResult:
        self.sent.append((data, dict(attributes)))
        return self._result


def _broken(valid_line: bytes) -> bytes:
    payload = json.loads(valid_line)
    payload["currency"] = "BTC"
    return json.dumps(payload).encode()


# --- publisher -----------------------------------------------------------------


def test_validating_producer_publishes_only_valid_records(
    tmp_path: Path, valid_line: bytes
) -> None:
    topic = FakeTopic()

    summary = publish_lines(
        [valid_line, _broken(valid_line), b"{oops"],
        topic,
        validator=RecordValidator(PipelineStage.SOURCE),
        sink=LocalJsonlSink(tmp_path),
    )

    assert [data for data, _ in topic.sent] == [valid_line]
    assert summary.published == 1
    assert summary.quarantined == {"unsupported_currency": 1, "malformed_payload": 1}
    stages = {json.loads(line)["stage"] for line in (tmp_path / QUARANTINE_FILE).open()}
    assert stages == {"source"}


def test_published_bytes_are_the_original_line(tmp_path: Path, valid_line: bytes) -> None:
    """Publisher decyduje, czy wypuścić rekord - nie poprawia go po cichu."""
    topic = FakeTopic()
    original = valid_line.replace(b'"event_timestamp":"', b'"event_timestamp": "')

    publish_lines(
        [original],
        topic,
        validator=RecordValidator(PipelineStage.SOURCE),
        sink=LocalJsonlSink(tmp_path),
    )

    assert topic.sent[0][0] == original


def test_validating_producer_does_not_publish_the_same_record_twice(
    tmp_path: Path, valid_line: bytes
) -> None:
    topic = FakeTopic()

    summary = publish_lines(
        [valid_line, valid_line],
        topic,
        validator=RecordValidator(PipelineStage.SOURCE),
        sink=LocalJsonlSink(tmp_path),
    )

    assert (summary.published, summary.replays_skipped) == (1, 1)
    assert len(topic.sent) == 1


def test_legacy_producer_publishes_everything(tmp_path: Path, valid_line: bytes) -> None:
    topic = FakeTopic()

    summary = publish_lines(
        [valid_line, _broken(valid_line), b"{oops"],
        topic,
        validator=None,
        sink=LocalJsonlSink(tmp_path),
    )

    assert summary.published == 3
    assert not summary.quarantined
    assert not (tmp_path / QUARANTINE_FILE).exists()


def test_publish_failure_is_raised_not_swallowed(tmp_path: Path, valid_line: bytes) -> None:
    """Niepotwierdzona publikacja to błąd - inaczej rekord zniknąłby bez śladu."""
    with pytest.raises(RuntimeError, match="broker unavailable"):
        publish_lines(
            [valid_line], FakeTopic(Failed()), validator=None, sink=LocalJsonlSink(tmp_path)
        )


def test_read_ndjson_skips_blank_lines_and_strips_line_endings(tmp_path: Path) -> None:
    path = tmp_path / "in.jsonl"
    path.write_bytes(b'{"a":1}\r\n\n{"b":2}\n')
    assert list(read_ndjson(path)) == [b'{"a":1}', b'{"b":2}']


def test_topology_paths() -> None:
    topology = Topology(project="p1")
    assert topology.topic_path(topology.topic) == "projects/p1/topics/purchase-events"
    assert (
        topology.subscription_path(topology.dead_letter_subscription)
        == "projects/p1/subscriptions/purchase-events-dlq-pull"
    )


# --- redrive -------------------------------------------------------------------


class FakeDeadLetterSubscription:
    """Subskrypcja dead-letter, na którą wiadomości docierają w kolejnych odczytach."""

    def __init__(self, *batches: Sequence[DeadLetter]) -> None:
        self._batches = list(batches)
        self.acked: list[str] = []

    def pull(self) -> Sequence[DeadLetter]:
        return self._batches.pop(0) if self._batches else []

    def ack(self, ack_ids: Sequence[str]) -> None:
        self.acked.extend(ack_ids)


def _letter(n: int) -> DeadLetter:
    return DeadLetter(ack_id=f"ack-{n}", data=b"{}", attributes={"origin": "test"})


def test_collect_waits_until_expected_messages_arrive() -> None:
    """Pusty odczyt w środku nie kończy zbierania, dopóki nie ma tylu, ilu się spodziewamy."""
    subscription = FakeDeadLetterSubscription([_letter(1)], [], [_letter(2), _letter(3)])

    collected = collect_dead_letters(subscription.pull, expected=3, timeout_s=10)

    assert [item.ack_id for item in collected] == ["ack-1", "ack-2", "ack-3"]
    assert subscription.acked == []


def test_collect_without_expectation_stops_at_first_empty_pull() -> None:
    subscription = FakeDeadLetterSubscription([_letter(1)], [], [_letter(2)])
    assert len(collect_dead_letters(subscription.pull)) == 1


def test_collect_times_out_when_messages_never_arrive() -> None:
    ticks = iter([0.0, 5.0, 11.0])
    with pytest.raises(TimeoutError, match="collected 0 of 2"):
        collect_dead_letters(lambda: [], expected=2, timeout_s=10, clock=lambda: next(ticks))


def test_republish_acks_only_after_publish_is_confirmed() -> None:
    subscription = FakeDeadLetterSubscription()
    topic = FakeTopic()

    moved = republish([_letter(1), _letter(2)], topic, subscription.ack)

    assert moved == 2
    assert subscription.acked == ["ack-1", "ack-2"]
    assert topic.sent[0][1] == {"origin": "test", "redriven": "true"}


def test_failed_republish_leaves_messages_on_dead_letter() -> None:
    """Bez potwierdzenia publikacji nie ma ack - wiadomość wróci na dead-letter sama."""
    subscription = FakeDeadLetterSubscription()

    with pytest.raises(RuntimeError):
        republish([_letter(1)], FakeTopic(Failed()), subscription.ack)

    assert subscription.acked == []


def test_republish_of_nothing_does_not_call_ack() -> None:
    subscription = FakeDeadLetterSubscription()
    calls: list[Sequence[str]] = []
    assert republish([], FakeTopic(), calls.append) == 0
    assert calls == []
    assert subscription.acked == []


# --- CLI -----------------------------------------------------------------------


def test_cli_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Definicja poleceń: domyślny projekt z env, wymagane parametry, flaga producenta legacy."""
    monkeypatch.setenv("DQ_GCP_PROJECT", "from-env")
    parser = _parser()

    publish = parser.parse_args(["publish", "in.jsonl", "--skip-validation"])
    assert (publish.project, publish.input, publish.skip_validation) == (
        "from-env",
        Path("in.jsonl"),
        True,
    )
    assert publish.quarantine_dir == Path("data/stream/source")

    setup = parser.parse_args(["--project", "p", "setup", "--push-endpoint", "http://x/"])
    assert (setup.project, setup.min_backoff, setup.max_backoff) == ("p", 10, 600)

    redrive = parser.parse_args(["redrive", "--expected", "3"])
    assert (redrive.expected, redrive.timeout) == (3, 60.0)

    with pytest.raises(SystemExit):
        parser.parse_args(["setup"])
