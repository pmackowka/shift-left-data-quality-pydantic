# ============================================================================
# Obraz pipeline'u: usługa ingest (Cloud Run) i - od etapu 5 - loader batchowy
# (Cloud Run Job). Jeden obraz, różne polecenie startowe.
#
# Dockerfile leży w korzeniu repozytorium, a nie w apps/pipeline/, bo budowanie
# potrzebuje całego workspace'u uv: uv.lock jest wspólny, a dq-pipeline zależy
# od dq-contracts z sąsiedniego katalogu.
#
# Budowanie dwuetapowe (multi-stage): etap `build` ma uv i kompiluje środowisko,
# etap końcowy dostaje tylko gotowe .venv. W obrazie produkcyjnym nie ma uv, cache
# pobierania ani kodu źródłowego pakietów spoza pipeline'u - w tym generatora danych.
# ============================================================================

# Wersja uv przypięta. `latest` oznaczałoby, że ten sam commit buduje się dziś
# inaczej niż za miesiąc.
FROM ghcr.io/astral-sh/uv:0.11 AS uv

FROM python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app

# UV_COMPILE_BYTECODE: kompilacja .pyc przy budowaniu, a nie przy pierwszym imporcie.
#   Na Cloud Run skraca to zimny start - kontener nie traci czasu na kompilację
#   w chwili, gdy czeka na niego pierwsze żądanie.
# UV_LINK_MODE=copy: pliki z cache kopiowane, nie linkowane - cache nie trafia do
#   obrazu końcowego, więc linki prowadziłyby donikąd.
# UV_PYTHON_DOWNLOADS=never: używamy Pythona z obrazu bazowego, uv nie pobiera własnego.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Warstwa zależności osobno od kodu. Docker cache'uje warstwy po kolei, więc
# zmiana w kodzie pipeline'u nie unieważnia warstwy z FastAPI i pydantic - przy
# kolejnym buildzie instalacja zależności trwa zero sekund.
# Wszystkie pliki pyproject.toml są potrzebne, bo uv musi zrozumieć cały workspace,
# nawet jeśli instaluje jeden pakiet.
COPY pyproject.toml uv.lock ./
COPY packages/dq-contracts/pyproject.toml packages/dq-contracts/
COPY packages/dq-datagen/pyproject.toml packages/dq-datagen/
COPY apps/pipeline/pyproject.toml apps/pipeline/
# --frozen: uv.lock jest prawdą, zakaz jego aktualizacji przy budowaniu.
# --no-dev: bez ruffa, mypy i pytest.
# --package dq-pipeline: tylko pipeline i jego zależności - generator zostaje poza.
# --no-install-workspace: na razie same zależności zewnętrzne, kod dojdzie niżej.
RUN uv sync --frozen --no-dev --package dq-pipeline --no-install-workspace

COPY packages/dq-contracts packages/dq-contracts
COPY apps/pipeline apps/pipeline
# --no-editable: pakiety workspace'u instalowane do .venv jako zwykłe pakiety,
# a nie odnośniki do /app/packages. Dzięki temu etap końcowy kopiuje samo .venv.
RUN uv sync --frozen --no-dev --package dq-pipeline --no-editable

FROM python:3.12-slim
# Użytkownik bez uprawnień roota. Usługa niczego nie instaluje w czasie działania,
# więc root dawałby wyłącznie większe pole rażenia przy ewentualnej podatności.
RUN useradd --create-home --uid 10001 app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
# Katalog roboczy należy do użytkownika `app` - lokalny sink zapisuje względem niego.
WORKDIR /home/app
USER app
# Port informacyjny. Cloud Run i tak przekazuje właściwy przez $PORT.
EXPOSE 8080
CMD ["dq-ingest"]
