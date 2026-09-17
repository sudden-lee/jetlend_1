from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import connections

from orders.models import Category, Good, Order, OrderItem, PromoCode
from orders.services import OrderError, OrderResult, create_order
from tests.conftest import NOW

pytestmark = pytest.mark.django_db(transaction=True)


def run_concurrently(user_ids: tuple[int, int], good: Good, promo: PromoCode):
    assert connections["default"].vendor == "postgresql"
    barrier = Barrier(2)

    def submit(user_id: int) -> tuple[int, OrderResult | OrderError]:
        connection = connections["default"]
        connection.ensure_connection()
        connection_id = id(connection.connection)
        try:
            barrier.wait(timeout=10)
            try:
                result = create_order(
                    actor_id=user_id,
                    user_id=user_id,
                    goods=[{"good_id": good.pk, "quantity": 1}],
                    promo_code=promo.code,
                    idempotency_key=str(uuid4()),
                )[0]
            except OrderError as exc:
                result = exc
            return connection_id, result
        finally:
            connection.close()

    with patch("orders.services.now", return_value=NOW):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit, user_id) for user_id in user_ids]
            results = [future.result(timeout=40) for future in futures]

    assert results[0][0] != results[1][0]
    outcomes = [result for _, result in results]
    errors = [result for result in outcomes if isinstance(result, OrderError)]
    assert len(errors) == 1
    assert Order.objects.count() == 1
    assert OrderItem.objects.count() == 1
    return outcomes


def test_concurrent_users_cannot_exceed_global_limit(django_user_model) -> None:
    users = [django_user_model.objects.create_user(username=f"buyer-{index}") for index in range(2)]
    category = Category.objects.create(name="Books")
    good = Good.objects.create(name="Book", category=category, price=Decimal("100"))
    promo = PromoCode.objects.create(
        code="ONLYONCE",
        discount=Decimal("0.1"),
        expires_at=NOW + timedelta(days=1),
        max_uses=1,
    )

    outcomes = run_concurrently((users[0].pk, users[1].pk), good, promo)
    errors = [result for result in outcomes if isinstance(result, OrderError)]
    assert errors[0].code == "promo_limit_reached"


def test_concurrent_requests_from_same_user_cannot_reuse_promo(django_user_model) -> None:
    user = django_user_model.objects.create_user(username="buyer")
    category = Category.objects.create(name="Books")
    good = Good.objects.create(name="Book", category=category, price=Decimal("100"))
    promo = PromoCode.objects.create(
        code="REUSE",
        discount=Decimal("0.1"),
        expires_at=NOW + timedelta(days=1),
        max_uses=10,
    )

    outcomes = run_concurrently((user.pk, user.pk), good, promo)
    errors = [result for result in outcomes if isinstance(result, OrderError)]
    assert errors[0].code == "promo_already_used"


def test_concurrent_retries_with_same_key_create_one_order(django_user_model) -> None:
    user = django_user_model.objects.create_user(username="idempotent-buyer")
    category = Category.objects.create(name="Books")
    good = Good.objects.create(name="Book", category=category, price=Decimal("100"))
    barrier = Barrier(2)

    def submit():
        connection = connections["default"]
        connection.ensure_connection()
        try:
            barrier.wait(timeout=10)
            return create_order(
                actor_id=user.pk,
                user_id=user.pk,
                goods=[{"good_id": good.pk, "quantity": 1}],
                idempotency_key="concurrent-retry",
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=40) for future in [pool.submit(submit) for _ in range(2)]]

    assert results[0][0] == results[1][0]
    assert {results[0][1], results[1][1]} == {False, True}
    assert Order.objects.count() == 1
