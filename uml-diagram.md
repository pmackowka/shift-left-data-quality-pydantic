# Diagram sekwencji UML: zdarzenie → walidacja → BigQuery albo kwarantanna

Pipeline streamingowy: kto wywołuje kogo i co się dzieje przy błędzie walidacji, powtórce i awarii zapisu. Pipeline batchowy i reguły kontraktu: [README](README.md).

```mermaid
sequenceDiagram
    autonumber
    participant GEN as Producent zdarzeń
    participant PUB as Publisher
    participant T as Pub/Sub purchase-events
    participant SUB as Subskrypcja push
    participant RUN as Cloud Run dq-ingest
    participant EV as BigQuery events
    participant Q as BigQuery quarantine
    participant DLQ as Pub/Sub dead-letter

    GEN->>PUB: zdarzenie purchase
    PUB->>PUB: walidacja u źródła (kontrakt pydantic)
    alt łamie kontrakt
        PUB->>Q: rekord + powód odrzucenia
    else poprawny
        PUB->>T: publish
        T->>SUB: dostarczenie (co najmniej raz)
        SUB->>RUN: HTTP POST z tokenem OIDC
        RUN->>RUN: ten sam kontrakt + odcisk treści transakcji
        alt nowy i poprawny
            RUN->>EV: zapis z insertId
            RUN-->>SUB: 204 (ack)
        else powtórka (ta sama treść)
            RUN-->>SUB: 204 bez zapisu
        else błąd walidacji albo duplikat (inna treść)
            RUN->>Q: kwarantanna z powodem
            RUN-->>SUB: 204 (ack)
        else awaria zapisu
            RUN-->>SUB: 5xx
            SUB->>RUN: ponowienie
            Note over SUB,DLQ: po 5 nieudanych próbach → dead-letter, redrive po usunięciu awarii
        end
    end
```

| Krok | Kto | Po co |
|---|---|---|
| 2–3 | producent danych | błędny rekord nie wchodzi do transportu |
| 7 | usługa ingest | drugi raz ten sam kontrakt: dane mogą przyjść spoza publishera |
| 8–9 | usługa ingest | zapis przed 204 — awaria nie gubi rekordu |
| 10–12 | usługa ingest | powtórka bez duplikatu, a konflikt widać w kwarantannie |
| 13–14 | Pub/Sub | ponowienia i dead-letter zamiast utraty zdarzenia |
