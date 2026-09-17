import hashlib
import json
import logging
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import TypedDict

from django.db import IntegrityError, OperationalError, transaction
from django.utils.timezone import now

from orders.models import (
    Good,
    Order,
    OrderIdempotencyKey,
    OrderItem,
    PromoCode,
    PromoCodeRedemption,
)

MONEY_STEP = Decimal("0.01")
ZERO = Decimal("0")
RETRYABLE_SQLSTATES = {"40001", "40P01"}
MAX_DB_ATTEMPTS = 3
logger = logging.getLogger(__name__)


class OrderError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class GoodInput(TypedDict):
    good_id: int
    quantity: int


class GoodResult(GoodInput):
    price: Decimal
    discount: Decimal
    total: Decimal


class OrderResult(TypedDict):
    user_id: int
    order_id: int
    goods: list[GoodResult]
    price: Decimal
    discount: Decimal
    total: Decimal


def create_order(
    *,
    actor_id: int,
    user_id: int,
    goods: list[GoodInput],
    idempotency_key: str,
    promo_code: str | None = None,
) -> tuple[OrderResult, bool]:
    """Create an order, or replay the saved result for the same logical request."""
    for attempt in range(MAX_DB_ATTEMPTS):
        try:
            return _create_order_once(
                actor_id=actor_id,
                user_id=user_id,
                goods=goods,
                promo_code=promo_code,
                idempotency_key=idempotency_key,
            )
        except OperationalError as exc:
            sqlstate = getattr(exc.__cause__, "sqlstate", None)
            if sqlstate not in RETRYABLE_SQLSTATES or attempt == MAX_DB_ATTEMPTS - 1:
                raise
            logger.warning(
                "Retrying order transaction after PostgreSQL error: sqlstate=%s attempt=%s/%s",
                sqlstate,
                attempt + 1,
                MAX_DB_ATTEMPTS,
            )
            time.sleep(0.02 * 2**attempt)
    raise AssertionError("unreachable")


@transaction.atomic
def _create_order_once(
    *,
    actor_id: int,
    user_id: int,
    goods: list[GoodInput],
    idempotency_key: str,
    promo_code: str | None,
) -> tuple[OrderResult, bool]:
    if user_id != actor_id:
        raise OrderError("forbidden", "You can only create an order for yourself.")

    request_hash = _request_hash(user_id, goods, promo_code)
    key, created = _claim_idempotency_key(actor_id, idempotency_key, request_hash)
    if not created:
        if key.request_hash != request_hash:
            raise OrderError(
                "idempotency_conflict",
                "This Idempotency-Key was already used with a different request.",
            )
        if key.order_id is None:
            raise OrderError("idempotency_incomplete", "The previous request is incomplete.")
        return _order_result(key.order), True

    promo: PromoCode | None = None
    if promo_code is not None:
        promo = PromoCode.objects.select_for_update().filter(code=promo_code).first()
        if promo is None:
            raise OrderError("promo_not_found", "Promo code does not exist.")
        if promo.expires_at <= now():
            raise OrderError("promo_expired", "Promo code has expired.")
        if PromoCodeRedemption.objects.filter(promo_code=promo, user_id=actor_id).exists():
            raise OrderError("promo_already_used", "You have already used this promo code.")
        if promo.uses_count >= promo.max_uses:
            raise OrderError("promo_limit_reached", "Promo code usage limit has been reached.")

    goods_by_id = Good.objects.in_bulk(item["good_id"] for item in goods)
    if len(goods_by_id) != len(goods):
        raise OrderError("goods_not_found", "One or more goods do not exist.")

    lines: list[OrderItem] = []
    applicable = False
    price = ZERO
    for item in goods:
        good = goods_by_id[item["good_id"]]
        rate = ZERO
        if (
            promo is not None
            and not good.excluded_from_promotions
            and (promo.category_id is None or promo.category_id == good.category_id)
        ):
            rate = promo.discount
            applicable = True
        subtotal = (good.price * item["quantity"]).quantize(MONEY_STEP)
        total = (subtotal * (1 - rate)).quantize(MONEY_STEP, rounding=ROUND_HALF_UP)
        price += subtotal
        lines.append(
            OrderItem(
                good=good,
                quantity=item["quantity"],
                price=good.price,
                discount=rate,
                total=total,
            )
        )
    if promo is not None and not applicable:
        raise OrderError("promo_not_applicable", "No goods are eligible for this promo code.")

    total = sum((line.total for line in lines), start=ZERO)
    discount = ((price - total) / price).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    order = Order.objects.create(
        user_id=actor_id,
        promo_code=promo,
        price=price,
        discount=discount,
        total=total,
    )
    if promo is not None:
        PromoCodeRedemption.objects.create(user_id=actor_id, promo_code=promo, order=order)
        promo.uses_count += 1
        promo.save(update_fields=["uses_count"])
    for line in lines:
        line.order = order
    OrderItem.objects.bulk_create(lines)
    key.order = order
    key.save(update_fields=["order"])
    return _lines_result(order, lines), False


def _claim_idempotency_key(
    user_id: int, key: str, request_hash: str
) -> tuple[OrderIdempotencyKey, bool]:
    try:
        with transaction.atomic():
            return (
                OrderIdempotencyKey.objects.create(
                    user_id=user_id, key=key, request_hash=request_hash
                ),
                True,
            )
    except IntegrityError:
        return (
            OrderIdempotencyKey.objects.select_for_update().get(user_id=user_id, key=key),
            False,
        )


def _request_hash(user_id: int, goods: list[GoodInput], promo_code: str | None) -> str:
    payload = {"goods": goods, "promo_code": promo_code, "user_id": user_id}
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _order_result(order: Order) -> OrderResult:
    return _lines_result(order, list(order.items.order_by("pk")))


def _lines_result(order: Order, lines: list[OrderItem]) -> OrderResult:
    return {
        "user_id": order.user_id,
        "order_id": order.pk,
        "goods": [
            {
                "good_id": line.good_id,
                "quantity": line.quantity,
                "price": line.price,
                "discount": line.discount,
                "total": line.total,
            }
            for line in lines
        ],
        "price": order.price,
        "discount": order.discount,
        "total": order.total,
    }
