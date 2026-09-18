# Shift-left data quality with pydantic

Validating ecommerce events **before** they reach the warehouse — one versioned pydantic
contract guarding both a streaming and a batch pipeline on Google Cloud.

> **Status: work in progress.** The repository is built stage by stage.
> Stage 0 (tooling skeleton) is done; contracts, generator, pipelines, Terraform and the
> full README follow. See [`docs/adr/`](docs/adr/) for architecture decisions.

## Quick start

```bash
make setup   # installs Python 3.12, creates .venv, syncs the uv workspace
make check   # ruff + mypy strict + pytest — the same gate CI runs
```

Run `make help` for the full list of commands.

## Repository layout

| Path | Purpose |
| --- | --- |
| `packages/dq-contracts/` | The data contract: pydantic models, quarantine mapping, BigQuery schema generation. Single source of truth, versioned with SemVer. |
| `packages/dq-datagen/` | Synthetic event generator with deliberate fault injection. |
| `apps/` | Publisher, Cloud Run ingest service, batch loader. |
| `infra/terraform/` | Pub/Sub, BigQuery, Cloud Run, IAM — table schemas generated from the pydantic models. |
| `tests/` | One positive and one negative test per validation rule. |
| `docs/adr/` | Architecture decision records. |

## License

MIT — see [LICENSE](LICENSE).
