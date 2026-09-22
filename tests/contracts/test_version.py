"""Spójność wersji kontraktu między kodem a metadanymi pakietu."""

from importlib.metadata import version

import dq_contracts
from dq_contracts.version import CONTRACT_VERSION


def test_version_matches_package_metadata() -> None:
    """Numer w kodzie musi się zgadzać z numerem w pyproject.toml.

    Wersja jest zduplikowana świadomie (kod runtime'owy nie powinien czytać metadanych
    przy każdym imporcie), więc potrzebny jest test, który tę duplikację pilnuje.
    Bez niego podbicie wersji w jednym miejscu przeszłoby niezauważone, a rekordy
    w hurtowni miałyby stempel nieistniejącej wersji kontraktu.
    """
    assert CONTRACT_VERSION == version("dq-contracts")


def test_package_exposes_version() -> None:
    assert dq_contracts.__version__ == CONTRACT_VERSION
