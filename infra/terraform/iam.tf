# ============================================================================
# Tożsamości i uprawnienia - zasada najmniejszych uprawnień.
#
# Dwa konta usług zamiast jednego, bo to dwie różne role w systemie:
#   ingest-runtime - TOŻSAMOŚĆ usługi: pisze do BigQuery i nic więcej,
#   pubsub-push    - kto WOŁA usługę: subskrypcja push podpisuje nim token OIDC.
# Wspólne konto oznaczałoby, że każdy, kto może wołać usługę, może też pisać do
# hurtowni z pominięciem walidacji.
# ============================================================================

resource "google_service_account" "ingest" {
  project      = google_project.this.project_id
  account_id   = "ingest-runtime"
  display_name = "dq ingest service runtime"

  depends_on = [google_project_service.apis]
}

resource "google_service_account" "push" {
  project      = google_project.this.project_id
  account_id   = "pubsub-push"
  display_name = "Pub/Sub push identity calling the ingest service"

  depends_on = [google_project_service.apis]
}

# Zapis do tabel - na poziomie DATASETU, nie projektu. Rola projektowa dawałaby
# prawo zapisu do każdego datasetu, który kiedykolwiek powstanie w projekcie.
# `insertAll` wymaga bigquery.tables.updateData, który zawiera dataEditor;
# jobUser nie jest potrzebny, bo streaming insert nie uruchamia zadań.
resource "google_bigquery_dataset_iam_member" "ingest_writes_dataset" {
  project    = google_project.this.project_id
  dataset_id = google_bigquery_dataset.dq.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.ingest.email}"
}

# Prawo wywołania usługi tylko dla konta push - usługa NIE ma dostępu publicznego
# (brak allUsers), więc żądanie bez ważnego tokenu OIDC odbija się na warstwie
# Google, zanim dotrze do kodu.
resource "google_cloud_run_v2_service_iam_member" "push_invokes_ingest" {
  count = var.deploy_service ? 1 : 0

  project  = google_project.this.project_id
  location = var.region
  name     = google_cloud_run_v2_service.ingest[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.push.email}"
}

# Agent usługi Pub/Sub - konto zarządzane przez Google, tworzone przy włączeniu API.
# To ON, a nie nasze konta, przenosi wiadomość na dead-letter, więc potrzebuje:
#   - publikacji na temat dead-letter,
#   - subskrypcji na subskrypcji źródłowej (żeby potwierdzić przeniesioną wiadomość).
# Bez tych dwóch ról polityka dead-letter jest skonfigurowana, ale nie działa,
# i to bez żadnego błędu - wiadomości są ponawiane w nieskończoność.
# (Projekty sprzed kwietnia 2021 potrzebowały jeszcze roli tokenCreator dla agenta
# przy push z OIDC; nowo tworzony projekt jej nie wymaga.)
locals {
  pubsub_agent = "serviceAccount:service-${google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_topic_iam_member" "agent_publishes_dead_letters" {
  project = google_project.this.project_id
  topic   = google_pubsub_topic.dead_letter.name
  role    = "roles/pubsub.publisher"
  member  = local.pubsub_agent
}

resource "google_pubsub_subscription_iam_member" "agent_acks_source" {
  count = var.deploy_service ? 1 : 0

  project      = google_project.this.project_id
  subscription = google_pubsub_subscription.push[0].name
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_agent
}
