import json

from django.core.exceptions import RequestDataTooBig
from django.http import HttpRequest, JsonResponse

from orders.services import OrderError, create_order, validate_payload


def error_response(code: str, message: str, status: int) -> JsonResponse:
    return JsonResponse({"error": {"code": code, "message": message}}, status=status)


def csrf_failure(request: HttpRequest, reason: str = "") -> JsonResponse:
    return error_response("csrf_failed", "A valid CSRF token is required.", 403)


def create_order_view(request: HttpRequest) -> JsonResponse:
    if request.method != "POST":
        response = error_response("method_not_allowed", "Use POST.", 405)
        response["Allow"] = "POST"
        return response
    try:
        if not request.user.is_authenticated:
            return error_response("unauthenticated", "Log in before creating an order.", 401)
        actor_id = request.user.pk
        if request.content_type != "application/json":
            return error_response("unsupported_media_type", "Use application/json.", 415)
        try:
            payload: object = json.loads(request.body)
        except RequestDataTooBig:
            return error_response("request_too_large", "Request body exceeds 64 KiB.", 413)
        except (ValueError, RecursionError):
            return error_response("invalid_json", "Request body must be valid JSON.", 400)
        data = validate_payload(payload)
        result = create_order(data, actor_id=actor_id)
    except OrderError as exc:
        return error_response(exc.code, str(exc), exc.status)
    return JsonResponse(result, status=201)
