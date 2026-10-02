"""Raport DuckDB na plikach z lokalnego sinka."""

import json
from pathlib import Path

import pytest

from dq_contracts import PipelineStage
from dq_pipeline.report import build_report, format_report, main
from dq_pipeline.sinks import LocalJsonlSink
from dq_pipeline.validation import Accepted, RecordValidator, Rejected


def _fill(root: Path, valid_line: bytes, conflicting_line: bytes) -> None:
    """Dwa procesy jak w demo: źródło z jednym odrzutem, ingest z przyjęciem i duplikatem."""
    source = RecordValidator(PipelineStage.SOURCE)
    verdict = source.validate(b"{oops")
    assert isinstance(verdict, Rejected)
    LocalJsonlSink(root / "source").write_rejected([verdict.record])

    ingest = RecordValidator(PipelineStage.INGEST)
    sink = LocalJsonlSink(root / "ingest")
    accepted = ingest.validate(valid_line)
    duplicate = ingest.validate(conflicting_line)
    assert isinstance(accepted, Accepted)
    assert isinstance(duplicate, Rejected)
    sink.write_events([accepted.event])
    sink.write_rejected([duplicate.record])


def test_report_counts_events_and_quarantine_by_stage(
    tmp_path: Path, valid_line: bytes, conflicting_line: bytes
) -> None:
    _fill(tmp_path, valid_line, conflicting_line)

    report = build_report(tmp_path)

    assert report.events == 1
    assert report.distinct_transactions == 1
    assert report.quarantine_by_stage("source") == {"malformed_payload": 1}
    assert report.quarantine_by_stage("ingest") == {"duplicate_transaction": 1}


def test_empty_directory_gives_zeros_not_an_error(tmp_path: Path) -> None:
    report = build_report(tmp_path)
    assert (report.events, report.distinct_transactions, report.quarantine) == (0, 0, [])


def test_nested_quarantine_columns_do_not_break_the_query(
    tmp_path: Path, valid_line: bytes, conflicting_line: bytes
) -> None:
    """Rekord kwarantanny ma zagnieżdżoną listę `issues`; raport czyta tylko swoje kolumny."""
    _fill(tmp_path, valid_line, conflicting_line)
    line = (tmp_path / "source" / "quarantine.jsonl").read_text().splitlines()[0]
    assert isinstance(json.loads(line)["issues"], list)
    assert build_report(tmp_path).quarantine


def test_format_and_cli(
    tmp_path: Path,
    valid_line: bytes,
    conflicting_line: bytes,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _fill(tmp_path, valid_line, conflicting_line)

    assert main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.strip() == format_report(build_report(tmp_path))
    assert "events accepted" in out
    assert "source  malformed_payload" in out
