"""Uniform error envelope: {"error": {"code", "message", "field"?}}."""
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("stopshade.errors")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, field: str | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.field = field


def error_response(status: int, code: str, message: str, field: str | None = None) -> JSONResponse:
    body: dict = {"code": code, "message": message}
    if field:
        body["field"] = field
    return JSONResponse({"error": body}, status_code=status, headers={"Cache-Control": "no-store"})


def invalid(field: str, message: str) -> ApiError:
    return ApiError(422, "INVALID_INPUT", message, field)


NOT_FOUND = ("NOT_FOUND", "This report can't be found.")


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return error_response(exc.status, exc.code, exc.message, exc.field)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        field = None
        for err in exc.errors():
            loc = err.get("loc") or ()
            if len(loc) >= 2:
                field = str(loc[1]).lower().replace("-", "_")
                break
        return error_response(422, "INVALID_INPUT", "This field is required.", field)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return error_response(404, *NOT_FOUND)
        if exc.status_code == 405:
            return error_response(405, "METHOD_NOT_ALLOWED", "Method not allowed.")
        return error_response(exc.status_code, "HTTP_ERROR", "Request failed.")

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        log.exception("unexpected error: %s", type(exc).__name__)
        return error_response(500, "INTERNAL_ERROR", "Something went wrong. Please try again.")
