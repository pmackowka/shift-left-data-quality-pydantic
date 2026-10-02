# ADR 0004: zapis do BigQuery przez streaming inserts + widok z deduplikacją

- **Status:** przyjęta, z zaznaczonym kandydatem do zmiany
- **Data:** 2026-10-02
- **Etap:** 6 (infrastruktura jako kod)

## Kontekst

Usługa ingest na Cloud Run zapisuje po jednym zdarzeniu na żądanie push. Trzeba wybrać
interfejs zapisu i sposób, w jaki hurtownia radzi sobie z ponowieniami między instancjami.

## Rozważone warianty

| Wariant | Za | Przeciw |
| --- | --- | --- |
| **`insertAll` (streaming inserts)** | jedno wywołanie, wiersz jako JSON, `insertId` odfiltrowuje ponowienia w krótkim oknie | płatny od pierwszego bajtu: ok. $0,01 / 200 MiB, min. 1 KB na wiersz; deduplikacja best effort |
| Storage Write API (gRPC) | 2 TiB miesięcznie za darmo, semantyka „dokładnie raz" w trybie committed | strumienie, offsety, schemat w protobufie — wielokrotnie więcej kodu przy zapisie po jednym wierszu |
| Subskrypcja BigQuery w Pub/Sub (bez usługi) | zero kodu | brak walidacji po odbiorze — znika połowa idei shift-left |
| `MERGE` przy każdym zapisie | twarda deduplikacja | zadanie DML na każde zdarzenie: limity i koszt nieproporcjonalne do jednego wiersza |

## Decyzja

`insertAll` w `dq_pipeline.bq.BigQuerySink`. `insertId` = `transaction_id` dla zdarzeń
i odcisk etapu + surowego payloadu dla kwarantanny. Odrzucone wiersze zgłaszane w odpowiedzi
API są zamieniane w wyjątek → 5xx → ponowienie przez Pub/Sub. Ostateczna gwarancja jednego
wiersza na transakcję to widok `events_deduplicated` (`QUALIFY ROW_NUMBER() ... = 1`),
z którego czytają raporty.

## Konsekwencje

- Trzy warstwy deduplikacji: pamięć powtórek w instancji (ADR 0003), `insertId`, widok.
- **Kandydat do zmiany:** przy 1 mln zdarzeń miesięcznie `insertAll` kosztuje ok. $0,05
  miesięcznie, a Storage Write API mieściłby się w darmowym limicie. Różnica jest dziś
  pomijalna, a zmiana dotyczy wyłącznie `bq.py`, bo reszta pipeline'u widzi protokół `Sink`.
- Ceny ze strony cennika BigQuery z 2026-10-02 (lista USD, regiony mogą się różnić).
