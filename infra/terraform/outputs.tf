# Wartości potrzebne poza Terraformem: dokąd pchać obraz, gdzie słucha usługa,
# do czego publikować. Odczyt: `terraform output -raw <nazwa>`.

output "project_id" {
  value = google_project.this.project_id
}

output "image_repository" {
  description = "Prefix for docker tag/push of the dq-pipeline image."
  value       = google_artifact_registry_repository.images.registry_uri
}

output "ingest_url" {
  description = "Cloud Run URL of the ingest service (null before deploy_service = true)."
  value       = var.deploy_service ? google_cloud_run_v2_service.ingest[0].uri : null
}

output "topic" {
  value = google_pubsub_topic.events.id
}

output "dataset" {
  value = "${google_project.this.project_id}.${google_bigquery_dataset.dq.dataset_id}"
}
