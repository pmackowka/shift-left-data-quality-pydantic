# ============================================================================
# Zmienne wejściowe. Wartości podaje się w `terraform.tfvars` (w .gitignore);
# szablon z pustymi wartościami: `example.tfvars`.
# ============================================================================

# Globalnie unikalny identyfikator projektu, 6-30 znaków. Raz użyty identyfikator
# nie wraca do puli nawet po usunięciu projektu - stąd sufiks losowy w przykładzie.
variable "project_id" {
  type        = string
  description = "ID of the GCP project to create, globally unique."

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id: 6-30 chars, lowercase letters, digits and hyphens, starts with a letter."
  }
}

variable "project_name" {
  type        = string
  description = "Human-readable project name shown in the console."
  default     = "Shift-left data quality"
}

# Konto rozliczeniowe w formacie XXXXXX-XXXXXX-XXXXXX. Bez niego nie da się włączyć
# Cloud Run ani BigQuery, nawet jeśli demo mieści się w darmowych limitach.
variable "billing_account" {
  type        = string
  description = "Billing account ID linked to the project."
  sensitive   = true
}

# Projekt może należeć do organizacji albo do folderu - nie do obu naraz. Konto
# prywatne (gmail) nie ma organizacji: wtedy obie zmienne zostają null.
variable "org_id" {
  type        = string
  description = "Organization ID owning the project, or null."
  default     = null
}

variable "folder_id" {
  type        = string
  description = "Folder ID owning the project, or null."
  default     = null

  validation {
    condition     = var.folder_id == null || var.org_id == null
    error_message = "Set org_id or folder_id, not both."
  }
}

# Warszawa: dane i usługa w jednym regionie, w UE, najbliżej użytkowników sklepu.
# Ten sam region dla Cloud Run i BigQuery oznacza brak transferu między regionami.
variable "region" {
  type        = string
  description = "Region for Cloud Run, Artifact Registry and BigQuery."
  default     = "europe-central2"
}

variable "bq_dataset" {
  type        = string
  description = "BigQuery dataset holding the events and quarantine tables."
  default     = "dq"
}

# Tag obrazu wdrażanego na Cloud Run - w praktyce SHA commita. `latest` sprawiłby,
# że ten sam kod Terraforma wdraża różne wersje usługi zależnie od dnia.
variable "image_tag" {
  type        = string
  description = "Tag of the dq-pipeline image in Artifact Registry, e.g. a commit SHA."
  default     = "local"
}

# Przełącznik dwuetapowego pierwszego wdrożenia. Usługa Cloud Run wskazuje na obraz,
# a obrazu nie da się wypchnąć, zanim istnieje rejestr. Stąd:
#   1. apply z deploy_service = false - projekt, API, rejestr, Pub/Sub, BigQuery, IAM,
#   2. docker push obrazu do rejestru,
#   3. apply z deploy_service = true - usługa Cloud Run i subskrypcja push do niej.
variable "deploy_service" {
  type        = bool
  description = "Create the Cloud Run service and its push subscription (needs the image pushed)."
  default     = true
}

# Ochrona tabel BigQuery i usługi przed `terraform destroy`. Domyślnie wyłączona,
# bo to demo z teardownem jedną komendą; w projekcie z prawdziwymi danymi - true.
variable "deletion_protection" {
  type        = bool
  description = "Protect BigQuery tables and the Cloud Run service from deletion."
  default     = false
}
