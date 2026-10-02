# ============================================================================
# Makefile jest tu jedynym interfejsem do projektu.
#
# Zasada: każdy punkt Definition of Done ma dokładnie jedną komendę. Osoba,
# która wchodzi do repozytorium pierwszy raz, nie musi znać uv, ruffa ani
# pytest - ma znać `make help`. To samo dotyczy CI: pipeline odpala te same
# cele, więc "u mnie działa" i "CI jest zielone" znaczą to samo.
#
# Uwaga składniowa: komentarze w tym pliku stoją NAD celami, nigdy wewnątrz
# bloku poleceń. Linia wcięta tabem jest przekazywana do shella - komentarz
# w tym miejscu wypisywałby się na ekran przy każdym uruchomieniu celu.
# ============================================================================

# Domyślny cel przy gołym `make`. Bez tego make wykonałby pierwszy cel w pliku,
# czyli tutaj help - ale poleganie na kolejności jest kruche, bo psuje się przy
# pierwszym przestawieniu bloków.
.DEFAULT_GOAL := help

# Wymuszamy bash. make domyślnie używa /bin/sh, gdzie nie ma m.in. pipefail
# ani podstawień, których używamy w celu `help`.
SHELL := /bin/bash

# .PHONY = te nazwy nie są plikami na dysku. Gdyby w repo pojawił się plik
# o nazwie `test`, make uznałby cel za aktualny i nie zrobiłby nic.
.PHONY: help setup lint format typecheck test check gen image local-stream local-stream-down batch-local bench clean

# Help generuje się sam z komentarzy `## ...` przy celach. Dzięki temu nie
# istnieje druga, ręcznie utrzymywana lista celów, która rozjechałaby się
# z rzeczywistością przy pierwszym nowym celu.
# $(MAKEFILE_LIST) to lista wczytanych plików make - tutaj po prostu ten plik.
# Wiodący @ tłumi wypisanie samego polecenia, więc widać wyłącznie jego wynik.
help: ## Lista dostępnych komend
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# uv sam pobiera interpreter, więc wersja Pythona nie zależy od tego, co akurat
# jest zainstalowane w systemie. --all-packages instaluje wszystkie pakiety
# workspace'a w trybie edytowalnym; bez tej flagi uv zsynchronizowałby tylko
# korzeń i importy w testach padłyby na ModuleNotFoundError.
setup: ## Instaluje interpreter 3.12, tworzy .venv i synchronizuje workspace
	uv python install 3.12
	uv sync --all-packages

# `uv run` uruchamia polecenie w środowisku projektu - nie trzeba aktywować
# .venv ręcznie ani pamiętać, czy narzędzie jest w PATH.
lint: ## ruff check
	uv run ruff check .

# --fix naprawia to, co da się naprawić bezpiecznie: kolejność importów,
# usunięcie nieużywanych, modernizację składni do target-version.
format: ## ruff format + autofix importów
	uv run ruff format .
	uv run ruff check --fix .

# Bez argumentów - listę plików do sprawdzenia mypy bierze z `files`
# w pyproject.toml, więc nie da się przypadkiem sprawdzić węższego zakresu
# lokalnie niż w CI.
typecheck: ## mypy strict
	uv run mypy

# Benchmarki (marker `slow`) mierzą czas i na współdzielonym runnerze CI dawałyby
# wyniki losowe, więc domyślnie ich nie ruszamy. Uruchamia się je świadomie:
# `uv run pytest -m slow`.
test: ## pytest z pokryciem, bez testów wydajnościowych
	uv run pytest -m "not slow" --cov --cov-report=term-missing

# Cel złożony: make wykona zależności po kolei i przerwie na pierwszym błędzie.
# Kolejność jest celowa - od najszybszego do najwolniejszego, żeby literówka
# nie czekała na wynik testów.
check: lint typecheck test ## Pełna bramka jakości, to samo co CI

# Parametry generatora. `?=` przypisuje wartość tylko wtedy, gdy zmienna nie przyszła
# z linii poleceń ani ze środowiska - więc `make gen N=100000 ERR=0.05` nadpisuje
# domyślne, a gołe `make gen` daje zawsze ten sam zbiór (stałe ziarno).
N ?= 1000
ERR ?= 0.2
SEED ?= 42
OUT ?= data/events.jsonl

# Katalog data/ jest w .gitignore - wygenerowane dane nie trafiają do repozytorium.
# Podsumowanie (liczba rekordów na rodzaj błędu) idzie na stderr, dane do pliku.
gen: ## Generuje N zdarzeń z odsetkiem błędnych ERR do OUT (NDJSON), np. make gen N=1000 ERR=0.2
	uv run dq-gen --count $(N) --error-rate $(ERR) --seed $(SEED) --output $(OUT)

# Obraz pipeline'u - ten sam, który poszedłby do Artifact Registry i na Cloud Run.
# Tag `local`, bo lokalny build nie ma numeru wersji; w chmurze tagiem byłby SHA commita.
image: ## Buduje obraz Dockera usługi ingest (dq-pipeline:local)
	docker build -t dq-pipeline:local .

# Streaming lokalnie, od zera: świeży emulator (stan Pub/Sub żyje w pamięci kontenera,
# więc `down` go czyści), pusty katalog danych, obraz zbudowany z bieżącego kodu.
# DQ_UID/DQ_GID: kontener ingest pisze do bind mounta jako użytkownik hosta - na Linuksie
#   inaczej nie miałby prawa zapisu do ./data/stream.
# PUBSUB_EMULATOR_HOST przełącza klienta Google na emulator; bez niej skrypt próbowałby
#   połączyć się z prawdziwym Pub/Sub.
# Środowisko zostaje sprzątnięte na końcu tylko przy sukcesie - po porażce kontenery
# zostają, żeby dało się zajrzeć w `docker compose logs ingest`. Sprzątanie ręczne:
# `make local-stream-down`.
local-stream: ## Streaming end-to-end na emulatorze Pub/Sub (Docker), sprawdzony wyrocznią
	docker compose down --volumes --remove-orphans
	rm -rf data/stream && mkdir -p data/stream
	DQ_UID=$$(id -u) DQ_GID=$$(id -g) docker compose up --detach --build --wait
	PUBSUB_EMULATOR_HOST=localhost:8085 DQ_UID=$$(id -u) DQ_GID=$$(id -g) uv run python scripts/local_stream.py
	docker compose down --volumes

local-stream-down: ## Zatrzymuje i usuwa kontenery z make local-stream
	docker compose down --volumes --remove-orphans

# Batch od zera: pusty katalog wyników, trzy loady (pierwszy, ten sam plik ponownie,
# plik nakładający się) i porównanie z odpowiedzią wzorcową generatora. Bez Dockera -
# loader czyta i pisze lokalne pliki. BATCH_N zmienia rozmiar pierwszego pliku.
BATCH_N ?= 10000
batch-local: ## Batch end-to-end: idempotentne loady NDJSON sprawdzone wyrocznią
	rm -rf data/batch
	BATCH_N=$(BATCH_N) uv run python scripts/batch_local.py

# Benchmark walidacji: model_validate_json vs TypeAdapter vs model_validate vs
# model_construct na tym samym zbiorze. Wynik zależy od maszyny - liczy się proporcja
# między wariantami, nie wartości bezwzględne. Nie idzie do CI: współdzielony runner
# dawałby losowe czasy, a pomiar bez stabilnego środowiska jest szumem.
BENCH_N ?= 100000
bench: ## Benchmark walidacji na BENCH_N rekordach (domyślnie 100 tys.)
	uv run python scripts/bench.py --count $(BENCH_N)

# -prune zatrzymuje schodzenie w głąb usuwanego katalogu, a `+` grupuje ścieżki
# w jedno wywołanie rm zamiast jednego wywołania na każdy katalog.
clean: ## Usuwa cache narzędzi i artefakty lokalne
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
