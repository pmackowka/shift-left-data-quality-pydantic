"""Usługa ingest: kody odpowiedzi decydują o ack/nack, więc to one są tu kontraktem."""

import base64
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dq_contracts import PipelineStage, PurchaseEvent, RejectedRecord
from dq_pipeline import ingest
from dq_pipeline.ingest import IngestSettings, PushEnvelope, create_app
from dq_pipeline.sinks import EVENTS_FILE, QUARANTINE_FILE, LocalJsonlSink


def envelope(data: bytes | None, message_id: str = "1", **message: Any) -> dict[str, Any]:
    """Koperta w formacie, który emulator wysyła naprawdę - z polami zdublowanymi."""
    body: dict[str, Any] = {
        "messageId": message_id,
        "message_id": message_id,
        "publishTime": "2026-10-01T11:17:12.194Z",
        "publish_time": "2026-10-01T11:17:12.194Z",
        "attributes": {"producer": "test"},
        **message,
    }
    if data is not None:
        body["data"] = base64.b64encode(data).decode()
    return {"subscription": "projects/local-demo/subscriptions/events-push", "message": body}


@pytest.fixture
def sink_dir(tmp_path: Path) -> Path:
    return tmp_path / "ingest"


@pytest.fixture
def client(sink_dir: Path) -> TestClient:
    return TestClient(create_app(LocalJsonlSink(sink_dir)))


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_valid_event_is_acked_and_stored(
    client: TestClient, sink_dir: Path, valid_line: bytes
) -> None:
    response = client.post("/", json=envelope(valid_line))

    assert response.status_code == 204
    assert _lines(sink_dir / EVENTS_FILE)[0]["transaction_id"] == "T-PIPE-000001"


def test_contract_violation_is_acked_and_quarantined_at_ingest(
    client: TestClient, sink_dir: Path, valid_line: bytes
) -> None:
    """Ack, nie nack: retry tych samych bajtów dałby ten sam werdykt."""
    broken = json.loads(valid_line)
    broken["currency"] = "BTC"

    response = client.post("/", json=envelope(json.dumps(broken).encode()))

    assert response.status_code == 204
    record = _lines(sink_dir / QUARANTINE_FILE)[0]
    assert record["reason"] == "unsupported_currency"
    assert record["stage"] == "ingest"
    assert not (sink_dir / EVENTS_FILE).exists()


@pytest.mark.parametrize("data", [b"{not json", None])
def test_unreadable_or_empty_message_is_quarantined_as_malformed(
    client: TestClient, sink_dir: Path, data: bytes | None
) -> None:
    response = client.post("/", json=envelope(data))

    assert response.status_code == 204
    assert _lines(sink_dir / QUARANTINE_FILE)[0]["reason"] == "malformed_payload"


def test_redelivered_message_is_acked_without_second_write(
    client: TestClient, sink_dir: Path, valid_line: bytes
) -> None:
    """Pub/Sub dostarczył drugi raz to samo - ack, jeden wiersz, zero kwarantanny."""
    client.post("/", json=envelope(valid_line, message_id="1"))
    response = client.post("/", json=envelope(valid_line, message_id="1"))

    assert response.status_code == 204
    assert len(_lines(sink_dir / EVENTS_FILE)) == 1
    assert not (sink_dir / QUARANTINE_FILE).exists()


def test_conflicting_transaction_is_quarantined_as_duplicate(
    client: TestClient, sink_dir: Path, valid_line: bytes, conflicting_line: bytes
) -> None:
    client.post("/", json=envelope(valid_line, message_id="1"))
    client.post("/", json=envelope(conflicting_line, message_id="2"))

    assert len(_lines(sink_dir / EVENTS_FILE)) == 1
    assert _lines(sink_dir / QUARANTINE_FILE)[0]["reason"] == "duplicate_transaction"


@pytest.mark.parametrize(
    "body",
    [
        {"subscription": "s"},
        {"message": {"messageId": "1", "publishTime": "2026-10-01T11:17:12Z"}},
        envelope(b"{}") | {"message": {**envelope(b"{}")["message"], "data": "%%%not-base64"}},
    ],
)
def test_invalid_envelope_is_nacked(client: TestClient, sink_dir: Path, body: Any) -> None:
    """Koperta spoza formatu push to błąd transportu: 4xx, a dane nie trafiają nigdzie."""
    response = client.post("/", json=body)

    assert response.status_code == 422
    assert not sink_dir.exists() or not any(sink_dir.iterdir())


class BrokenSink:
    """Sink, którego zapis zawsze pada - jak niedostępne BigQuery albo pełny dysk."""

    def write_events(self, events: Sequence[PurchaseEvent]) -> None:
        raise OSError("disk full")

    def write_rejected(self, records: Sequence[RejectedRecord]) -> None:
        raise OSError("disk full")


def test_sink_failure_is_nacked_so_pubsub_retries(valid_line: bytes) -> None:
    """Awaria zapisu musi skończyć się kodem błędu - ack zgubiłby rekord na zawsze."""
    client = TestClient(create_app(BrokenSink()), raise_server_exceptions=False)

    response = client.post("/", json=envelope(valid_line))

    assert response.status_code == 500


def test_health_endpoint(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_envelope_reads_camel_case_and_ignores_unknown_fields() -> None:
    parsed = PushEnvelope.model_validate(
        envelope(b"{}", deliveryAttempt=3, orderingKey="k", futureGoogleField=True)
    )
    assert parsed.message.message_id == "1"
    assert parsed.message.delivery_attempt == 3
    assert parsed.message.data == b"{}"
    assert parsed.message.attributes == {"producer": "test"}


def test_settings_read_project_prefix_and_cloud_run_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DQ_SINK_DIR", str(tmp_path))
    monkeypatch.setenv("PORT", "9090")
    settings = IngestSettings()
    assert settings.sink_dir == tmp_path
    assert settings.port == 9090


def test_invalid_port_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "not-a-port")
    with pytest.raises(ValueError, match="port"):
        IngestSettings()


def test_main_serves_the_app_on_configured_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []
    # Podmiana w module `uvicorn`, nie w `ingest` - `ingest` woła `uvicorn.run` przez
    # atrybut modułu, więc widzi podmianę bez reeksportowania nazwy.
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append(kwargs))
    monkeypatch.setenv("DQ_SINK_DIR", str(tmp_path / "sink"))
    monkeypatch.setenv("PORT", "8181")

    ingest.main()

    assert calls == [{"host": "0.0.0.0", "port": 8181, "access_log": False}]
    assert (tmp_path / "sink").is_dir()


def test_default_validator_marks_stage_as_ingest(sink_dir: Path) -> None:
    client = TestClient(create_app(LocalJsonlSink(sink_dir)))
    client.post("/", json=envelope(b"[]"))
    assert _lines(sink_dir / QUARANTINE_FILE)[0]["stage"] == PipelineStage.INGEST
