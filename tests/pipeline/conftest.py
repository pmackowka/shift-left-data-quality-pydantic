"""Fabryki danych dla testów pipeline'u - rekordy z generatora, a nie ręcznie pisane.

Generator ma odpowiedź wzorcową dla każdego rekordu, więc testy pipeline'u nie muszą
utrzymywać własnych przykładów złych danych. Gdy kontrakt dostanie nową regułę,
generator dostanie nowy błąd, a te testy pokryją go bez zmian.
"""

import json
import random
import uuid
from datetime import UTC, datetime

import pytest

from dq_datagen import build_valid_event

REFERENCE_TIME = datetime.now(UTC)


@pytest.fixture
def valid_line() -> bytes:
    event = build_valid_event(
        random.Random(1), transaction_id="T-PIPE-000001", reference_time=REFERENCE_TIME
    )
    return event.model_dump_json().encode()


@pytest.fixture
def conflicting_line(valid_line: bytes) -> bytes:
    """Ten sam `transaction_id`, inna treść (nowe `event_id`) - prawdziwy duplikat, nie powtórka."""
    payload = json.loads(valid_line)
    payload["event_id"] = str(uuid.UUID(int=1, version=4))
    return json.dumps(payload).encode()
