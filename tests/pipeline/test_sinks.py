"""Lokalny sink JSONL: osobne pliki na tabele, dopisywanie, bezpieczeństwo wątków."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dq_contracts import PipelineStage, PurchaseEvent, RejectedRecord
from dq_pipeline.sinks import EVENTS_FILE, QUARANTINE_FILE, LocalJsonlSink, Sink
from dq_pipeline.validation import Accepted, RecordValidator, Rejected


def _event(line: bytes) -> PurchaseEvent:
    verdict = RecordValidator(PipelineStage.INGEST).validate(line)
    assert isinstance(verdict, Accepted)
    return verdict.event


def _rejected() -> RejectedRecord:
    verdict = RecordValidator(PipelineStage.INGEST).validate(b"{broken")
    assert isinstance(verdict, Rejected)
    return verdict.record


def test_local_sink_satisfies_the_protocol(tmp_path: Path) -> None:
    # Przypisanie do zmiennej typu `Sink` to test dla mypy, nie dla pytest: jeśli
    # sygnatura metod się rozjedzie, `make typecheck` padnie na tej linii.
    sink: Sink = LocalJsonlSink(tmp_path)
    assert sink is not None


def test_events_and_quarantine_go_to_separate_files(tmp_path: Path, valid_line: bytes) -> None:
    sink = LocalJsonlSink(tmp_path / "ingest")
    sink.write_events([_event(valid_line)])
    sink.write_rejected([_rejected()])

    events = (tmp_path / "ingest" / EVENTS_FILE).read_text().splitlines()
    quarantine = (tmp_path / "ingest" / QUARANTINE_FILE).read_text().splitlines()
    assert json.loads(events[0])["transaction_id"] == "T-PIPE-000001"
    assert json.loads(quarantine[0])["reason"] == "malformed_payload"


def test_written_event_round_trips_through_the_contract(tmp_path: Path, valid_line: bytes) -> None:
    """Linia w `events.jsonl` sama spełnia kontrakt - plik da się przetworzyć ponownie."""
    sink = LocalJsonlSink(tmp_path)
    sink.write_events([_event(valid_line)])
    line = (tmp_path / EVENTS_FILE).read_bytes().splitlines()[0]
    assert isinstance(RecordValidator(PipelineStage.BATCH).validate(line), Accepted)


def test_sink_appends_instead_of_overwriting(tmp_path: Path) -> None:
    LocalJsonlSink(tmp_path).write_rejected([_rejected()])
    LocalJsonlSink(tmp_path).write_rejected([_rejected()])
    assert len((tmp_path / QUARANTINE_FILE).read_text().splitlines()) == 2


def test_empty_batch_creates_no_file(tmp_path: Path) -> None:
    LocalJsonlSink(tmp_path).write_events([])
    assert not (tmp_path / EVENTS_FILE).exists()


def test_concurrent_writes_do_not_interleave_lines(tmp_path: Path) -> None:
    sink = LocalJsonlSink(tmp_path)
    record = _rejected()
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda _: sink.write_rejected([record] * 5), range(100)))
    lines = (tmp_path / QUARANTINE_FILE).read_text().splitlines()
    assert len(lines) == 500
    assert all(json.loads(line)["reason"] == "malformed_payload" for line in lines)
