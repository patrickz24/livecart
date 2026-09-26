"""How the errors of the ports and of the domain answer over HTTP.

- ``RecordNotFound`` is 404 with one fixed body, whatever was missing: a
  seller who asks for another seller's session gets exactly what an id that
  names nothing gets, never a 403 that would confirm the id exists;
- the refusals that depend on the state of a row are 409: a transition the
  machine does not allow, a change the session's status no longer takes, a
  taken live id, a product the catalog cannot hold, a restock below what is
  reserved and sold, a request whose idempotency key has not answered yet;
- an idempotency key sent again with another request is 422.

The body is always ``{"detail": ...}``, with a message chosen here: an
exception's own text may name ids or values and never reaches a client. The
handlers that ``install`` registers answer for every route; a route that
must record its answer first (the idempotent restock) calls ``error_answer``.

A request that fails validation gets FastAPI's 422, each error's location,
type and message, without the ``input`` FastAPI adds to each: the value it
refused, and for a missing field or an unexpected one the whole object
around it, a login's password included. The client sent that value, but a
response carrying it may be kept by proxies, error trackers or client logs.
"""

from collections.abc import Mapping
from typing import Final

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.constants import REF_PREFIX
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from livecart_core.application.dto import MAX_STOCK_TOTAL
from livecart_core.application.idempotency import (
    IdempotencyKeyReused,
    IdempotentRequestInFlight,
)
from livecart_core.application.ports import (
    LiveSessionConflict,
    ProductConflict,
    RecordNotFound,
    StockConflict,
)
from livecart_core.domain.errors import EditRefused, InvalidTransition
from livecart_core.domain.live_sessions import CatalogConflict

NOT_FOUND: Final = "Not found"
CATALOG_CONFLICTS: Final = {
    CatalogConflict.KEY_TAKEN: "The session already sells this code and variant",
    CatalogConflict.VARIANT_MIX: (
        "A code has either one product without a variant or only products with one"
    ),
    CatalogConflict.FULL: "The session holds as many products as it may",
}
ANSWERED: Final = (
    RecordNotFound,
    InvalidTransition,
    EditRefused,
    LiveSessionConflict,
    ProductConflict,
    StockConflict,
    IdempotencyKeyReused,
    IdempotentRequestInFlight,
)


class ErrorResponse(BaseModel):
    detail: str


def error_answer(error: Exception) -> tuple[int, str]:
    """The status code and detail that answer ``error``; any other error is raised again."""
    match error:
        case RecordNotFound():
            return 404, NOT_FOUND
        case InvalidTransition(current=current, target=target):
            return 409, f"The session is {current}: it cannot become {target}"
        case EditRefused(status=status, keys=True):
            return 409, (
                f"The session is {status}: its platform, live id and product codes and "
                "variants can no longer change"
            )
        case EditRefused(status=status):
            return 409, f"The session is {status}: it can no longer change"
        case LiveSessionConflict():
            return 409, "Another session has this platform and live id"
        case ProductConflict(conflict=conflict):
            return 409, CATALOG_CONFLICTS[conflict]
        case StockConflict(above_maximum=True):
            return 409, f"The stock total would rise above {MAX_STOCK_TOTAL}"
        case StockConflict():
            return 409, "The stock total would fall below the units reserved and sold"
        case IdempotentRequestInFlight():
            return 409, "The request of this Idempotency-Key has not answered yet: try again"
        case IdempotencyKeyReused():
            return 422, "This Idempotency-Key was sent with another request"
    raise error


async def _answer(_request: Request, error: Exception) -> JSONResponse:
    status_code, detail = error_answer(error)
    return JSONResponse({"detail": detail}, status_code=status_code)


async def _invalid_request(_request: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, RequestValidationError):
        raise error
    errors = [
        {key: value for key, value in item.items() if key != "input"} for item in error.errors()
    ]
    return JSONResponse(
        {"detail": jsonable_encoder(errors)}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
    )


def install(app: FastAPI) -> None:
    """Register the answers of every error of ``ANSWERED``, and of an invalid request."""
    for error_type in ANSWERED:
        app.add_exception_handler(error_type, _answer)
    app.add_exception_handler(RequestValidationError, _invalid_request)


def documented(descriptions: Mapping[int, str]) -> dict[int | str, dict[str, object]]:
    """The OpenAPI ``responses`` of a route: each error code, its meaning and its body."""
    return {
        code: {"model": ErrorResponse, "description": description}
        for code, description in descriptions.items()
    }


def documented_json(descriptions: Mapping[int, str]) -> dict[int | str, dict[str, object]]:
    """``documented`` for a route whose success is no JSON, such as a stream of events.

    FastAPI files a response ``model`` under the route's own media type
    (``text/event-stream``), where the errors are JSON: they are declared
    with their content type instead, and the schema that ``documented``
    routes put in the components.
    """
    body = {"application/json": {"schema": {"$ref": f"{REF_PREFIX}{ErrorResponse.__name__}"}}}
    return {
        code: {"description": description, "content": body}
        for code, description in descriptions.items()
    }
