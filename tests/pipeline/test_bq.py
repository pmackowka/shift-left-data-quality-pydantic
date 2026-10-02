"""Sink BigQuery bez BigQuery: klient podmieniony na funkcję zapisującą wywołania."""

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from dq_contracts import PipelineStage, PurchaseEvent, RejectedRecord
from dq_pipeline.bq import BigQueryInsertError, BigQuerySink
from dq_pipeline.ingest import IngestSettings, build_sink
from dq_pipeline.sinks import LocalJsonlSink, Sink
from dq_pipeline.validation import Accepted, RecordValidator, Rejected


class FakeTable:
    """Zapamiętuje wywołania `insert_rows_json`; opcjonalnie zwraca błędy wierszy."""

    def __init__(self, errors: Sequence[Mapping[str, Any]] = ()) -> None:
        self.calls: list[tuple[str, list[dict[str, Any]], list[str]]] = []
        self._errors = list(errors)

    def __call__(
        self, table: str, rows: list[dict[str, Any]], row_ids: list[str]
    ) -> Sequence[Mapping[str, Any]]:
        self.calls.append((table, rows, row_ids))
        return self._errors


def _event(line: bytes) -> PurchaseEvent:
    verdict = RecordValidator(PipelineStage.INGEST).validate(line)
    assert isinstance(verdict, Accepted)
    return verdict.event


def _rejected(raw: bytes = b"{broken") -> RejectedRecord:
    verdict = RecordValidator(PipelineStage.INGEST).validate(raw)
    assert isinstance(verdict, Rejected)
    return verdict.record


def test_sink_satisfies_the_protocol() -> None:
    sink: Sink = BigQuerySink(FakeTable(), project="p", dataset="d")
    assert sink is not None


def test_event_row_keeps_decimal_precision_and_uses_transaction_id(valid_line: bytes) -> None:
    table = FakeTable()
    event = _event(valid_line)

    BigQuerySink(table, project="p1", dataset="dq").write_events([event])

    name, rows, row_ids = table.calls[0]
    assert name == "p1.dq.events"
    assert row_ids == [event.transaction_id]
    # Kwota jako tekst - BigQuery wczyta NUMERIC bez przejścia przez float.
    assert rows[0]["value"] == str(event.value)
    assert Decimal(rows[0]["value"]) == event.value
    assert isinstance(rows[0]["items"], list)
    # Wiersz da się zserializować do JSON-a - tak wysyła go klient.
    json.dumps(rows[0])


def test_quarantine_row_id_is_stable_for_the_same_message() -> None:
    """Ponowiona wiadomość daje ten sam insertId - BigQuery odrzuci drugi insert."""
    table = FakeTable()
    sink = BigQuerySink(table, project="p", dataset="dq")

    sink.write_rejected([_rejected()])
    sink.write_rejected([_rejected()])
    sink.write_rejected([_rejected(b"{other")])

    ids = [call[2][0] for call in table.calls]
    assert table.calls[0][0] == "p.dq.quarantine"
    assert ids[0] == ids[1] != ids[2]


def test_rejected_rows_raise_so_pubsub_retries(valid_line: bytes) -> None:
    """insertAll zgłasza błędy w odpowiedzi, nie wyjątkiem - sink musi je zamienić w wyjątek."""
    table = FakeTable(errors=[{"index": 0, "errors": [{"reason": "invalid"}]}])
    with pytest.raises(BigQueryInsertError, match=r"rejected 1 of 1 rows in p\.dq\.events"):
        BigQuerySink(table, project="p", dataset="dq").write_events([_event(valid_line)])


def test_empty_batch_makes_no_call() -> None:
    table = FakeTable()
    BigQuerySink(table, project="p", dataset="dq").write_events([])
    assert table.calls == []


def test_settings_require_project_for_bigquery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DQ_SINK", "bigquery")
    with pytest.raises(ValueError, match="DQ_BQ_PROJECT is required"):
        IngestSettings()


def test_settings_reject_unknown_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DQ_SINK", "bigqeury")
    with pytest.raises(ValueError, match="sink"):
        IngestSettings()


def test_build_sink_picks_implementation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    local = build_sink(IngestSettings(sink="local", sink_dir=tmp_path))
    assert isinstance(local, LocalJsonlSink)

    created: list[tuple[str, str]] = []

    def fake_bigquery_sink(project: str, dataset: str) -> Sink:
        created.append((project, dataset))
        return local

    monkeypatch.setattr("dq_pipeline.bq.bigquery_sink", fake_bigquery_sink)
    build_sink(IngestSettings(sink="bigquery", bq_project="proj", bq_dataset="dq"))
    assert created == [("proj", "dq")]
