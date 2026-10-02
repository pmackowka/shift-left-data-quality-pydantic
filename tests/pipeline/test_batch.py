"""Loader batchowy: idempotentność pliku i wiersza, atomowość, zgodność z wyrocznią."""

import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dq_datagen import FAULT_CATALOG, GeneratedRecord, GeneratorConfig, generate
from dq_pipeline import batch
from dq_pipeline.batch import MANIFEST_FILE, LoadManifest, format_result, load_file, main
from dq_pipeline.report import build_report
from dq_pipeline.sinks import EVENTS_FILE


def _write(path: Path, lines: list[bytes] | list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [line.encode() if isinstance(line, str) else line for line in lines]
    path.write_bytes(b"".join(line + b"\n" for line in data))
    return path


def _generated(count: int, error_rate: float, seed: int) -> list[GeneratedRecord]:
    config = GeneratorConfig(
        count=count, error_rate=error_rate, seed=seed, reference_time=datetime.now(UTC)
    )
    return list(generate(config))


def test_load_matches_the_generator_oracle(tmp_path: Path) -> None:
    records = _generated(2000, 0.25, seed=11)
    source = _write(tmp_path / "in" / "a.jsonl", [r.line for r in records])

    result = load_file(source, tmp_path / "out")

    expected = Counter(
        str(FAULT_CATALOG[r.fault].expected_reason) for r in records if r.fault is not None
    )
    assert result.status == "loaded"
    assert result.manifest.accepted == sum(r.fault is None for r in records)
    assert result.manifest.quarantined == dict(expected)
    assert result.manifest.lines == 2000
    report = build_report(tmp_path / "out" / "loads")
    assert report.events == report.distinct_transactions == result.manifest.accepted
    assert report.quarantine_by_stage("batch") == dict(expected)


def test_same_file_twice_is_a_no_op(tmp_path: Path, valid_line: bytes) -> None:
    source = _write(tmp_path / "a.jsonl", [valid_line, b"{broken"])

    first = load_file(source, tmp_path / "out")
    second = load_file(source, tmp_path / "out")

    assert (first.status, second.status) == ("loaded", "skipped")
    assert second.manifest == first.manifest
    assert len(list((tmp_path / "out" / "loads").iterdir())) == 1


def test_same_content_under_another_name_is_the_same_load(
    tmp_path: Path, valid_line: bytes
) -> None:
    """Load identyfikuje treść, nie nazwa pliku."""
    load_file(_write(tmp_path / "a.jsonl", [valid_line]), tmp_path / "out")
    again = load_file(_write(tmp_path / "copy-of-a.jsonl", [valid_line]), tmp_path / "out")
    assert again.status == "skipped"


def test_overlapping_file_skips_replays_and_quarantines_conflicts(
    tmp_path: Path, valid_line: bytes, conflicting_line: bytes
) -> None:
    """Drugi plik: jedna powtórka, jeden konflikt treści, jeden nowy rekord."""
    load_file(_write(tmp_path / "a.jsonl", [valid_line]), tmp_path / "out")
    new = _generated(1, 0.0, seed=99)[0].line.encode()

    result = load_file(
        _write(tmp_path / "b.jsonl", [valid_line, conflicting_line, new]), tmp_path / "out"
    )

    assert (result.manifest.accepted, result.manifest.replayed) == (1, 1)
    assert result.manifest.quarantined == {"duplicate_transaction": 1}
    report = build_report(tmp_path / "out" / "loads")
    assert report.events == report.distinct_transactions == 2


def test_stored_event_fingerprint_matches_validator(tmp_path: Path, valid_line: bytes) -> None:
    """Zapisana linia ma ten sam odcisk, który walidator liczy dla zdarzenia.

    Na tym stoi idempotentność wierszy: gdyby sink zapisywał inną postać niż kanoniczna,
    zasilenie pamięci z poprzednich loadów robiłoby z każdej powtórki duplikat.
    """
    load_file(_write(tmp_path / "a.jsonl", [valid_line]), tmp_path / "out")
    stored = next((tmp_path / "out" / "loads").glob(f"*/{EVENTS_FILE}")).read_bytes().strip()
    reformatted = json.dumps(json.loads(valid_line), separators=(", ", ": ")).encode()

    result = load_file(_write(tmp_path / "b.jsonl", [reformatted]), tmp_path / "out")

    assert stored != reformatted
    assert result.manifest.replayed == 1


def test_interrupted_load_leaves_nothing_behind(
    tmp_path: Path, valid_line: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Awaria w połowie: brak katalogu loadu, brak resztek w staging, ponowienie działa."""
    source = _write(tmp_path / "a.jsonl", [valid_line])

    def crash(src: object, dst: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("os.replace", crash)
    with pytest.raises(KeyboardInterrupt):
        load_file(source, tmp_path / "out")
    monkeypatch.undo()

    assert not (tmp_path / "out" / "loads").exists() or not any(
        (tmp_path / "out" / "loads").iterdir()
    )
    assert not any((tmp_path / "out" / "staging").iterdir())
    assert load_file(source, tmp_path / "out").status == "loaded"


def test_losing_a_concurrent_race_reports_skip(
    tmp_path: Path, valid_line: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inny proces skończył ten sam load pierwszy - nasz wynik ląduje w koszu, nie obok."""
    source = _write(tmp_path / "a.jsonl", [valid_line])
    winner = load_file(source, tmp_path / "winner")
    final = tmp_path / "out" / "loads" / winner.manifest.load_id
    real_replace = os.replace

    def other_process_finishes_first(src: str, dst: str) -> None:
        final.mkdir(parents=True)
        (final / MANIFEST_FILE).write_text(winner.manifest.model_dump_json())
        real_replace(src, dst)

    monkeypatch.setattr("os.replace", other_process_finishes_first)
    result = load_file(source, tmp_path / "out")

    assert result.status == "skipped"
    assert not any((tmp_path / "out" / "staging").iterdir())


def test_large_file_is_flushed_in_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(batch, "_FLUSH_EVERY", 7)
    records = _generated(50, 0.5, seed=3)
    result = load_file(_write(tmp_path / "a.jsonl", [r.line for r in records]), tmp_path / "out")
    report = build_report(tmp_path / "out" / "loads")
    assert report.events == result.manifest.accepted
    assert sum(n for _, _, n in report.quarantine) == result.manifest.quarantined_total


def test_manifest_is_validated_when_read_back(tmp_path: Path, valid_line: bytes) -> None:
    """Ręcznie zepsuty manifest to czytelny błąd walidacji, nie KeyError w raporcie."""
    result = load_file(_write(tmp_path / "a.jsonl", [valid_line]), tmp_path / "out")
    path = tmp_path / "out" / "loads" / result.manifest.load_id / MANIFEST_FILE
    path.write_text(json.dumps({**json.loads(path.read_text()), "lines": "many"}))

    with pytest.raises(ValueError, match="lines"):
        load_file(_write(tmp_path / "a.jsonl", [valid_line]), tmp_path / "out")


def test_cli_loads_files_in_order(
    tmp_path: Path, valid_line: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write(tmp_path / "a.jsonl", [valid_line, b"{broken"])

    assert main([str(source), str(source), "--root", str(tmp_path / "out")]) == 0

    out = capsys.readouterr().out
    assert ": loaded in " in out
    assert ": skipped" in out
    assert "malformed_payload" in out


def test_format_result_for_a_skipped_load(tmp_path: Path, valid_line: bytes) -> None:
    source = _write(tmp_path / "a.jsonl", [valid_line])
    load_file(source, tmp_path / "out")
    text = format_result(load_file(source, tmp_path / "out"))
    assert text.splitlines()[0].endswith(": skipped")
    assert isinstance(
        LoadManifest.model_validate_json(
            next((tmp_path / "out" / "loads").glob(f"*/{MANIFEST_FILE}")).read_bytes()
        ),
        LoadManifest,
    )
