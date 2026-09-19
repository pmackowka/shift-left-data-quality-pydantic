"""Test dymny etapu 1: workspace składa się, a oba pakiety są importowalne.

Test dymny (smoke test) sprawdza, czy system w ogóle wstaje - nie czy liczy
poprawnie. Tutaj weryfikuje rzecz, która w monorepo psuje się najczęściej:
czy `uv sync --all-packages` faktycznie zainstalował oba pakiety workspace'a
i czy układ src-layout jest poprawnie opisany w pyproject.toml obu pakietów.

Bez tego testu błąd w konfiguracji pakietu wyszedłby dopiero w etapie 2, przy
pierwszym imporcie modelu - i wyglądałby na błąd modelu, a nie konfiguracji.
"""

import dq_contracts
import dq_datagen


def test_packages_are_importable() -> None:
    # Sprawdzamy wersję, a nie sam import, bo goły import przeszedłby też wtedy,
    # gdyby Python znalazł pusty katalog jako pakiet namespace'owy.
    assert dq_contracts.__version__ == "0.1.0"
    assert dq_datagen.__version__ == "0.1.0"
