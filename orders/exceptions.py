from http import HTTPStatus

from django.core.exceptions import RequestDataTooBig
from rest_framework.response import Response
from rest_framework.serializers import Serializer
from rest_framework.views import exception_handler as drf_exception_handler


class OrderError(Exception):
    def __init__(self, code: str, message: str, http_status: HTTPStatus) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class RequestValidationError(Exception):
    def __init__(self, code: str, message: str, errors=None) -> None:
        super().__init__(message)
        self.code = code
        self.errors = errors


def validated(serializer: Serializer, code: str, message: str) -> Serializer:
    if not serializer.is_valid():
        raise RequestValidationError(code, message, serializer.errors)
    return serializer


def error_response(code: str, message: str, status_code: int, **extra) -> Response:
    return Response({"error": {"code": code, "message": message, **extra}}, status=status_code)


def api_exception_handler(exc, context):
    if isinstance(exc, OrderError):
        return error_response(exc.code, str(exc), exc.http_status)
    if isinstance(exc, RequestValidationError):
        extra = {"details": exc.errors} if exc.errors is not None else {}
        return error_response(exc.code, str(exc), HTTPStatus.BAD_REQUEST, **extra)
    if isinstance(exc, RequestDataTooBig):
        return error_response(
            "request_too_large",
            "Request body exceeds 64 KiB.",
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
        )
    return drf_exception_handler(exc, context)
