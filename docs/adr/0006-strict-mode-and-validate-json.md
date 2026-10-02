# ADR 0006: tryb strict i `model_validate_json` na ścieżce produkcyjnej

- **Status:** przyjęta
- **Data:** 2026-10-02 (pomiar), decyzja o strict z etapu 2
- **Etap:** 2 (kontrakt), potwierdzona benchmarkiem z etapu 5

## Kontekst

Pydantic pozwala walidować w trybie lax (koercja typów) albo strict, a dane z JSON-a
zamieniać w model na kilka sposobów. Decyzja wpływa jednocześnie na poprawność i koszt.

## Rozważone warianty (pomiar: `make bench`, 100 tys. rekordów)

| Wariant | µs / rekord | Przepuszczone zepsute (z 1000) | Uwagi |
| --- | ---: | ---: | --- |
| **`model_validate_json`, strict** | 6,5 | 125 | 125 to duplikaty — schemat ich nie widzi, łapie je pamięć transakcji |
| `json.loads` + `model_validate`, strict | — | — | odrzuca każdy rekord: tekst w polach `Decimal`, `UUID`, `datetime` |
| `json.loads` + `model_validate(strict=False)` | 8,5 | 250 | wolniejszy i przepuszcza `"2"` zamiast `2` |
| `json.loads` + `model_construct` | 3,4 | 1000 | brak walidacji; `value` zostaje `str`, pozycje — słownikami |
| `TypeAdapter(list[...])` na całej partii | 6,9 | 0 | jeden zły rekord odrzuca całą partię, w tym poprawne |

## Decyzja

Kontrakt w trybie strict (`ConfigDict(strict=True, extra="forbid")`, powtórzone w modelach
zagnieżdżonych). Wszystkie wejścia z zewnątrz walidowane przez `model_validate_json`
rekord po rekordzie. `model_construct` tylko dla danych zwalidowanych tym samym kontraktem
chwilę wcześniej.

## Konsekwencje

- Walidacja kosztuje ok. 4 µs na rekord ponad samo parsowanie — kilka sekund CPU dziennie
  przy milionie zdarzeń.
- Generator buduje zdarzenia z natywnych typów (`model_validate` na obiektach Pythona),
  a pipeline zawsze czyta JSON — dwa tryby strict są świadomie rozdzielone.
- Pomiar z jednej maszyny (MacBook arm64); liczą się proporcje, nie wartości bezwzględne.
