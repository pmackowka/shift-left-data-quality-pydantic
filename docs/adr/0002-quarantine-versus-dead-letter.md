# ADR 0002: kwarantanna dla błędów danych, dead-letter dla awarii przetwarzania

- **Status:** przyjęta (zastępuje pierwotny plan z README)
- **Data:** 2026-10-01
- **Etap:** 4 (streaming lokalnie)

## Kontekst

Wiadomość, której pipeline nie przyjął, może trafić w dwa miejsca: do kwarantanny (tabela
z powodem odrzucenia) albo na temat dead-letter Pub/Sub (po wyczerpaniu prób dostarczenia).
Pierwotny plan kierował uszkodzony JSON na dead-letter, a dead-letter w całości do kwarantanny.

## Rozważone warianty

| Wariant | Za | Przeciw |
| --- | --- | --- |
| Uszkodzony JSON na dead-letter, dead-letter do kwarantanny (plan pierwotny) | jedno miejsce na „wszystko, co nie weszło" | 5 prób przetworzenia bajtów, które za każdym razem dadzą ten sam błąd; awaria sinka miesza się z błędami danych i zawyża metrykę jakości |
| **Podział według pytania „czy ponowienie może coś zmienić?"** | retry tylko tam, gdzie ma sens; raport jakości liczy wyłącznie problemy danych | dwa miejsca do monitorowania zamiast jednego |

## Decyzja

- **Kwarantanna** — werdykt deterministyczny: rekord łamie kontrakt albo nie jest JSON-em
  (`malformed_payload`). Usługa odpowiada 204 (ack). Rekord zostaje z powodem i surowym
  payloadem, więc da się go naprawić i wgrać ponownie.
- **Dead-letter** — przetwarzanie się nie udało z przyczyn technicznych (sink niedostępny,
  usługa leży). Usługa odpowiada 5xx, Pub/Sub ponawia, po 5 próbach przenosi wiadomość na
  dead-letter. Stamtąd wraca na temat przez `dq-pubsub redrive`, nie do kwarantanny — leżą
  tam zwykle poprawne zdarzenia.

## Konsekwencje

- Tabela kodów odpowiedzi w `dq_pipeline.ingest` jest jedynym miejscem tej logiki.
- Redrive potwierdza wiadomość na dead-letter dopiero po potwierdzonej publikacji — najgorszy
  przypadek to podwójna publikacja, którą ingest rozpozna jako powtórkę (ADR 0003).
- Przeniesienie na dead-letter nie zostało zweryfikowane end-to-end: emulator Pub/Sub przy
  serii wiadomości wstrzymuje push po ok. 3 nieudanych rundach i niczego nie przenosi.
  Demo kładzie wiadomości na dead-letter wprost i sprawdza redrive.
