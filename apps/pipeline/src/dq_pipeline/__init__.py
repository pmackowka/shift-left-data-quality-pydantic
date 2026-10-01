"""Pipeline'y egzekwujące kontrakt `dq_contracts`: streaming (Pub/Sub) i batch.

Wspólny rdzeń obu trybów to dwa elementy:

- `validation` - jedno miejsce, w którym surowe bajty zamieniają się w werdykt
  „przyjęty" albo „do kwarantanny"; publisher, usługa ingest i loader batchowy
  wołają dokładnie ten sam kod,
- `sinks`      - abstrakcja zapisu; lokalnie JSONL, w chmurze BigQuery.

Dzięki temu różnice między trybami ograniczają się do transportu (skąd przychodzą
bajty), a nie do logiki biznesowej - i to ta logika jest testowana raz.
"""

__version__ = "0.1.0"
