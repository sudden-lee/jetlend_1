from django.core.exceptions import RequestDataTooBig
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from orders.serializers import OrderCreateSerializer, OrderOutputSerializer
from orders.services import OrderError, create_order

ORDER_ERROR_STATUS = {
    "forbidden": status.HTTP_403_FORBIDDEN,
    "goods_not_found": status.HTTP_404_NOT_FOUND,
    "promo_already_used": status.HTTP_409_CONFLICT,
    "promo_limit_reached": status.HTTP_409_CONFLICT,
    "promo_not_found": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "promo_expired": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "promo_not_applicable": status.HTTP_422_UNPROCESSABLE_ENTITY,
}


def error_response(code: str, message: str, status_code: int, **extra) -> Response:
    error = {"code": code, "message": message, **extra}
    return Response({"error": error}, status=status_code)


class OrderCreateAPIView(APIView):
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
        try:
            result = create_order(actor_id=request.user.pk, **serializer.validated_data)
        except OrderError as exc:
            return error_response(exc.code, str(exc), ORDER_ERROR_STATUS[exc.code])
        return Response(OrderOutputSerializer(result).data, status=status.HTTP_201_CREATED)
