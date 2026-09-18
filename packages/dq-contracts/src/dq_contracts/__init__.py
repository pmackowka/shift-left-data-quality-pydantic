"""Kontrakty danych dla zdarzen ecommerce.

Ten pakiet jest jedynym zrodlem prawdy o schemacie. Oba pipeline'y (streaming
i batch) oraz generator schematow BigQuery importuja stad te same modele.
Wersja pakietu zmienia sie zgodnie z SemVer: zmiana lamiaca kontrakt to major.
"""

__version__ = "0.1.0"
