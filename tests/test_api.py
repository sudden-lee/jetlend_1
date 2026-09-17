import json
import uuid
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import IntegrityError, transaction
from django.test import Client
from rest_framework.test import APIClient

from orders.models import (
    Good,
    Order,
    OrderIdempotencyKey,
    OrderItem,
    PromoCode,
    PromoCodeRedemption,
)
from orders.services import OrderError, create_order
from tests.conftest import NOW

URL = "/api/orders/"

pytestmark = pytest.mark.django_db


def post_order(client: APIClient, payload: object, key: str | None = None):
    return client.post(
        URL,
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
    )


def assert_error(client: APIClient, payload: object, status: int, code: str) -> None:
    orders_before = Order.objects.count()
    items_before = OrderItem.objects.count()
    response = post_order(client, payload)
    assert response.status_code == status, response.content
    assert response.json()["error"]["code"] == code
    assert Order.objects.count() == orders_before
    assert OrderItem.objects.count() == items_before


def test_example_and_price_snapshot(api_client, user, catalog, promo, payload) -> None:
    with patch("orders.services.now", return_value=NOW):
        response = post_order(api_client, payload)

    assert response.status_code == 201, response.content
    order = Order.objects.get()
    assert response.json() == {
        "user_id": user.pk,
        "order_id": order.pk,
        "goods": [
            {
                "good_id": catalog["good"].pk,
                "quantity": 2,
                "price": "100",
                "discount": "0.1",
                "total": "180",
            }
        ],
        "price": "200",
        "discount": "0.1",
        "total": "180",
    }
    assert order.price == Decimal("200")
    assert order.total == Decimal("180")
    assert order.discount == Decimal("0.1")
    assert order.promo_code_id == promo.pk
    promo.refresh_from_db()
    assert promo.uses_count == 1
    Good.objects.filter(pk=catalog["good"].pk).update(price=Decimal("200"))
    item = order.items.get()
    assert item.price == Decimal("100")
    assert item.total == Decimal("180")


def test_orders_without_promo_can_be_created_repeatedly(api_client, payload) -> None:
    payload.pop("promo_code")
    for _ in range(3):
        response = post_order(api_client, payload)
        assert response.status_code == 201, response.content
        assert response.json()["discount"] == "0"
        assert response.json()["total"] == "200"
    assert Order.objects.filter(promo_code__isnull=True).count() == 3


def test_mixed_basket_only_discounts_eligible_goods(api_client, catalog, promo, payload) -> None:
    promo.category = catalog["category"]
    promo.save(update_fields=["category"])
    payload["goods"] = [
        {"good_id": catalog[name].pk, "quantity": 1}
        for name in ("good", "other_good", "excluded_good")
    ]
    with patch("orders.services.now", return_value=NOW):
        response = post_order(api_client, payload)

    assert response.status_code == 201, response.content
    data = response.json()
    assert [item["discount"] for item in data["goods"]] == ["0.1", "0", "0"]
    assert [item["total"] for item in data["goods"]] == ["90", "100", "100"]
    assert data["price"] == "300"
    assert data["total"] == "290"
    assert data["discount"] == "0.0333"
    assert Order.objects.get().discount == Decimal("0.0333")


def test_ineligible_goods_are_rejected(api_client, catalog, promo, payload) -> None:
    payload["goods"] = [{"good_id": catalog["excluded_good"].pk, "quantity": 1}]
    with patch("orders.services.now", return_value=NOW):
        assert_error(api_client, payload, 422, "promo_not_applicable")

    promo.category = catalog["category"]
    promo.save(update_fields=["category"])
    payload["goods"] = [{"good_id": catalog["other_good"].pk, "quantity": 1}]
    with patch("orders.services.now", return_value=NOW):
        assert_error(api_client, payload, 422, "promo_not_applicable")


@pytest.mark.parametrize("expiration", [NOW, NOW - timedelta(microseconds=1)])
def test_expired_promo(api_client, promo, payload, expiration) -> None:
    promo.expires_at = expiration
    promo.save(update_fields=["expires_at"])
    with patch("orders.services.now", return_value=NOW):
        assert_error(api_client, payload, 422, "promo_expired")


def test_unknown_promo(api_client, payload) -> None:
    payload["promo_code"] = "UNKNOWN"
    assert_error(api_client, payload, 422, "promo_not_found")


def test_promo_limit(api_client, user, other_user, catalog, promo, payload) -> None:
    promo.max_uses = 1
    promo.save(update_fields=["max_uses"])
    with patch("orders.services.now", return_value=NOW):
        create_order(
            actor_id=other_user.pk,
            user_id=other_user.pk,
            goods=[{"good_id": catalog["good"].pk, "quantity": 1}],
            promo_code=promo.code,
            idempotency_key="other-user-order",
        )
        assert_error(api_client, payload, 409, "promo_limit_reached")


def test_user_cannot_reuse_promo(api_client, payload) -> None:
    with patch("orders.services.now", return_value=NOW):
        assert post_order(api_client, payload).status_code == 201
        assert_error(api_client, payload, 409, "promo_already_used")


def test_full_discount(api_client, promo, payload) -> None:
    promo.discount = Decimal("1")
    promo.save(update_fields=["discount"])
    with patch("orders.services.now", return_value=NOW):
        response = post_order(api_client, payload)
    assert response.status_code == 201, response.content
    assert response.json()["total"] == "0"
    assert response.json()["discount"] == "1"


def test_money_rounds_half_up_per_line(api_client, catalog, promo, payload) -> None:
    catalog["good"].price = Decimal("1.01")
    catalog["good"].save(update_fields=["price"])
    promo.discount = Decimal("0.5")
    promo.save(update_fields=["discount"])
    payload["goods"][0]["quantity"] = 1
    with patch("orders.services.now", return_value=NOW):
        response = post_order(api_client, payload)

    assert response.status_code == 201, response.content
    assert response.json()["price"] == "1.01"
    assert response.json()["total"] == "0.51"
    assert response.json()["discount"] == "0.495"
    assert Order.objects.get().total == Decimal("0.51")


def test_rounding_can_reduce_effective_discount_to_zero(api_client, catalog, payload) -> None:
    catalog["good"].price = Decimal("0.05")
    catalog["good"].save(update_fields=["price"])
    payload["goods"][0]["quantity"] = 1
    with patch("orders.services.now", return_value=NOW):
        response = post_order(api_client, payload)
        assert response.status_code == 201, response.content
        assert response.json()["total"] == "0.05"
        assert response.json()["discount"] == "0"
        assert_error(api_client, payload, 409, "promo_already_used")


def test_invalid_payloads(api_client, catalog, payload) -> None:
    cases: list[object] = [None, [], "order", {}, {**payload, "extra": 1}]
    for value in (None, True, False, 0, -1, 1.5, "1", 2**63):
        cases.append({**payload, "user_id": value})
    for value in (None, {}, "goods", [], [{}] * 101):
        cases.append({**payload, "goods": value})
    for field in ("good_id", "quantity"):
        for value in (None, True, False, 0, -1, 1.5, "1"):
            item = {"good_id": catalog["good"].pk, "quantity": 1, field: value}
            cases.append({**payload, "goods": [item]})
    cases.extend(
        [
            {**payload, "goods": [{"good_id": catalog["good"].pk}]},
            {**payload, "goods": [{"good_id": 2**63, "quantity": 1}]},
            {**payload, "goods": [{"good_id": catalog["good"].pk, "quantity": 10_001}]},
            {**payload, "goods": [{"good_id": catalog["good"].pk, "quantity": 1, "x": 1}]},
            {**payload, "goods": payload["goods"] * 2},
        ]
    )
    for value in (None, "", " ", "a" * 65, "SUMMER 2025", 1, True):
        cases.append({**payload, "promo_code": value})

    for invalid_payload in cases:
        response = post_order(api_client, invalid_payload)
        assert response.status_code == 400, invalid_payload
        assert response.json()["error"]["code"] == "invalid_request"
    assert Order.objects.count() == 0


def test_unknown_goods_does_not_consume_promo(api_client, catalog, payload) -> None:
    invalid = deepcopy(payload)
    invalid["goods"].append({"good_id": catalog["excluded_good"].pk + 1000, "quantity": 1})
    with patch("orders.services.now", return_value=NOW):
        assert_error(api_client, invalid, 404, "goods_not_found")
        assert post_order(api_client, payload).status_code == 201


def test_authentication_and_ownership(user, other_user, payload) -> None:
    client = APIClient()
    assert client.post(URL, payload, format="json").status_code == 403

    client.force_authenticate(user=user)
    payload["user_id"] = other_user.pk
    assert_error(client, payload, 403, "forbidden")

    client.force_authenticate(user=None)
    user.is_active = False
    user.save(update_fields=["is_active"])
    client.force_login(user)
    payload["user_id"] = user.pk
    response = client.post(URL, payload, format="json")
    assert response.status_code == 403
    assert response.data["detail"].code == "not_authenticated"


def test_csrf_is_required_for_session_authentication(user, payload) -> None:
    client = Client(enforce_csrf_checks=True)
    assert client.login(username=user.username, password="test-password-only")
    response = client.post(URL, json.dumps(payload), content_type="application/json")
    assert response.status_code == 403
    assert Order.objects.count() == 0

    client = Client(enforce_csrf_checks=True)
    assert client.login(username=user.username, password="test-password-only")
    login_page = client.get("/accounts/login/")
    assert login_page.wsgi_request.user.is_authenticated
    token = client.cookies["csrftoken"].value
    with patch("orders.services.now", return_value=NOW):
        response = client.post(
            URL,
            json.dumps(payload),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
            HTTP_IDEMPOTENCY_KEY="csrf-order",
        )
    assert response.status_code == 201, (
        response.wsgi_request.user.is_authenticated,
        response.data["detail"].code,
    )


def test_http_protocol_errors(api_client, payload) -> None:
    response = api_client.get(URL)
    assert response.status_code == 405
    assert response["Allow"] == "POST, OPTIONS"

    assert api_client.post(URL, "{}", content_type="text/plain").status_code == 415
    for body in ("{", "", b"\xff"):
        response = api_client.generic("POST", URL, body, content_type="application/json")
        assert response.status_code == 400

    response = api_client.generic("POST", URL, " " * 65_537, content_type="application/json")
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert Order.objects.count() == 0


def test_item_failure_rolls_back_order_and_promo_usage(user, catalog, promo) -> None:
    kwargs = {
        "actor_id": user.pk,
        "user_id": user.pk,
        "goods": [{"good_id": catalog["good"].pk, "quantity": 2}],
        "promo_code": promo.code,
        "idempotency_key": "rollback-order",
    }
    with patch("orders.services.now", return_value=NOW):
        with patch(
            "orders.services.OrderItem.objects.bulk_create", side_effect=RuntimeError("fail")
        ):
            with pytest.raises(RuntimeError, match="fail"):
                create_order(**kwargs)
        assert not Order.objects.exists()
        assert not PromoCodeRedemption.objects.exists()
        assert create_order(**kwargs)[0]["total"] == Decimal("180")


def test_promo_redemption_survives_order_deletion(user, catalog, promo) -> None:
    kwargs = {
        "actor_id": user.pk,
        "user_id": user.pk,
        "goods": [{"good_id": catalog["good"].pk, "quantity": 1}],
        "promo_code": promo.code,
        "idempotency_key": "deletion-order",
    }
    with patch("orders.services.now", return_value=NOW):
        result, _ = create_order(**kwargs)
        OrderIdempotencyKey.objects.all().delete()
        Order.objects.get(pk=result["order_id"]).delete()
        assert PromoCodeRedemption.objects.get().order_id is None
        kwargs["idempotency_key"] = "deletion-order-retry"
        with pytest.raises(OrderError, match="already used"):
            create_order(**kwargs)


def test_database_constraints(user, promo) -> None:
    valid = PromoCode.objects.create(
        code="MINIMUM",
        discount=Decimal("0.0001"),
        expires_at=NOW + timedelta(days=1),
        max_uses=1,
    )
    valid.refresh_from_db()
    assert valid.discount == Decimal("0.0001")

    with pytest.raises(IntegrityError), transaction.atomic():
        PromoCode.objects.create(
            code="TOO_SMALL",
            discount=Decimal("0.00001"),
            expires_at=NOW + timedelta(days=1),
            max_uses=1,
        )

    fields = {"user": user, "promo_code": promo}
    PromoCodeRedemption.objects.create(**fields)
    with pytest.raises(IntegrityError), transaction.atomic():
        PromoCodeRedemption.objects.create(**fields)
