# ============================================================================
# Projekt GCP i włączone API - fundament, na którym stoi cała reszta.
#
# Kto uruchamia `apply`, potrzebuje uprawnień spoza tego projektu (bo projekt
# jeszcze nie istnieje): roles/resourcemanager.projectCreator w organizacji lub
# folderze (konto prywatne może tworzyć projekty bez tej roli) oraz
# roles/billing.user na koncie rozliczeniowym. Poświadczenia: ADC z
# `gcloud auth application-default login` - bez kluczy JSON.
# ============================================================================

# Krok 1 każdego wdrożenia od zera. Wszystkie pozostałe zasoby zależą od tego bloku
# przez `google_project.this.project_id`, więc Terraform utworzy go jako pierwszy.
resource "google_project" "this" {
  project_id      = var.project_id
  name            = var.project_name
  billing_account = var.billing_account
  org_id          = var.org_id
  folder_id       = var.folder_id

  # Bez domyślnej sieci VPC. Cloud Run, Pub/Sub i BigQuery to usługi zarządzane
  # i jej nie potrzebują, a domyślna sieć przychodzi z szerokimi regułami firewalla.
  auto_create_network = false

  # Provider od wersji 6 domyślnie BLOKUJE usunięcie projektu (PREVENT). DELETE
  # pozwala, żeby `make destroy` faktycznie usunął projekt razem z zawartością -
  # i żeby rachunek wrócił do zera. Projekt po usunięciu przez 30 dni da się
  # jeszcze przywrócić.
  deletion_policy = "DELETE"

  labels = {
    purpose = "demo"
    repo    = "shift-left-data-quality-pydantic"
  }
}

# Krok 2: włączenie API. Nowy projekt ma prawie wszystko wyłączone, a próba
# utworzenia np. tematu Pub/Sub przy wyłączonym API kończy się błędem 403.
# Każdy zasób niżej ma `depends_on = [google_project_service.apis]`, bo Terraform
# nie widzi tej zależności sam - zasób odwołuje się do projektu, nie do API.
resource "google_project_service" "apis" {
  for_each = toset([
    "artifactregistry.googleapis.com", # rejestr obrazów Dockera
    "bigquery.googleapis.com",         # hurtownia: events, quarantine
    "iam.googleapis.com",              # konta usług
    "pubsub.googleapis.com",           # temat, subskrypcja push, dead-letter
    "run.googleapis.com",              # usługa ingest
  ])

  project = google_project.this.project_id
  service = each.key

  # Przy `destroy` i tak znika cały projekt - wyłączanie API po kolei tylko
  # wydłużyłoby teardown i potrafi się zablokować na zależnościach między API.
  disable_on_destroy = false
}
