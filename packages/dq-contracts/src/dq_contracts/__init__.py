"""Kontrakty danych dla zdarzeń ecommerce.

Ten pakiet jest jedynym źródłem prawdy o schemacie. Oba pipeline'y (streaming
i batch) oraz generator schematów BigQuery importują stąd te same modele.

Dlaczego to ma znaczenie: gdy schemat istnieje w dwóch miejscach - raz jako
model w kodzie, raz jako definicja tabeli w hurtowni - rozjeżdża się przy
pierwszej zmianie, a rozjazd wychodzi na jaw dopiero przy błędnym raporcie.
Tutaj definicja tabeli BigQuery powstaje z modelu, więc rozjazd jest niemożliwy.

Wersja pakietu zmienia się zgodnie z SemVer; zmiana łamiąca kontrakt to major.
Numer wersji podróżuje w kopercie wiadomości, dzięki czemu po stronie hurtowni
widać, który kontrakt wyprodukował dany rekord.
"""

# Wersja jest tu zduplikowana względem pyproject.toml świadomie: kod runtime'owy
# musi znać swoją wersję, żeby wpisać ją do koperty zdarzenia, a czytanie
# metadanych pakietu przy każdym imporcie kosztuje. Spójność obu miejsc pilnuje
# test w etapie 2.
__version__ = "0.1.0"
