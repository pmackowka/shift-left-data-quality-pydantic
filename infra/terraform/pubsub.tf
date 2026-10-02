# ============================================================================
# Pub/Sub: temat zdarzeń, subskrypcja push do usługi ingest i dead-letter.
#
# Nazwy muszą się zgadzać z `dq_pipeline.pubsub.Topology` - pilnuje tego test
# `tests/test_terraform_names.py`. Rozjazd nie dałby błędu, tylko publikację do
# tematu, którego nikt nie subskrybuje.
# ============================================================================

resource "google_pubsub_topic" "events" {
  project = google_project.this.project_id
  name    = "purchase-events"

  # Wiadomości przechowywane 7 dni także po potwierdzeniu - pozwala to przewinąć
  # subskrypcję (seek) i przetworzyć dzień jeszcze raz po naprawie błędu w usłudze.
  message_retention_duration = "604800s"

  depends_on = [google_project_service.apis]
}

resource "google_pubsub_topic" "dead_letter" {
  project = google_project.this.project_id
  name    = "purchase-events-dlq"

  depends_on = [google_project_service.apis]
}

# Krok 5 (drugi apply, deploy_service = true): subskrypcja wskazuje na adres usługi,
# więc powstaje razem z nią.
resource "google_pubsub_subscription" "push" {
  count = var.deploy_service ? 1 : 0

  project = google_project.this.project_id
  name    = "purchase-events-push"
  topic   = google_pubsub_topic.events.id

  push_config {
    # Ukośnik na końcu: usługa nasłuchuje na `POST /`.
    push_endpoint = "${google_cloud_run_v2_service.ingest[0].uri}/"

    # Token OIDC podpisany kontem pubsub-push. Cloud Run sprawdza go przed wpuszczeniem
    # żądania - kod usługi nie musi (i nie powinien) robić tego sam.
    oidc_token {
      service_account_email = google_service_account.push.email
    }
  }

  # Czas na odpowiedź usługi. Ingest odpowiada w milisekundach; 30 s to zapas na
  # zimny start kontenera po skalowaniu do zera.
  ack_deadline_seconds = 30

  # Po 5 nieudanych próbach (minimum dopuszczalne przez Pub/Sub) wiadomość trafia
  # na dead-letter i wraca przez `dq-pubsub redrive`, gdy przyczyna zniknie.
  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = 5
  }

  # Wykładnicze odstępy między próbami: 10 s, potem coraz dłużej, maks. 10 minut.
  # Lokalnie demo skraca je do 1-2 s, żeby nie czekać kwadransa.
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  # Pusty ttl = subskrypcja nigdy nie wygasa. Domyślnie Pub/Sub kasuje subskrypcję
  # nieaktywną przez 31 dni - w demo, które leży miesiąc, oznaczałoby to cichą
  # utratę połączenia tematu z usługą.
  expiration_policy {
    ttl = ""
  }
}

# Subskrypcja pull na dead-letter - z niej czyta redrive. Bez żadnej subskrypcji
# wiadomości opublikowane na temat dead-letter po prostu przepadają.
resource "google_pubsub_subscription" "dead_letter_pull" {
  project = google_project.this.project_id
  name    = "purchase-events-dlq-pull"
  topic   = google_pubsub_topic.dead_letter.id

  # Redrive najpierw zbiera wiadomości, potem je publikuje - minuta zapasu, zanim
  # niepotwierdzone wiadomości wrócą do kolejki.
  ack_deadline_seconds = 60

  # Wiadomości na dead-letter czekają na człowieka - 7 dni to maksimum Pub/Sub.
  message_retention_duration = "604800s"

  expiration_policy {
    ttl = ""
  }
}
