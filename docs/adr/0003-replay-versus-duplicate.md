# ADR 0003: powtórka to nie duplikat — odcisk treści zamiast samego identyfikatora

- **Status:** przyjęta
- **Data:** 2026-10-02
- **Etap:** 5 (batch lokalnie), poprawka do etapu 4

## Kontekst

Przy gwarancji „co najmniej raz" ten sam rekord przychodzi drugi raz z powodów
transportowych: Pub/Sub ponawia dostarczenie, redrive publikuje ponownie, ktoś wgrywa ten
sam plik. Rejestr z kontraktu (`TransactionRegistry`) pamięta wyłącznie identyfikatory, więc
każde takie ponowienie lądowało w kwarantannie jako `duplicate_transaction`.

## Rozważone warianty

| Wariant | Za | Przeciw |
| --- | --- | --- |
| Identyfikator transakcji (stan z etapu 4) | najprostszy | ponowienia transportu zawyżają kwarantannę; idempotentny batch niemożliwy |
| Para `transaction_id` + `event_id` | tania | producent może ponowić z nowym `event_id` albo wysłać inną treść z tym samym — oba przypadki źle sklasyfikowane |
| **`transaction_id` + odcisk treści kanonicznej** | powtórka = identyczne zdarzenie po walidacji, niezależnie od formatowania i strefy czasowej | koszt serializacji i skrótu na rekord |
| Rozszerzenie `TransactionRegistry` w kontrakcie | jedno miejsce | zmiana pakietu kontraktu wymusza podbicie `CONTRACT_VERSION`, które jedzie w każdym zdarzeniu — a reguły danych się nie zmieniają |

## Decyzja

`TransactionLedger` w `dq_pipeline.validation`: `transaction_id` → BLAKE2b (128 bit)
z `model_dump_json()` po walidacji. Ten sam identyfikator i odcisk → powtórka, pomijana bez
zapisu i bez kwarantanny. Ten sam identyfikator, inny odcisk → duplikat, kwarantanna.

## Konsekwencje

- Ta sama reguła w streamingu (ack bez zapisu) i w batchu (wiersz pomijany).
- Sink musi zapisywać postać kanoniczną — z niej batch zasila pamięć wcześniejszymi loadami.
- Pamięć jest per proces. Trwałą deduplikację w chmurze zapewnia BigQuery (ADR 0004).
- Wersja kontraktu zostaje 0.1.0.
