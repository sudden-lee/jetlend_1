import hashlib
import json
import logging
import time
from decimal import ROUND_HALF_UP, Decimal
from http import HTTPStatus
from typing import TypedDict

from django.db import IntegrityError, OperationalError, transaction
from django.utils.timezone import now

from orders.exceptions import OrderError
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
    if not goods:
        raise ValueError("goods must not be empty")
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
    _check_ownership(actor_id, user_id)

    request_hash = _request_hash(user_id, goods, promo_code)
    key, created = _claim_idempotency_key(actor_id, idempotency_key, request_hash)
    if not created:
        return _replay(key, request_hash)

    promo = _lock_promo(promo_code, actor_id)
    goods_by_id = _load_goods(goods)
    lines, price, applicable = _build_lines(goods, goods_by_id, promo)
    if promo is not None and not applicable:
        raise OrderError(
            "promo_not_applicable",
            "No goods are eligible for this promo code.",
            HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    order = _persist_order(actor_id, promo, price, lines, key)
    return _lines_result(order, lines), False


def _check_ownership(actor_id: int, user_id: int) -> None:
    if user_id != actor_id:
        raise OrderError(
            "forbidden", "You can only create an order for yourself.", HTTPStatus.FORBIDDEN
        )


def _replay(key: OrderIdempotencyKey, request_hash: str) -> tuple[OrderResult, bool]:
    if key.request_hash != request_hash:
        raise OrderError(
            "idempotency_conflict",
            "This Idempotency-Key was already used with a different request.",
            HTTPStatus.CONFLICT,
        )
    if key.order_id is None:
        raise OrderError(
            "idempotency_incomplete", "The previous request is incomplete.", HTTPStatus.CONFLICT
        )
    return _order_result(key.order), True


def _lock_promo(promo_code: str | None, actor_id: int) -> PromoCode | None:
    if promo_code is None:
        return None
    promo = PromoCode.objects.select_for_update().filter(code=promo_code).first()
    if promo is None:
        raise OrderError(
            "promo_not_found", "Promo code does not exist.", HTTPStatus.UNPROCESSABLE_ENTITY
        )
    if promo.expires_at <= now():
        raise OrderError(
            "promo_expired", "Promo code has expired.", HTTPStatus.UNPROCESSABLE_ENTITY
        )
    if PromoCodeRedemption.objects.filter(promo_code=promo, user_id=actor_id).exists():
        raise OrderError(
            "promo_already_used", "You have already used this promo code.", HTTPStatus.CONFLICT
        )
    if promo.uses_count >= promo.max_uses:
        raise OrderError(
            "promo_limit_reached", "Promo code usage limit has been reached.", HTTPStatus.CONFLICT
        )
    return promo


def _load_goods(goods: list[GoodInput]) -> dict[int, Good]:
    goods_by_id = Good.objects.in_bulk(item["good_id"] for item in goods)
    if len(goods_by_id) != len(goods):
        raise OrderError("goods_not_found", "One or more goods do not exist.", HTTPStatus.NOT_FOUND)
    return goods_by_id


def _build_lines(
    goods: list[GoodInput], goods_by_id: dict[int, Good], promo: PromoCode | None
) -> tuple[list[OrderItem], Decimal, bool]:
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
    return lines, price, applicable


def _persist_order(
    actor_id: int,
    promo: PromoCode | None,
    price: Decimal,
    lines: list[OrderItem],
    key: OrderIdempotencyKey,
) -> Order:
    total = sum((line.total for line in lines), start=ZERO)
    discount = ((price - total) / price).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    order = Order.objects.create(
        user_id=actor_id, promo_code=promo, price=price, discount=discount, total=total
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
    return order


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
