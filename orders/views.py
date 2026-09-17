import re

from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from orders.exceptions import RequestValidationError, validated
from orders.serializers import OrderCreateSerializer, OrderOutputSerializer
from orders.services import create_order

IDEMPOTENCY_KEY_PATTERN = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")


def _idempotency_key(request: Request) -> str:
    key = request.headers.get("Idempotency-Key")
    if not key or not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        raise RequestValidationError(
            "invalid_idempotency_key",
            "Idempotency-Key must contain 1-128 ASCII letters, digits, '.', '_', ':' or '-'.",
        )
    return key


class OrderCreateAPIView(APIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "orders"

    def post(self, request: Request) -> Response:
        serializer = validated(
            OrderCreateSerializer(data=request.data),
            "invalid_request",
            "Request validation failed.",
        )
        result, replayed = create_order(
            actor_id=request.user.pk,
            idempotency_key=_idempotency_key(request),
            **serializer.validated_data,
        )
        response = Response(OrderOutputSerializer(result).data, status=status.HTTP_201_CREATED)
        response["Idempotency-Replayed"] = str(replayed).lower()
        return response
