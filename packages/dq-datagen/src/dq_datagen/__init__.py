"""Generator syntetycznych zdarzeń ecommerce z kontrolowanym wstrzykiwaniem błędów.

Generator ma jedno zadanie, którego nie widać na pierwszy rzut oka: musi umieć
wyprodukować KAŻDY przypadek błędu z listy reguł walidacji. Bez tego nie da się
pokazać kwarantanny w działaniu ani zmierzyć, jak pipeline zachowuje się przy
realnym odsetku śmieci na wejściu.

Dane poprawne buduje na modelach z dq_contracts, a błędne - psując ich postać JSON-ową,
bo model z definicji nie pozwoli stworzyć obiektu łamiącego kontrakt.

- `builder`   - jedno poprawne zdarzenie z losowych, ale wiarygodnych danych,
- `faults`    - katalog błędów z odpowiedzią wzorcową (oczekiwany powód kwarantanny),
- `generator` - plan błędów i strumień rekordów dla zadanych parametrów,
- `cli`       - polecenie `dq-gen`, zapis do NDJSON.
"""

from dq_datagen.builder import build_valid_event
from dq_datagen.faults import FAULT_CATALOG, FaultContext, FaultKind, FaultSpec, inject_fault
from dq_datagen.generator import GeneratedRecord, GeneratorConfig, generate

__version__ = "0.1.0"

__all__ = [
    "FAULT_CATALOG",
    "FaultContext",
    "FaultKind",
    "FaultSpec",
    "GeneratedRecord",
    "GeneratorConfig",
    "__version__",
    "build_valid_event",
    "generate",
    "inject_fault",
]
