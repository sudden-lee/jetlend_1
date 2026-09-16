import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TypedDict

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from orders.models import Good, Order, OrderItem, PromoCode, PromoCodeRedemption


class OrderError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def positive_integer(value: object, field: str, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise OrderError("invalid_request", f"{field} must be an integer from 1 to {maximum}.")
    return value


@dataclass(frozen=True)
class GoodsInput:
    good_id: int
    quantity: int

    def __post_init__(self) -> None:
        positive_integer(self.good_id, "good_id", 2**63 - 1)
        positive_integer(self.quantity, "quantity", 10_000)


@dataclass(frozen=True)
class OrderInput:
    user_id: int
    goods: tuple[GoodsInput, ...]
    promo_code: str | None = None

    def __post_init__(self) -> None:
        positive_integer(self.user_id, "user_id", 2**63 - 1)
        if not isinstance(self.goods, tuple) or not 1 <= len(self.goods) <= 100:
            raise OrderError("invalid_request", "goods must contain from 1 to 100 items.")
        if any(not isinstance(item, GoodsInput) for item in self.goods):
            raise OrderError("invalid_request", "goods must contain GoodsInput items.")
        good_ids = [item.good_id for item in self.goods]
        if len(good_ids) != len(set(good_ids)):
            raise OrderError(
                "invalid_request", "Duplicate good_id; combine quantities in one item."
            )
        if self.promo_code is not None and (
            not isinstance(self.promo_code, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.promo_code) is None
        ):
            raise OrderError(
                "invalid_request", "promo_code must contain 1-64 letters, digits, _ or -."
            )


class GoodsResponse(TypedDict):
    good_id: int
    quantity: int
    price: int | float
    discount: str
    total: int | float


class OrderResponse(TypedDict):
    user_id: int
    order_id: int
    goods: list[GoodsResponse]
    price: int | float
    discount: str
    total: int | float


def validate_payload(payload: object) -> OrderInput:
    if not isinstance(payload, dict) or set(payload) - {"user_id", "goods", "promo_code"}:
        raise OrderError(
            "invalid_request", "Expected an object with user_id, goods and promo_code."
        )
    user_id = payload.get("user_id")
    goods = payload.get("goods")
    if not isinstance(goods, list):
        raise OrderError("invalid_request", "goods must contain from 1 to 100 items.")
    items: list[GoodsInput] = []
    for item in goods:
        if not isinstance(item, dict) or set(item) != {"good_id", "quantity"}:
            raise OrderError("invalid_request", "Each item must contain only good_id and quantity.")
        items.append(GoodsInput(item["good_id"], item["quantity"]))
    promo_code = payload.get("promo_code")
    if "promo_code" in payload and promo_code is None:
        raise OrderError("invalid_request", "promo_code must contain 1-64 letters, digits, _ or -.")
    return OrderInput(user_id, tuple(items), promo_code)


def money(cents: int) -> int | float:
    # Only the JSON boundary uses float; all amounts and arithmetic use cents/Decimal.
    return cents // 100 if cents % 100 == 0 else cents / 100


def rate_string(rate: Decimal) -> str:
    return format(rate.normalize(), "f")


@transaction.atomic
def create_order(data: OrderInput, *, actor_id: int) -> OrderResponse:
    if data.user_id != actor_id:
        raise OrderError("forbidden", "You can only create an order for yourself.", 403)
    if not get_user_model().objects.filter(pk=actor_id, is_active=True).exists():
        raise OrderError("unauthenticated", "An active user is required.", 401)

    promo: PromoCode | None = None
    if data.promo_code is not None:
        promo = PromoCode.objects.select_for_update().filter(code=data.promo_code).first()
        if promo is None:
            raise OrderError("promo_not_found", "Promo code does not exist.", 422)
        if promo.expires_at <= timezone.now():
            raise OrderError("promo_expired", "Promo code has expired.", 422)
        uses = PromoCodeRedemption.objects.filter(promo_code=promo)
        if uses.filter(user_id=actor_id).exists():
            raise OrderError("promo_already_used", "You have already used this promo code.", 409)
        if uses.count() >= promo.max_uses:
            raise OrderError("promo_limit_reached", "Promo code usage limit has been reached.", 409)

    goods = Good.objects.in_bulk(item.good_id for item in data.goods)
    if len(goods) != len(data.goods):
        raise OrderError("goods_not_found", "One or more goods do not exist.", 404)
    lines: list[OrderItem] = []
    applicable = False
    price_cents = 0
    for item in data.goods:
        good = goods[item.good_id]
        rate = Decimal(0)
        if (
            promo is not None
            and not good.excluded_from_promotions
            and (promo.category_id is None or promo.category_id == good.category_id)
        ):
            rate = promo.discount
            applicable = True
        subtotal = good.price_cents * item.quantity
        total = int((Decimal(subtotal) * (1 - rate)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        price_cents += subtotal
        lines.append(
            OrderItem(
                good=good,
                quantity=item.quantity,
                price_cents=good.price_cents,
                discount=rate,
                total_cents=total,
            )
        )
    if promo is not None and not applicable:
        raise OrderError("promo_not_applicable", "No goods are eligible for this promo code.", 422)

    total_cents = sum(line.total_cents for line in lines)
    discount = (Decimal(price_cents - total_cents) / Decimal(price_cents)).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )
    order = Order.objects.create(
        user_id=actor_id,
        promo_code=promo,
        price_cents=price_cents,
        discount=discount,
        total_cents=total_cents,
    )
    if promo is not None:
        PromoCodeRedemption.objects.create(user_id=actor_id, promo_code=promo, order=order)
    for line in lines:
        line.order = order
    OrderItem.objects.bulk_create(lines)
    return {
        "user_id": actor_id,
        "order_id": order.pk,
        "goods": [
            {
                "good_id": line.good_id,
                "quantity": line.quantity,
                "price": money(line.price_cents),
                "discount": rate_string(line.discount),
                "total": money(line.total_cents),
            }
            for line in lines
        ],
        "price": money(price_cents),
        "discount": rate_string(discount),
        "total": money(total_cents),
    }
