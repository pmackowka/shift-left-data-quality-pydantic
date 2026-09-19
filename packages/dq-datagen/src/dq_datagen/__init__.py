"""Generator syntetycznych zdarzeń ecommerce z kontrolowanym wstrzykiwaniem błędów.

Generator ma jedno zadanie, którego nie widać na pierwszy rzut oka: musi umieć
wyprodukować KAŻDY przypadek błędu z listy reguł walidacji. Bez tego nie da się
pokazać kwarantanny w działaniu ani zmierzyć, jak pipeline zachowuje się przy
realnym odsetku śmieci na wejściu.

Dane poprawne buduje na modelach z dq_contracts, a błędne - omijając walidację,
bo model z definicji nie pozwoli stworzyć obiektu łamiącego kontrakt.
"""

__version__ = "0.1.0"
