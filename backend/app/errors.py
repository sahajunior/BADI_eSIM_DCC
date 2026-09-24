"""Safe, consistent API errors without reflected request bodies or database details."""

from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    fields: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "fields": fields or {},
                "request_id": getattr(request.state, "request_id", str(uuid4())),
            }
        },
        headers={"Cache-Control": "no-store"},
    )


def install_error_handlers(app: FastAPI) -> None:
    async def api_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ApiError)
        response = error_response(request, exc.status_code, exc.code, exc.message)
        if exc.status_code == 429:
            response.headers["Retry-After"] = "300"
        return response

    async def validation_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        fields = {".".join(str(part) for part in err["loc"]): err["msg"] for err in exc.errors()}
        return error_response(request, 422, "validation_error", "Invalid request.", fields)

    async def http_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, HTTPException)
        messages = {404: "Resource not found.", 405: "Method not allowed."}
        response = error_response(
            request, exc.status_code, "http_error", messages.get(exc.status_code, "Request denied.")
        )
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    async def database_error(request: Request, _exc: Exception) -> JSONResponse:
        return error_response(
            request, 503, "service_unavailable", "Service temporarily unavailable."
        )

    app.add_exception_handler(ApiError, api_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(SQLAlchemyError, database_error)
