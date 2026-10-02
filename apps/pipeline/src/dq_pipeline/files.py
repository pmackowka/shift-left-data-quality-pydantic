"""Odczyt plików NDJSON - wspólny dla publishera i loadera batchowego.

Osobny moduł, a nie funkcja w `pubsub`, żeby loader batchowy nie importował klienta
Pub/Sub (i całego gRPC) tylko po to, żeby przeczytać linie z pliku.
"""

from collections.abc import Iterator
from pathlib import Path


def read_ndjson(path: Path) -> Iterator[bytes]:
    """Linie pliku NDJSON jako bajty; puste linie pomijamy jako formatowanie, nie dane.

    Bajty, a nie tekst: dekodowanie zostawiamy walidatorowi, który przy błędnym kodowaniu
    zapisze rekord w kwarantannie zamiast przerwać cały plik na `UnicodeDecodeError`.
    """
    with path.open("rb") as source:
        for raw in source:
            line = raw.rstrip(b"\r\n")
            if line:
                yield line
