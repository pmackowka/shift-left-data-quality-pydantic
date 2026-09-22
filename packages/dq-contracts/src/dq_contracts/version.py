"""Wersja kontraktu jako osobny moduł.

Dlaczego nie w `__init__.py`: `quarantine.py` i `events.py` potrzebują numeru wersji,
a `__init__.py` importuje oba te moduły, żeby wystawić publiczne API. Import wersji
z `__init__` tworzyłby cykl, który działa tylko dopóki nikt nie przestawi kolejności
linii w `__init__.py`. Osobny moduł bez żadnych zależności usuwa problem u źródła.
"""

from typing import Final

CONTRACT_VERSION: Final[str] = "0.1.0"
"""Wersja kontraktu w formacie SemVer.

Musi być zgodna z `version` w `packages/dq-contracts/pyproject.toml` - pilnuje tego
test `test_version_matches_package_metadata`. Duplikat istnieje świadomie: odczyt
metadanych pakietu przy każdym imporcie kosztuje, a wersja trafia do każdego rekordu.
"""
