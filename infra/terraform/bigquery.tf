# ============================================================================
# BigQuery: dataset i dwie tabele - events i quarantine.
#
# Schematy tabel NIE są tu pisane ręcznie. Powstają z modeli pydantic
# (`make schemas` -> schemas/*.json), a test w pakiecie kontraktu pada, gdy model
# zmieni się bez przegenerowania plików. Definicja tabeli i walidacja danych mają
# jedno źródło.
# ============================================================================

resource "google_bigquery_dataset" "dq" {
  project    = google_project.this.project_id
  dataset_id = var.bq_dataset
  # Ten sam region co usługa ingest - zapis bez transferu między regionami i dane
  # w jednym, znanym miejscu w UE.
  location    = var.region
  description = "Purchase events accepted by the contract and records rejected by it."

  # Usunięcie datasetu z tabelami w środku - potrzebne, żeby teardown był jedną
  # komendą. Przy prawdziwych danych: false.
  delete_contents_on_destroy = !var.deletion_protection

  depends_on = [google_project_service.apis]
}

resource "google_bigquery_table" "events" {
  project     = google_project.this.project_id
  dataset_id  = google_bigquery_dataset.dq.dataset_id
  table_id    = "events"
  description = "Purchase events that passed the contract. Generated schema: schemas/events.json."
  schema      = file("${path.module}/schemas/events.json")

  # Partycja dzienna po czasie ZDARZENIA, nie po czasie zapisu. Raport „sprzedaż
  # z 3 października" czyta jedną partycję, nawet jeśli część zdarzeń dotarła
  # z opóźnieniem. Koszt zapytania w BigQuery = przeczytane bajty, więc partycja
  # to najprostsza oszczędność, jaka istnieje.
  time_partitioning {
    type  = "DAY"
    field = "event_timestamp"
  }

  # Klastrowanie po transaction_id: wyszukiwanie transakcji i deduplikacja
  # (widok niżej) czytają posortowane bloki zamiast całej partycji.
  clustering = ["transaction_id"]

  deletion_protection = var.deletion_protection
}

resource "google_bigquery_table" "quarantine" {
  project     = google_project.this.project_id
  dataset_id  = google_bigquery_dataset.dq.dataset_id
  table_id    = "quarantine"
  description = "Records rejected by the contract, with reason and raw payload. Generated schema: schemas/quarantine.json."
  schema      = file("${path.module}/schemas/quarantine.json")

  time_partitioning {
    type  = "DAY"
    field = "rejected_at"
  }

  # Typowe pytanie do kwarantanny: „ile odrzuceń z powodu X na etapie Y w tym
  # tygodniu" - klastrowanie po tych dwóch kolumnach.
  clustering = ["stage", "reason"]

  deletion_protection = var.deletion_protection
}

# Ostatnia warstwa deduplikacji. Ingest pomija powtórki w pamięci instancji,
# a BigQuery odrzuca ponowiony insert z tym samym insertId - ale oba mechanizmy
# mają granice (restart instancji, kilka instancji naraz, okno ok. minuty).
# Widok gwarantuje jeden wiersz na transakcję w każdym zapytaniu, niezależnie od
# tego, co działo się przy zapisie. Raporty czytają widok, nie tabelę.
resource "google_bigquery_table" "events_deduplicated" {
  project     = google_project.this.project_id
  dataset_id  = google_bigquery_dataset.dq.dataset_id
  table_id    = "events_deduplicated"
  description = "One row per transaction_id - read this instead of the raw events table."

  view {
    use_legacy_sql = false
    query          = <<-SQL
      SELECT *
      FROM `${google_project.this.project_id}.${google_bigquery_dataset.dq.dataset_id}.${google_bigquery_table.events.table_id}`
      WHERE TRUE
      QUALIFY ROW_NUMBER() OVER (PARTITION BY transaction_id ORDER BY event_timestamp, event_id) = 1
    SQL
  }

  deletion_protection = var.deletion_protection
}
