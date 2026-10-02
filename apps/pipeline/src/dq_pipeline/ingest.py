"""Usługa ingest - odbiorca subskrypcji push Pub/Sub, docelowo na Cloud Run.

Pub/Sub w trybie push wysyła każdą wiadomość jako żądanie HTTP POST i patrzy wyłącznie
na kod odpowiedzi. Cała semantyka niezawodności tej usługi sprowadza się więc do tego,
jaki kod zwróci:

| Sytuacja                                | Kod  | Co robi Pub/Sub                      |
| --------------------------------------- | ---- | ------------------------------------ |
| rekord przyjęty                         | 204  | ack - wiadomość znika z subskrypcji  |
| rekord w kwarantannie (też zły JSON)    | 204  | ack - decyzja zapadła i jest zapisana |
| powtórka rekordu już przyjętego         | 204  | ack - nic do zapisania, zapis już jest |
| koperta niezgodna z formatem push       | 422  | nack - ponowienie, potem dead-letter |
| awaria zapisu (sink)                    | 500  | nack - ponowienie, potem dead-letter |

Najważniejszy jest drugi wiersz. Rekord łamiący kontrakt dostaje ack, bo ponowne
dostarczenie tych samych bajtów da ten sam werdykt - retry tylko zapchałby kolejkę.
Kod błędu zwracamy wtedy, gdy ponowienie MA szansę coś zmienić: sink chwilowo niedostępny,
usługa przeciążona. Takie wiadomości po wyczerpaniu prób trafiają na temat dead-letter
i wracają przez redrive, gdy przyczyna zniknie.

Uwierzytelnienie: na Cloud Run subskrypcja dołącza token OIDC, a usługa jest wdrożona bez
dostępu publicznego - token weryfikuje sama platforma, zanim żądanie dotrze do tego kodu.
Lokalnie emulator tokenu nie wysyła, więc kod niczego tu nie sprawdza i sprawdzać nie musi.
"""

import logging
from pathlib import Path
from typing import Annotated, assert_never

import uvicorn
from fastapi import FastAPI, Response, status
from pydantic import AliasChoices, AwareDatetime, Base64Bytes, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings, SettingsConfigDict

from dq_contracts import PipelineStage
from dq_pipeline.sinks import LocalJsonlSink, Sink
from dq_pipeline.validation import Accepted, RecordValidator, Rejected, Replayed

logger = logging.getLogger("dq_pipeline.ingest")


class PubsubMessage(BaseModel):
    """Wiadomość Pub/Sub w kopercie push.

    Ten model jest w trybie łagodnym i ignoruje nadmiarowe pola - odwrotnie niż kontrakt.
    To nie niekonsekwencja: kontrakt opisuje dane, za które odpowiadamy, a koperta należy
    do Google. Emulator wysyła każde pole dwa razy (`messageId` i `message_id`), a Google
    dokłada nowe pola bez uprzedzenia. `extra="forbid"` zamieniłby każdą taką zmianę
    w awarię całego strumienia, i to z powodu pól, których nie czytamy.
    """

    # alias_generator=to_camel: pola nazywamy po pythonowemu, a pydantic czyta je pod
    # nazwami z JSON-a Google'a (`message_id` <- `messageId`). validate_by_name pozwala
    # dodatkowo podać nazwę pythonową, co upraszcza budowanie kopert w testach.
    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_alias=True,
        validate_by_name=True,
        extra="ignore",
        frozen=True,
    )

    # Base64Bytes dekoduje base64 w trakcie walidacji - kod dostaje gotowe bajty, a zły
    # base64 jest błędem koperty (422), nie błędem danych. Wartość domyślna obsługuje
    # wiadomości bez treści: Pub/Sub pozwala opublikować same atrybuty, a pusta treść
    # trafi potem do kwarantanny jako `malformed_payload`.
    data: Base64Bytes = b""
    message_id: str
    publish_time: AwareDatetime
    attributes: dict[str, str] = Field(default_factory=dict)
    # Numer próby dostarczenia. Pub/Sub podaje go tylko przy włączonej polityce
    # dead-letter, a emulator nie podaje wcale - stąd opcjonalność.
    delivery_attempt: int | None = None


class PushEnvelope(BaseModel):
    """Koperta żądania push: wiadomość i nazwa subskrypcji, która ją dostarczyła."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    message: PubsubMessage
    subscription: str


class IngestSettings(BaseSettings):
    """Konfiguracja usługi ze zmiennych środowiskowych.

    Prefiks `DQ_` oddziela zmienne projektu od reszty środowiska. Wyjątkiem jest `PORT`:
    tę nazwę narzuca Cloud Run, więc pole czyta ją wprost przez `validation_alias`.
    """

    model_config = SettingsConfigDict(env_prefix="DQ_", frozen=True)

    sink_dir: Path = Path("data/stream/ingest")
    port: Annotated[int, Field(gt=0, lt=65536, validation_alias=AliasChoices("PORT"))] = 8080


def create_app(sink: Sink, validator: RecordValidator | None = None) -> FastAPI:
    """Buduje aplikację z wstrzykniętym sinkiem.

    Fabryka zamiast globalnej instancji `app`: testy podstawiają własny sink i walidator,
    a kod produkcyjny (`main`) składa je z konfiguracji. Globalna aplikacja tworzona przy
    imporcie modułu czytałaby env w momencie importu, czyli także w testach.
    """
    validator = validator or RecordValidator(PipelineStage.INGEST)
    app = FastAPI(title="dq-ingest", docs_url=None, redoc_url=None)

    # `/health`, nie `/healthz`: Cloud Run rezerwuje część ścieżek zakończonych na „z"
    # i taki endpoint odpowiadałby 404 z warstwy Google, zanim żądanie dojdzie do usługi.
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # Endpoint synchroniczny (`def`, nie `async def`): FastAPI uruchamia go w puli
    # wątków. Walidacja pydantic i zapis do pliku blokują, więc w `async def` zatrzymałyby
    # pętlę zdarzeń i usługa obsługiwałaby jedno żądanie naraz.
    @app.post("/", status_code=status.HTTP_204_NO_CONTENT)
    def receive(envelope: PushEnvelope) -> Response:
        message = envelope.message
        # `match` po typie werdyktu z `assert_never` na końcu: mypy sprawdza, że obsłużone
        # są wszystkie warianty `Verdict` - nowy wariant bez gałęzi to błąd typów, nie cichy 204.
        match validator.validate(message.data):
            case Accepted(event=event):
                sink.write_events([event])
                outcome = "accepted"
            case Replayed():
                outcome = "replayed"
            case Rejected(record=record):
                sink.write_rejected([record])
                outcome = f"quarantined:{record.reason}"
            case unreachable:
                assert_never(unreachable)
        # Zapis do sinka PRZED odpowiedzią 204. Odwrotna kolejność (najpierw ack) gubi
        # rekord przy awarii zapisu - Pub/Sub uznałby go za dostarczony. Ta kolejność
        # daje „co najmniej raz": przy awarii po zapisie, a przed odpowiedzią, rekord
        # przyjdzie ponownie i zostanie rozpoznany jako powtórka - bez drugiego zapisu.
        logger.info(
            "message %s attempt=%s %s",
            message.message_id,
            message.delivery_attempt,
            outcome,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


def main() -> None:
    """Punkt wejścia `dq-ingest`: konfiguracja z env, sink lokalny, serwer na $PORT."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = IngestSettings()
    app = create_app(LocalJsonlSink(settings.sink_dir))
    # host 0.0.0.0: w kontenerze 127.0.0.1 oznacza wnętrze kontenera, więc ruch
    # z zewnątrz (emulator, Cloud Run) nie dotarłby do usługi.
    uvicorn.run(app, host="0.0.0.0", port=settings.port, access_log=False)
