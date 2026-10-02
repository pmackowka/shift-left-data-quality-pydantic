# ADR 0005: idempotentność batcha — SHA-256 pliku, atomowy rename, pamięć wierszy

- **Status:** przyjęta lokalnie; wersja chmurowa świadomie niezaimplementowana
- **Data:** 2026-10-02
- **Etap:** 5 (batch lokalnie)

## Kontekst

Ten sam plik bywa wgrywany dwa razy, a eksporty się nakładają („ostatnie 7 dni" codziennie).
Load przerwany w połowie nie może zostawić połowy danych.

## Rozważone warianty

| Wariant | Za | Przeciw |
| --- | --- | --- |
| Rejestr nazw załadowanych plików | proste | ten sam plik pod inną nazwą ładuje się drugi raz; nie chroni przed nakładaniem |
| Dopisywanie do wspólnych plików z kontrolą duplikatów | jedno miejsce danych | przerwany load zostawia częściowy zapis bez możliwości wycofania |
| **SHA-256 treści + katalog roboczy + `os.replace` + pamięć wierszy** | no-op dla tej samej treści; „wszystko albo nic" dla pliku; nakładające się wiersze rozpoznane jako powtórki | każdy load czyta identyfikatory wcześniejszych zdarzeń |

## Decyzja

Load = 16 znaków SHA-256 treści. Wynik w `staging/`, manifest `_load.json` zapisany jako
ostatni, katalog przeniesiony do `loads/<load_id>/` jednym `os.replace`. Pamięć transakcji
zasilana z wcześniejszych loadów — lokalny odpowiednik `MERGE ... ON transaction_id`.

## Konsekwencje

- Ponowny load to no-op; przerwany load sprząta katalog roboczy (także przy Ctrl+C);
  przegrany wyścig dwóch loadów tego samego pliku kończy się statusem `skipped`.
- **Loader nie jest wdrożony w chmurze.** Atomowa zmiana nazwy katalogu nie zachodzi na
  buckecie GCS zamontowanym w Cloud Run. Wersja chmurowa to inny mechanizm zapisu: load job
  do tabeli tymczasowej i `MERGE` po `transaction_id`. Walidacja jest wspólna i gotowa.
