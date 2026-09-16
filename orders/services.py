from decimal import ROUND_HALF_UP, Decimal
from typing import TypedDict

from django.db import transaction
from django.db.models import Count, Q
from django.utils.timezone import now

from orders.models import Good, Order, OrderItem, PromoCode, PromoCodeRedemption

MONEY_STEP = Decimal("0.01")
ZERO = Decimal("0")


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


@transaction.atomic
def create_order(
    *,
    actor_id: int,
    user_id: int,
    goods: list[GoodInput],
    promo_code: str | None = None,
) -> OrderResult:
    if user_id != actor_id:
        raise OrderError("forbidden", "You can only create an order for yourself.")

    promo: PromoCode | None = None
    if promo_code is not None:
        promo = PromoCode.objects.select_for_update().filter(code=promo_code).first()
        if promo is None:
            raise OrderError("promo_not_found", "Promo code does not exist.")
        if promo.expires_at <= now():
            raise OrderError("promo_expired", "Promo code has expired.")
        usage = PromoCodeRedemption.objects.filter(promo_code=promo).aggregate(
            total=Count("pk"), by_user=Count("pk", filter=Q(user_id=actor_id))
        )
        if usage["by_user"]:
            raise OrderError("promo_already_used", "You have already used this promo code.")
        if usage["total"] >= promo.max_uses:
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
                "price": line.price,
                "discount": line.discount,
                "total": line.total,
            }
            for line in lines
        ],
        "price": price,
        "discount": discount,
        "total": total,
    }
