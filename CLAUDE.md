# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Czym jest ten projekt

Demonstracja podejścia shift-left do jakości danych: jeden wersjonowany kontrakt pydantic v2
waliduje zdarzenia ecommerce w dwóch pipeline'ach (streaming przez Pub/Sub → Cloud Run → BigQuery
oraz batch), a rekordy łamiące kontrakt trafiają do kwarantanny z powodem odrzucenia.

Projekt ma dwa równorzędne cele: nauczyć właściciela repo pydantic v2 w praktyce oraz być
publicznym portfolio czytelnym dla rekrutera i inżyniera. Oba cele wpływają na decyzje — kod
ma być produkcyjnej jakości, a mechanizmy wyjaśnione, nie tylko użyte.

## Komendy

```bash
make setup                  # instaluje Pythona 3.12, tworzy .venv, synchronizuje workspace uv
make check                  # lint + typy + testy, dokładnie ta sama bramka co CI
make lint                   # ruff check
make format                 # ruff format + autofix importów
make typecheck              # mypy strict z pluginem pydantic
make test                   # pytest z pokryciem, bez testów oznaczonych `slow`
make help                   # pełna lista celów
```

Pojedynczy test lub plik:

```bash
uv run pytest tests/contracts/test_purchase_event.py::test_value_must_match_line_items -x
uv run pytest -k "currency" -vv
uv run pytest -m slow        # testy wydajnościowe, domyślnie pomijane
```

Każda zmiana w zależnościach wymaga `uv sync --all-packages`; CI leci na `--frozen`, więc
`uv.lock` musi być w commicie.

## Architektura

### Zasada nadrzędna: jedno źródło prawdy

`packages/dq-contracts` to jedyne miejsce, w którym opisany jest schemat danych. Publisher,
usługa ingest, loader batchowy i generator schematów BigQuery importują ten sam pakiet.
Wymusza to `uv workspace` — zależności rozwiązują się do kodu z dysku (`{ workspace = true }`),
nie do wersji z PyPI. Schemat nie może powstać w dwóch miejscach, bo natychmiast się rozjedzie.

Pakiet `dq-contracts` nie ma i nie może mieć zależności od GCP — to czysta warstwa domenowa.
Kod chmurowy żyje w `apps/`.

### Przepływ danych

```
generator → walidacja #1 → Pub/Sub topic → push subscription → Cloud Run (ingest)
                ↓ odrzucone                                          ↓ walidacja #2
         BigQuery: quarantine                          BigQuery: events | quarantine
                                                                  ↓ nieprzetwarzalne
                                                          Pub/Sub dead-letter topic
```

Walidacja wykonuje się dwa razy świadomie: przed publikacją (producent nie zaśmieca tematu)
i po odbiorze (konsument nie ufa producentowi). To sedno shift-left, a nie zdublowana praca.

### Sink jako abstrakcja

Zapis danych idzie przez interfejs `Sink` z dwiema implementacjami: lokalną (JSONL + DuckDB
do raportów) i BigQuery. Dzięki temu cały streaming i batch uruchamiają się bez konta GCP,
tym samym kodem ścieżki biznesowej.

### Schematy BigQuery generowane z modeli

Schematy tabel powstają z modeli pydantic (`make schemas`), commitują się do
`infra/terraform/schemas/`, a Terraform czyta je przez `file()`. CI pilnuje driftu — jeśli
wygenerowany schemat różni się od zacommitowanego, build pada. Nigdy nie przepisuj schematu
tabeli ręcznie.

## Etapy projektu

Etapy numerujemy od 1. Realizacja idzie w tej kolejności, każdy etap kończy się działającą
komendą — jeśli czegoś nie da się uruchomić jedną komendą, etap nie jest zamknięty.
Pełny opis każdego etapu wraz z uzasadnieniem jest w README, sekcja „Etapy prac".

1. **Etap 1 — szkielet** (gotowy): uv workspace, ruff, mypy strict, pytest, Makefile, CI.
2. **Etap 2 — modele pydantic + testy**: `dq_contracts`, każda reguła walidacji ma test
   pozytywny i negatywny.
3. **Etap 3 — generator**: `dq_datagen`, parametryzowane wstrzykiwanie błędów, pokrywa
   każdą regułę z etapu 2.
4. **Etap 4 — streaming lokalnie**: emulator Pub/Sub w Dockerze, `make local-stream`.
5. **Etap 5 — batch lokalnie**: raport OK/kwarantanna, idempotentność, benchmark 100k rekordów
   (`model_validate` vs `model_construct` vs `TypeAdapter`).
6. **Etap 6 — Terraform**: kod infrastruktury łącznie z tworzeniem projektu GCP.
7. **Etap 7 — README**: pełna dokumentacja produktowa.

### Topologia wdrożenia (docelowa, nieuruchomiona)

`dq-contracts` to biblioteka wbudowana w obraz, nie osobna usługa. Usługa ingest idzie na
Cloud Run (`europe-central2`), obraz do Artifact Registry, loader batchowy jako Cloud Run Job
na tym samym obrazie, dane do BigQuery (`events`, `quarantine`), stan Terraforma docelowo do
bucketa GCS z wersjonowaniem. Pierwszy `apply` jest dwuetapowy: rejestr i zasoby, potem push
obrazu, na końcu usługa Cloud Run — bo jej definicja wskazuje na konkretny tag obrazu.
Szczegóły i diagramy: README, sekcja „Jak wyglądałoby wdrożenie".

## Reguły walidacji do pokrycia

Każda z poniższych musi mieć test pozytywny i negatywny oraz odpowiadający jej wariant
w generatorze:

- wartość transakcji niezgodna z sumą pozycji (cross-field, `model_validator`)
- znacznik czasu z przyszłości
- ujemna lub zerowa cena albo ilość
- waluta spoza dozwolonego zbioru
- duplikat identyfikatora transakcji
- brak pola wymaganego lub pole nadmiarowe (`extra="forbid"`)
- string tam, gdzie ma być liczba — różnica między trybem strict a lax

## Ustalenia, których nie widać w kodzie

- **Infrastruktura powstaje „na sucho".** Terraform ma być kompletny i zwalidowany, ale
  właściciel repo świadomie nie uruchamia `apply` ani nie zakłada projektu GCP. Walidacja bez
  konta: `terraform fmt -check`, `init -backend=false`, `validate` — przez obraz
  `hashicorp/terraform` w Dockerze, bo Terraform nie jest zainstalowany w systemie.
- **Projekt GCP też tworzy Terraform**, nie klikanie w konsoli. Nad każdym blokiem komentarz
  po polsku wyjaśniający, co i w jakiej kolejności trzeba zrobić, żeby to odpalić.
- W README punkt „Demo na GCP" zostaje jawnie oznaczony jako niewykonany. Nie deklaruj, że
  pipeline przeszedł na prawdziwym GCP.
- Środowisko lokalne: brak Javy, więc emulator Pub/Sub idzie przez Dockera
  (`google-cloud-cli:emulators`), nie przez `gcloud components`.
- Emulator BigQuery (`goccy/bigquery-emulator`) został rozważony i odrzucony na rzecz
  lokalnego sinka JSONL + DuckDB; uzasadnienie trafia do ADR.

## Konwencje

- **Kod, nazwy, commity, opis repozytorium i topics po angielsku. Komentarze, docstringi,
  README i cała dokumentacja po polsku.** README jest wyłącznie polskie — to świadoma decyzja
  właściciela repo, nie przeoczenie.
- Commity w konwencji Conventional Commits, tytuł do 72 znaków.
- Nigdy nie commituj na `main`. Każda zmiana idzie przez branch (`feat/`, `fix/`, `chore/`,
  `docs/` + kebab-case) i pull request przez `gh pr create`, z opisem: co się zmienia, dlaczego,
  jak zweryfikować. Przed otwarciem PR uruchom `make check` i wklej wynik do opisu.
- Po scaleniu: `gh pr merge --squash --delete-branch`.
- **Komentarze piszemy gęsto, ale nierównomiernie.** W plikach konfiguracyjnych (TOML, YAML,
  Makefile, Terraform) komentujemy praktycznie każdą decyzję, łącznie z tym, co dana opcja
  robi — to wiedza, której nie da się odczytać z samego kodu, a projekt ma uczyć. W kodzie
  Pythona komentujemy decyzje i mechanizmy pydantic, nie składnię języka; docstring modułu
  wyjaśnia rolę pliku w całości, a nie parafrazuje nazwy klas.
- W Makefile komentarze stoją NAD celem. Linia wcięta tabem trafia do shella i wypisuje się
  na ekran przy każdym uruchomieniu.
- Kod uruchamiaj, zanim powiesz, że działa.
