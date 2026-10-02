# ============================================================================
# Cloud Run: usługa ingest - odbiorca subskrypcji push.
# ============================================================================

# Krok 5 (drugi apply, deploy_service = true) - wymaga obrazu wypchniętego do
# rejestru w kroku 4. Bez obrazu Cloud Run odrzuci rewizję i apply się nie powiedzie.
resource "google_cloud_run_v2_service" "ingest" {
  count = var.deploy_service ? 1 : 0

  project  = google_project.this.project_id
  name     = "dq-ingest"
  location = var.region

  # Ruch z internetu dopuszczony na poziomie sieci, ale bez allUsers w IAM: żądanie
  # bez tokenu OIDC konta pubsub-push dostaje 403 od Google, zanim dotrze do kontenera.
  # To standardowa konfiguracja dla push z uwierzytelnieniem.
  ingress = "INGRESS_TRAFFIC_ALL"

  deletion_protection = var.deletion_protection

  template {
    service_account = google_service_account.ingest.email

    # Skalowanie do zera: brak ruchu = brak kosztu. Górny limit chroni rachunek
    # przed zalewem wiadomości (np. replay tygodnia z subskrypcji) - nadmiar czeka
    # w Pub/Sub zamiast uruchamiać setki instancji.
    scaling {
      min_instance_count = 0
      max_instance_count = 5
    }

    containers {
      image = "${google_artifact_registry_repository.images.registry_uri}/dq-pipeline:${var.image_tag}"

      # Ta sama usługa co lokalnie, inny sink - przełącza go wyłącznie konfiguracja.
      env {
        name  = "DQ_SINK"
        value = "bigquery"
      }
      env {
        name  = "DQ_BQ_PROJECT"
        value = google_project.this.project_id
      }
      env {
        name  = "DQ_BQ_DATASET"
        value = google_bigquery_dataset.dq.dataset_id
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        # CPU przydzielany tylko w trakcie obsługi żądania - przy usłudze, która
        # nie robi nic w tle, to płacenie wyłącznie za faktyczną pracę.
        cpu_idle = true
      }

      # Ruch trafia do instancji dopiero, gdy /health odpowiada - pierwsze wiadomości
      # po zimnym starcie nie dostają błędu połączenia.
      startup_probe {
        http_get {
          path = "/health"
        }
      }
    }
  }

  depends_on = [google_project_service.apis]
}
