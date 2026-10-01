"""Generator: plan błędów, powtarzalność i zgodność z odpowiedzią wzorcową."""

import json
import os
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from pydantic_core import to_json

from dq_contracts import TransactionRegistry
from dq_datagen import FAULT_CATALOG, FaultKind, GeneratorConfig, generate
from tests.datagen.helpers import verdict

REFERENCE_TIME = datetime.now(UTC)


def _config(**overrides: object) -> GeneratorConfig:
    params: dict[str, object] = {
        "count": 500,
        "error_rate": 0.3,
        "seed": 42,
        "reference_time": REFERENCE_TIME,
    }
    params.update(overrides)
    return GeneratorConfig.model_validate(params)


def test_every_record_gets_the_verdict_the_generator_declared() -> None:
    """Test najważniejszy w etapie: generator i kontrakt zgadzają się co do każdego rekordu.

    Jeden rejestr na cały przebieg, rekordy po kolei - dokładnie tak, jak przetworzy je
    pipeline. Dzięki temu duplikaty są sprawdzane w realnym kontekście, a nie przy
    sztucznie podstawionej kotwicy.
    """
    registry = TransactionRegistry()
    for record in generate(_config(count=2000)):
        expected = None if record.fault is None else FAULT_CATALOG[record.fault].expected_reason
        assert verdict(record.line, registry) is expected, record


def test_exact_number_of_faulty_records() -> None:
    faults = [r.fault for r in generate(_config(count=1000, error_rate=0.2))]
    assert sum(f is not None for f in faults) == 200


def test_fault_kinds_are_spread_evenly() -> None:
    """Round-robin daje każdemu rodzajowi tyle samo wystąpień, z dokładnością do jednego."""
    counts = Counter(r.fault for r in generate(_config()) if r.fault is not None)
    assert set(counts) == set(FaultKind)
    assert max(counts.values()) - min(counts.values()) <= 1


def test_same_parameters_give_identical_output() -> None:
    first = [r.line for r in generate(_config())]
    second = [r.line for r in generate(_config())]
    assert first == second


def test_different_seed_gives_different_output() -> None:
    first = [r.line for r in generate(_config(seed=1))]
    second = [r.line for r in generate(_config(seed=2))]
    assert first != second


def test_zero_error_rate_gives_only_valid_records() -> None:
    assert all(r.fault is None for r in generate(_config(error_rate=0)))


def test_full_error_rate_without_duplicates_breaks_every_record() -> None:
    kinds = set(FaultKind) - {FaultKind.DUPLICATE_TRANSACTION}
    assert all(r.fault is not None for r in generate(_config(error_rate=1, faults=kinds)))


def test_duplicates_keep_the_first_record_valid_as_anchor() -> None:
    records = list(generate(_config(count=50, error_rate=1)))
    assert records[0].fault is None
    assert all(r.fault is not None for r in records[1:])


def test_only_selected_fault_kinds_are_injected() -> None:
    selected = {FaultKind.MISSING_FIELD, FaultKind.TYPE_MISMATCH}
    injected = {r.fault for r in generate(_config(faults=selected)) if r.fault is not None}
    assert injected == selected


def test_faulty_and_valid_lines_share_the_same_format() -> None:
    """Zepsuta linia nie może zdradzać się formatem - tylko treścią, którą sprawdza kontrakt."""
    for record in generate(_config(count=200)):
        # Linia jest w postaci kanonicznej: ponowna serializacja jej treści daje
        # bajt w bajt to samo, czyli zero spacji i ta sama kolejność kluczy.
        assert record.line == to_json(json.loads(record.line)).decode()


def test_output_does_not_depend_on_hash_randomization() -> None:
    """Ten sam wynik w procesach z różnym PYTHONHASHSEED.

    Iteracja po `frozenset` łańcuchów zmienia kolejność między procesami. Test
    w jednym procesie tego nie wykryje, bo w obrębie procesu kolejność jest stała.
    """
    script = (
        "from dq_datagen.cli import main; "
        "main(['-n', '200', '--error-rate', '0.5', '--seed', '3', "
        "'--reference-time', '2026-09-01T00:00:00+00:00'])"
    )
    outputs = {
        subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            check=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": hash_seed},
        ).stdout
        for hash_seed in ("1", "2", "3")
    }
    assert len(outputs) == 1


def test_transaction_ids_are_unique_and_carry_the_seed() -> None:
    ids = [r.line for r in generate(_config(error_rate=0))]
    assert len(set(ids)) == len(ids)
    assert '"transaction_id":"T-42-0000000"' in ids[0]


@pytest.mark.parametrize(
    "overrides",
    [
        {"count": 0},
        {"error_rate": 1.5},
        {"error_rate": -0.1},
        {"seed": -1},
        {"faults": []},
        {"faults": ["not_a_fault"]},
        {"reference_time": "2026-01-01T00:00:00"},
    ],
)
def test_invalid_config_is_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _config(**overrides)


def test_config_coerces_text_from_command_line() -> None:
    """Tryb lax w konfiguracji: tekst z terminala zamienia się na właściwe typy."""
    config = GeneratorConfig.model_validate(
        {"count": "10", "error_rate": "0.5", "faults": ["missing_field"]}
    )
    assert config.count == 10
    assert config.error_rate == 0.5
    assert config.faults == frozenset({FaultKind.MISSING_FIELD})
