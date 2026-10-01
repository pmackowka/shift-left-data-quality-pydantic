"""Werdykt walidatora: przyjęcie, kwarantanna z powodem, uszkodzony JSON, duplikaty."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from dq_contracts import PipelineStage, QuarantineReason
from dq_datagen import FAULT_CATALOG, GeneratorConfig, generate
from dq_pipeline.validation import Accepted, RecordValidator, Rejected


def test_valid_record_is_accepted(valid_line: bytes) -> None:
    verdict = RecordValidator(PipelineStage.INGEST).validate(valid_line)
    assert isinstance(verdict, Accepted)
    assert verdict.event.transaction_id == "T-PIPE-000001"


def test_generated_stream_matches_the_oracle() -> None:
    """Walidator pipeline'u zgadza się z odpowiedzią wzorcową generatora dla każdego rekordu."""
    validator = RecordValidator(PipelineStage.BATCH)
    config = GeneratorConfig(count=1000, error_rate=0.3, seed=5, reference_time=datetime.now(UTC))
    for record in generate(config):
        verdict = validator.validate(record.line.encode())
        if record.fault is None:
            assert isinstance(verdict, Accepted), record
        else:
            assert isinstance(verdict, Rejected), record
            assert verdict.record.reason is FAULT_CATALOG[record.fault].expected_reason
            assert verdict.record.stage is PipelineStage.BATCH


@pytest.mark.parametrize("raw", [b"{broken", b"", b"\xff\xfe{}"])
def test_unparseable_payload_is_quarantined_as_malformed(raw: bytes) -> None:
    """Uszkodzony JSON to kwarantanna, nie wyjątek - i nie dead-letter."""
    verdict = RecordValidator(PipelineStage.INGEST).validate(raw)
    assert isinstance(verdict, Rejected)
    assert verdict.record.reason is QuarantineReason.MALFORMED_PAYLOAD


def test_invalid_utf8_is_kept_in_quarantine_instead_of_crashing() -> None:
    verdict = RecordValidator(PipelineStage.INGEST).validate(b'{"a": "\xff"')
    assert isinstance(verdict, Rejected)
    assert "�" in verdict.record.raw_payload


def test_json_that_is_not_an_object_is_a_type_mismatch() -> None:
    verdict = RecordValidator(PipelineStage.INGEST).validate(b"[1, 2]")
    assert isinstance(verdict, Rejected)
    assert verdict.record.reason is QuarantineReason.TYPE_MISMATCH


def test_second_occurrence_of_transaction_is_a_duplicate(valid_line: bytes) -> None:
    validator = RecordValidator(PipelineStage.INGEST)
    assert isinstance(validator.validate(valid_line), Accepted)

    verdict = validator.validate(valid_line)
    assert isinstance(verdict, Rejected)
    assert verdict.record.reason is QuarantineReason.DUPLICATE_TRANSACTION
    assert verdict.record.transaction_id == "T-PIPE-000001"


def test_rejected_record_does_not_reserve_its_transaction_id(valid_line: bytes) -> None:
    """Poprawiona wersja odrzuconego rekordu przechodzi - naprawa danych nie jest karana."""
    broken = json.loads(valid_line)
    broken["currency"] = "BTC"
    validator = RecordValidator(PipelineStage.INGEST)

    assert isinstance(validator.validate(json.dumps(broken).encode()), Rejected)
    assert isinstance(validator.validate(valid_line), Accepted)


def test_concurrent_duplicates_are_accepted_exactly_once(valid_line: bytes) -> None:
    """Ten sam rekord z wielu wątków naraz: przyjęty raz, reszta to duplikaty."""
    validator = RecordValidator(PipelineStage.INGEST)
    with ThreadPoolExecutor(max_workers=16) as pool:
        verdicts = list(pool.map(validator.validate, [valid_line] * 200))
    assert sum(isinstance(v, Accepted) for v in verdicts) == 1
    assert validator.registry.duplicate_count == 199
