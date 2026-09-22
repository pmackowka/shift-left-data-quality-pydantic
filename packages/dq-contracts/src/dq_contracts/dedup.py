"""Wykrywanie duplikatów identyfikatora transakcji.

Duplikat jest jedyną regułą kontraktu, której nie da się sprawdzić na pojedynczym
rekordzie - wymaga pamięci o tym, co już przeszło. Dlatego mieszka poza modelem
pydantic i kończy się `BusinessRuleError`, a nie `ValidationError`.
"""

from dq_contracts.errors import DuplicateTransactionError


class TransactionRegistry:
    """Pamięć identyfikatorów transakcji w obrębie jednego przebiegu.

    Zakres pamięci jest tu świadomie wąski: jeden proces, jeden przebieg. To wystarcza
    dla partii w trybie batch i dla pojedynczej instancji konsumenta, ale NIE jest
    deduplikacją trwałą - restart procesu czyści stan, a druga instancja Cloud Run ma
    własny, niezależny zbiór.

    Trwała deduplikacja w tym projekcie odbywa się warstwę niżej, w hurtowni:
    zapis do BigQuery idzie przez MERGE po `transaction_id`, więc powtórzona wiadomość
    nadpisuje wiersz zamiast dokładać drugi. Ten rejestr pełni inną rolę - wyłapuje
    duplikat wcześnie, żeby w raporcie z przebiegu było widać, ile ich było i skąd
    pochodziły. Bez niego duplikaty znikałyby po cichu w MERGE i nikt by ich nie policzył.

    Świadomie odrzucony wariant: filtr Blooma zamiast zbioru. Oszczędziłby pamięci przy
    milionach identyfikatorów, ale kosztem fałszywych trafień - a fałszywe trafienie
    oznacza tu odrzucenie poprawnej transakcji. Przy skali tego projektu (partie rzędu
    100 tys. rekordów) zbiór mieści się w pamięci bez trudu, więc nie ma czego kupować
    za tę cenę.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._duplicates: int = 0

    def register(self, transaction_id: str) -> None:
        """Zapamiętuje identyfikator; przy powtórzeniu rzuca `DuplicateTransactionError`.

        Licznik duplikatów rośnie przy każdym powtórzeniu, także wielokrotnym - trzecie
        wystąpienie tego samego identyfikatora to drugi duplikat, nie pierwszy.
        """
        if transaction_id in self._seen:
            self._duplicates += 1
            raise DuplicateTransactionError(transaction_id)
        self._seen.add(transaction_id)

    def __contains__(self, transaction_id: str) -> bool:
        return transaction_id in self._seen

    def __len__(self) -> int:
        """Liczba unikalnych identyfikatorów, nie liczba wywołań `register`."""
        return len(self._seen)

    @property
    def duplicate_count(self) -> int:
        """Ile razy `register` odrzucił powtórzenie - wprost do raportu z przebiegu."""
        return self._duplicates
