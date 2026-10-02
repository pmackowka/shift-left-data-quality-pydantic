# ============================================================================
# Wersje Terraforma i providera.
#
# Ten katalog opisuje KOMPLET infrastruktury demo, łącznie z samym projektem GCP.
# W repozytorium kod jest zwalidowany (`make tf-validate`, job `terraform` w CI),
# ale świadomie nigdy nie został uruchomiony - nie istnieje projekt, rachunek ani
# wdrożona usługa. Kolejność uruchomienia od zera opisuje README, sekcja
# „Jak wyglądałoby wdrożenie", a komentarze nad blokami mówią, co robi każdy z nich.
# ============================================================================

terraform {
  # 1.9+: walidacja zmiennych może odwoływać się do innych zmiennych - korzysta
  # z tego `variables.tf` (projekt w organizacji ALBO w folderze, nie w obu).
  required_version = ">= 1.9"

  required_providers {
    google = {
      source = "hashicorp/google"
      # `~> 8.5` = dowolne 8.x od 8.5 wzwyż, bez 9.0. Major providera zmienia
      # domyślne zachowania zasobów (np. ochronę przed usunięciem), więc
      # przejście na nowy major ma być decyzją, a nie skutkiem `init -upgrade`.
      version = "~> 8.5"
    }
  }

  # Stan lokalny, bo projekt nie istnieje, a więc nie istnieje też bucket na stan.
  # Docelowo stan idzie do bucketa GCS z wersjonowaniem - ale bucket musi powstać
  # PRZED pierwszym `terraform init` z tym backendem, więc jest to krok trzeci:
  #   1. `terraform apply` ze stanem lokalnym tworzy projekt i bucket,
  #   2. odkomentowanie bloku poniżej,
  #   3. `terraform init -migrate-state` przenosi stan do bucketa.
  # Plik stanu zawiera atrybuty zasobów otwartym tekstem, dlatego *.tfstate jest
  # w .gitignore i nigdy nie trafia do repozytorium.
  #
  # backend "gcs" {
  #   bucket = "<project_id>-tfstate"
  #   prefix = "shift-left-dq"
  # }
}

# Provider bez domyślnego projektu: projekt jest tu ZASOBEM, a nie kontekstem.
# Każdy zasób wskazuje go jawnie przez `google_project.this.project_id`, więc nie da
# się przypadkiem utworzyć czegoś w projekcie ustawionym akurat w `gcloud config`.
provider "google" {
  region = var.region
}
