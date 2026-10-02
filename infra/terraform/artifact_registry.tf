# ============================================================================
# Rejestr obrazów Dockera - skąd Cloud Run pobiera obraz usługi ingest.
# ============================================================================

# Krok 3 (pierwszy apply, deploy_service = false). Po utworzeniu rejestru:
#   gcloud auth configure-docker europe-central2-docker.pkg.dev
#   docker buildx build --platform linux/amd64 \
#     -t $(terraform output -raw image_repository)/dq-pipeline:<tag> --push .
# --platform linux/amd64 jest konieczne przy budowaniu na Macu z procesorem ARM:
# Cloud Run uruchamia kontenery x86-64 i obraz arm64 nie wystartuje.
resource "google_artifact_registry_repository" "images" {
  project       = google_project.this.project_id
  location      = var.region
  repository_id = "dq"
  format        = "DOCKER"
  description   = "Images of the dq-pipeline (ingest service and batch loader)."

  depends_on = [google_project_service.apis]
}
