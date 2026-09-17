from django.core.exceptions import RequestDataTooBig
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from orders.serializers import (
    IdempotencyKeySerializer,
    OrderCreateSerializer,
    OrderOutputSerializer,
)
from orders.services import OrderError, create_order

ORDER_ERROR_STATUS = {
    "forbidden": status.HTTP_403_FORBIDDEN,
    "goods_not_found": status.HTTP_404_NOT_FOUND,
    "promo_already_used": status.HTTP_409_CONFLICT,
    "promo_limit_reached": status.HTTP_409_CONFLICT,
    "promo_not_found": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "promo_expired": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "promo_not_applicable": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "idempotency_conflict": status.HTTP_409_CONFLICT,
    "idempotency_incomplete": status.HTTP_409_CONFLICT,
}


def error_response(code: str, message: str, status_code: int, **extra) -> Response:
    error = {"code": code, "message": message, **extra}
    return Response({"error": error}, status=status_code)


class OrderCreateAPIView(APIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "orders"

    def post(self, request: Request) -> Response:
        try:
            serializer = OrderCreateSerializer(data=request.data)
        except RequestDataTooBig:
            return error_response(
                "request_too_large",
                "Request body exceeds 64 KiB.",
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        if not serializer.is_valid():
            return error_response(
                "invalid_request",
                "Request validation failed.",
                status.HTTP_400_BAD_REQUEST,
                details=serializer.errors,
            )
        key_serializer = IdempotencyKeySerializer(
            data={"key": request.headers.get("Idempotency-Key")}
        )
        if not key_serializer.is_valid():
            return error_response(
                "invalid_idempotency_key",
                "Idempotency-Key must contain 1-128 ASCII letters, digits, '.', '_', ':' or '-'.",
                status.HTTP_400_BAD_REQUEST,
            )
        try:
            result, replayed = create_order(
                actor_id=request.user.pk,
                idempotency_key=key_serializer.validated_data["key"],
                **serializer.validated_data,
            )
        except OrderError as exc:
            return error_response(exc.code, str(exc), ORDER_ERROR_STATUS[exc.code])
        response = Response(OrderOutputSerializer(result).data, status=status.HTTP_201_CREATED)
        response["Idempotency-Replayed"] = str(replayed).lower()
        return response
