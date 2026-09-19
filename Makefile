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
.PHONY: help setup lint format typecheck test check clean

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

# -prune zatrzymuje schodzenie w głąb usuwanego katalogu, a `+` grupuje ścieżki
# w jedno wywołanie rm zamiast jednego wywołania na każdy katalog.
clean: ## Usuwa cache narzędzi i artefakty lokalne
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
