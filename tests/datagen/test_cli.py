"""Polecenie `dq-gen`: zapis NDJSON, podsumowanie na stderr, błędy argumentów z kodem 2."""

import json
import re
from pathlib import Path

import pytest

from dq_datagen.cli import main


def test_writes_ndjson_file_and_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = tmp_path / "nested" / "events.jsonl"

    exit_code = main(["-n", "100", "--error-rate", "0.2", "--seed", "7", "-o", str(output)])

    assert exit_code == 0
    lines = output.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100
    # Każda linia to osobny dokument JSON - także zepsuta, bo błędy dotyczą kontraktu,
    # nie składni.
    assert all(isinstance(json.loads(line), dict) for line in lines)
    summary = capsys.readouterr().err
    assert "100 records" in summary
    # 20% ze 100 to dokładnie 20 błędnych, bo generator układa plan, a nie losuje
    # każdy rekord osobno - więc poprawnych jest dokładnie 80.
    assert re.search(r"^\s+valid\s+80$", summary, flags=re.MULTILINE)


def test_writes_to_stdout_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    main(["-n", "5"])
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 5
    assert "5 records -> -" in captured.err


def test_restricts_faults_and_accepts_reference_time(capsys: pytest.CaptureFixture[str]) -> None:
    main(
        [
            "-n",
            "20",
            "--error-rate",
            "1",
            "--faults",
            "missing_field, unexpected_field",
            "--reference-time",
            "2026-09-01T12:00:00+02:00",
        ]
    )
    err = capsys.readouterr().err
    assert "missing_field" in err
    assert "unexpected_field" in err
    assert "value_mismatch" not in err
    assert "2026-09-01T12:00:00+02:00" in err


@pytest.mark.parametrize(
    "argv",
    [
        ["-n", "10", "--error-rate", "2"],
        ["-n", "abc"],
        ["-n", "10", "--faults", "nope"],
        ["--error-rate", "0.1"],
    ],
)
def test_invalid_arguments_exit_with_code_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    assert exc_info.value.code == 2
