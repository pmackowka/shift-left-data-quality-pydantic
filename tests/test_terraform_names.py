"""Nazwy zasobów w Terraformie zgadzają się z nazwami, których używa kod.

Rozjazd nie dałby żadnego błędu przy `terraform validate` ani w testach jednostkowych -
publisher wysyłałby do tematu, którego nikt nie subskrybuje, a ingest pisałby do tabeli,
której nie ma. Ten test zamienia taki rozjazd w czerwony build.
"""

import re
from pathlib import Path

from dq_contracts.bigquery import TABLES
from dq_pipeline.ingest import IngestSettings
from dq_pipeline.pubsub import Topology

TERRAFORM = Path(__file__).resolve().parents[1] / "infra" / "terraform"


def _names(filename: str, attribute: str) -> set[str]:
    text = (TERRAFORM / filename).read_text()
    return set(re.findall(rf'^\s*{attribute}\s*=\s*"([^"]+)"', text, flags=re.MULTILINE))


def test_pubsub_names_match_topology() -> None:
    topology = Topology(project="any")
    assert _names("pubsub.tf", "name") == {
        topology.topic,
        topology.dead_letter_topic,
        topology.push_subscription,
        topology.dead_letter_subscription,
    }


def test_bigquery_tables_match_generated_schemas() -> None:
    tables = _names("bigquery.tf", "table_id")
    assert set(TABLES) <= tables
    for table in TABLES:
        assert (
            f'file("${{path.module}}/schemas/{table}.json")'
            in (TERRAFORM / "bigquery.tf").read_text()
        )


def test_dataset_default_matches_service_default() -> None:
    variables = (TERRAFORM / "variables.tf").read_text()
    block = variables[variables.index('variable "bq_dataset"') :]
    default = re.search(r'default\s*=\s*"([^"]+)"', block)
    assert default is not None
    assert default.group(1) == IngestSettings.model_fields["bq_dataset"].default
