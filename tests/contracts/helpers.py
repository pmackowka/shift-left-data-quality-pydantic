"""Pomocniki testowe wspólne dla testów kontraktu."""

import json
from typing import Any


def as_json(payload: dict[str, Any]) -> str:
    """Serializuje payload do JSON-a, czyli do postaci, w jakiej dane realnie przychodzą.

    Testy kontraktu idą przez `model_validate_json`, a nie `model_validate`, bo tak
    wygląda ścieżka produkcyjna: z Pub/Sub przychodzą bajty, z pliku NDJSON linie tekstu.
    Walidowanie słownika z natywnymi obiektami Pythona sprawdzałoby ścieżkę, której
    w tym pipelinie nie ma - i przy `strict=True` dawałoby inne wyniki, bo tryb strict
    znaczy co innego dla JSON-a, a co innego dla obiektów Pythona.
    """
    return json.dumps(payload)
