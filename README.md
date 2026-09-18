# Shift-left data quality z pydantic

Walidacja zdarzeń ecommerce **zanim** trafią do hurtowni. Jeden wersjonowany kontrakt
pydantic pilnuje dwóch pipeline'ów naraz — streamingowego i batchowego — na Google Cloud.

> **Status: projekt w budowie.** Repozytorium powstaje etapami. Etap 0 (szkielet i narzędzia)
> jest gotowy. Dalej: kontrakty danych, generator, pipeline'y, Terraform i pełne README
> z diagramem architektury, regułami walidacji, benchmarkiem i szacunkiem kosztów.

## Dlaczego shift-left

Zły rekord wykryty w hurtowni kosztuje wielokrotnie więcej niż ten sam rekord odrzucony
u źródła: zdążył już zasilić raporty, modele atrybucji i decyzje zakupowe. Shift-left
przesuwa walidację do momentu powstania zdarzenia — do kwarantanny trafia pojedynczy
rekord z powodem odrzucenia, a nie cała partia po fakcie.

## Szybki start

```bash
make setup   # instaluje Pythona 3.12, tworzy .venv, synchronizuje workspace uv
make check   # ruff + mypy strict + pytest — dokładnie ta sama bramka, którą odpala CI
```

Pełna lista komend: `make help`.

## Struktura repozytorium

| Ścieżka | Do czego służy |
| --- | --- |
| `packages/dq-contracts/` | Kontrakt danych: modele pydantic, mapowanie błędów na kwarantannę, generowanie schematów BigQuery. Jedyne źródło prawdy, wersjonowane wg SemVer. |
| `packages/dq-datagen/` | Generator syntetycznych zdarzeń z kontrolowanym wstrzykiwaniem błędów. |
| `apps/` | Publisher, usługa ingest na Cloud Run, loader batchowy. |
| `infra/terraform/` | Pub/Sub, BigQuery, Cloud Run, IAM. Schematy tabel generowane z modeli pydantic, nie przepisywane ręcznie. |
| `tests/` | Test pozytywny i negatywny dla każdej reguły walidacji. |
| `docs/adr/` | Decyzje architektoniczne i warianty odrzucone. |

## Konwencje

- Kod, nazwy, commity i opis repozytorium po angielsku.
- Komentarze, docstringi i dokumentacja po polsku.
- Każda zmiana przez branch i pull request, CI sprawdza lint, typy i testy.

## Licencja

MIT — zobacz [LICENSE](LICENSE).
