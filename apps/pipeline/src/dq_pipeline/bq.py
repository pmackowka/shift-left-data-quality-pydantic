"""Sink BigQuery - zapis zdarzeń i kwarantanny z usługi ingest na Cloud Run.

Zapis idzie przez `insertAll` (streaming inserts): jeden wiersz na żądanie push,
widoczny w zapytaniach po sekundach. To świadomie prostszy z dwóch interfejsów - Storage
Write API jest tańszy (wg cennika z 2026-10: pierwsze 2 TiB miesięcznie za darmo, podczas gdy
`insertAll` płaci od pierwszego bajtu) i daje „dokładnie raz", ale wymaga strumieni, offsetów
i protobufów. Przy skali demo różnica to ok. $0,05 na milion zdarzeń, a w złożoności - rząd
wielkości. Zmiana dotyczy tylko tego modułu, bo reszta pipeline'u widzi wyłącznie protokół
`Sink`. Pełne uzasadnienie: docs/adr/0004-bigquery-streaming-inserts.md.

Deduplikacja ma tu trzy warstwy, każda łapie co innego:

1. `TransactionLedger` w usłudze - powtórki w obrębie jednej instancji, bez zapisu.
2. `insertId` = identyfikator wiersza - BigQuery odrzuca ponowiony insert z tym samym
   identyfikatorem w krótkim oknie (best effort, ok. minuty). Łapie ponowienia po awarii
   między zapisem a odpowiedzią 204, także między instancjami.
3. Widok `events_deduplicated` w BigQuery (Terraform) - ostateczna gwarancja dla
   zapytań, niezależna od okien czasowych i liczby instancji.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from google.cloud import bigquery

from dq_contracts import PurchaseEvent, RejectedRecord
from dq_pipeline.validation import fingerprint

Insert = Callable[[str, list[dict[str, Any]], list[str]], Sequence[Mapping[str, Any]]]


class BigQueryInsertError(RuntimeError):
    """BigQuery odrzucił wiersze. Usługa odpowie 5xx, a Pub/Sub ponowi dostarczenie."""


class BigQuerySink:
    """Implementacja protokołu `Sink` na tabelach BigQuery.

    Wiersz to `model_dump(mode="json")`: Decimal jako tekst (BigQuery przyjmuje NUMERIC
    z tekstu bez utraty precyzji - z liczby zmiennoprzecinkowej by ją tracił), czas jako
    ISO 8601, zagnieżdżone modele jako obiekty. Kolumny zgadzają się z tabelą, bo schemat
    tabeli powstał z tych samych modeli (`make schemas`).
    """

    def __init__(self, insert: Insert, *, project: str, dataset: str) -> None:
        self._insert = insert
        self.events_table = f"{project}.{dataset}.events"
        self.quarantine_table = f"{project}.{dataset}.quarantine"

    def write_events(self, events: Sequence[PurchaseEvent]) -> None:
        # insertId = transaction_id. Zdarzenie przyjęte drugi raz ma ten sam identyfikator
        # transakcji i tę samą treść (inaczej byłoby duplikatem w kwarantannie), więc
        # odrzucenie ponowionego insertu niczego nie gubi.
        self._write(
            self.events_table,
            [event.model_dump(mode="json") for event in events],
            [event.transaction_id for event in events],
        )

    def write_rejected(self, records: Sequence[RejectedRecord]) -> None:
        # Rekord kwarantanny nie ma naturalnego klucza - `transaction_id` bywa pusty albo
        # zduplikowany. insertId z odcisku etapu i surowego payloadu: ta sama wiadomość
        # ponowiona po awarii daje ten sam identyfikator.
        self._write(
            self.quarantine_table,
            [record.model_dump(mode="json") for record in records],
            [fingerprint(f"{record.stage}\n{record.raw_payload}").hex() for record in records],
        )

    def _write(self, table: str, rows: list[dict[str, Any]], row_ids: list[str]) -> None:
        if not rows:
            return
        # `insertAll` nie rzuca wyjątku przy odrzuconych wierszach - zwraca listę błędów.
        # Zignorowanie jej to klasyczna cicha utrata danych, więc zamieniamy ją w wyjątek.
        errors = self._insert(table, rows, row_ids)
        if errors:
            msg = f"BigQuery rejected {len(errors)} of {len(rows)} rows in {table}: {errors[:3]}"
            raise BigQueryInsertError(msg)


def bigquery_sink(project: str, dataset: str) -> BigQuerySink:  # pragma: no cover - styk z GCP
    """Sink podpięty pod prawdziwego klienta - poświadczenia z konta usługi Cloud Run (ADC)."""
    client = bigquery.Client(project=project)

    def insert(
        table: str, rows: list[dict[str, Any]], row_ids: list[str]
    ) -> Sequence[Mapping[str, Any]]:
        return client.insert_rows_json(table, rows, row_ids=row_ids)

    return BigQuerySink(insert, project=project, dataset=dataset)
