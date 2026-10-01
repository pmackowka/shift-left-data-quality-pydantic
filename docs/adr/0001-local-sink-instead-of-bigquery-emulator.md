# ADR 0001: lokalny sink JSONL + DuckDB zamiast emulatora BigQuery

- **Status:** przyjęta
- **Data:** 2026-10-01
- **Etap:** 4 (streaming lokalnie)

## Kontekst

Pipeline ma działać w całości bez konta GCP: streaming na emulatorze Pub/Sub, batch na
plikach lokalnych. Pozostaje pytanie, gdzie lokalnie lądują dane, które na produkcji idą
do BigQuery (`events`, `quarantine`).

## Rozważone warianty

| Wariant | Za | Przeciw |
| --- | --- | --- |
| `goccy/bigquery-emulator` w Dockerze | ten sam klient `google-cloud-bigquery` co na produkcji | projekt społecznościowy, nie Google'a, implementuje dialekt BigQuery na SQLite; zgodność z BigQuery trzeba by weryfikować funkcja po funkcji, więc „działa lokalnie" nie znaczy „działa w BigQuery"; kolejny kontener w demo |
| **Interfejs `Sink` + JSONL lokalnie + DuckDB do raportów** | zero zależności, pliki czytelne w edytorze, DuckDB czyta JSONL bezpośrednio SQL-em; ścieżka biznesowa (walidacja, routing) identyczna jak w chmurze | klient BigQuery nie jest testowany lokalnie |
| Postgres w Dockerze | dojrzały, pełny SQL | inny dialekt i inny model danych niż BigQuery; testowałby integrację, której na produkcji nie ma |

## Decyzja

Wariant drugi. Zapis idzie przez protokół `Sink` (`dq_pipeline.sinks`) z implementacją
`LocalJsonlSink`; implementacja BigQuery dochodzi razem z infrastrukturą w etapie 6.

Emulator BigQuery przegrywa nie dlatego, że nie działa, tylko dlatego, że jego zielony
wynik niewiele dowodzi. Ten projekt opiera się na zachowaniach, w których reimplementacja
łatwo się rozjeżdża z oryginałem: precyzja `NUMERIC` dla kwot i semantyka `MERGE` dla
deduplikacji. Ich zgodności z emulatorem nie weryfikowano - i właśnie o to chodzi: każdy
taki punkt wymagałby osobnego sprawdzenia, a test, który przechodzi na emulatorze i pada
w chmurze, jest gorszy od braku testu, bo daje fałszywą pewność.

## Konsekwencje

- Cała logika, która decyduje o jakości danych (walidacja, kwarantanna, duplikaty), jest
  testowana lokalnie, bo nie zależy od sinka.
- Sink BigQuery pozostaje cienką warstwą bez lokalnego testu integracyjnego - ryzyko
  przyjęte świadomie i opisane w README jako niewykonane demo na GCP.
- Pliki JSONL mają kolumny przyszłych tabel, więc zapytania raportowe DuckDB przenoszą się
  do BigQuery z drobnymi zmianami dialektu.
