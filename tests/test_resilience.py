from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.db import OperationalError
from django.utils.timezone import now
from rest_framework.throttling import ScopedRateThrottle

from orders.models import Order, OrderIdempotencyKey, PromoCodeRedemption
from orders.services import create_order
from tests.test_api import URL, post_order

pytestmark = pytest.mark.django_db


def test_same_idempotency_key_replays_order(api_client, payload, promo) -> None:
    first = post_order(api_client, payload, "same-request")
    second = post_order(api_client, payload, "same-request")

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert first["Idempotency-Replayed"] == "false"
    assert second["Idempotency-Replayed"] == "true"
    assert Order.objects.count() == 1
    assert PromoCodeRedemption.objects.count() == 1
    promo.refresh_from_db()
    assert promo.uses_count == 1


def test_same_idempotency_key_rejects_different_payload(api_client, payload) -> None:
    assert post_order(api_client, payload, "changed-request").status_code == 201
    payload["goods"][0]["quantity"] += 1

    response = post_order(api_client, payload, "changed-request")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"
    assert Order.objects.count() == 1


@pytest.mark.parametrize("key", [None, "", "contains space", " padded ", "x" * 129])
def test_idempotency_key_is_required_and_validated(api_client, payload, key) -> None:
    kwargs = {} if key is None else {"HTTP_IDEMPOTENCY_KEY": key}
    response = api_client.post(URL, payload, format="json", **kwargs)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_idempotency_key"
    assert not Order.objects.exists()


def test_order_endpoint_is_throttled(api_client, payload, monkeypatch) -> None:
    payload.pop("promo_code")
    cache.clear()
    monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", {"orders": "1/min"})

    assert post_order(api_client, payload, "throttle-first").status_code == 201
    response = post_order(api_client, payload, "throttle-second")

    assert response.status_code == 429
    assert Order.objects.count() == 1


def test_purge_idempotency_keys_deletes_only_expired(api_client, payload) -> None:
    assert post_order(api_client, payload, "expired-key").status_code == 201
    expired = OrderIdempotencyKey.objects.get()
    expired.expires_at = now() - timedelta(seconds=1)
    expired.save(update_fields=["expires_at"])
    OrderIdempotencyKey.objects.create(
        user_id=expired.user_id,
        key="current-key",
        request_hash="0" * 64,
        expires_at=now() + timedelta(hours=1),
    )
    output = StringIO()

    call_command("purge_idempotency_keys", stdout=output)

    assert output.getvalue().strip() == "deleted=1"
    assert list(OrderIdempotencyKey.objects.values_list("key", flat=True)) == ["current-key"]


class DatabaseFailure(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def operational_error(sqlstate: str) -> OperationalError:
    error = OperationalError("database failure")
    error.__cause__ = DatabaseFailure(sqlstate)
    return error


def test_retry_is_bounded_to_transient_postgresql_errors() -> None:
    result = ({"order_id": 1}, False)
    goods = [{"good_id": 1, "quantity": 1}]
    with (
        patch(
            "orders.services._create_order_once",
            side_effect=[operational_error("40P01"), operational_error("40001"), result],
        ) as create_once,
        patch("orders.services.time.sleep") as sleep,
    ):
        assert (
            create_order(
                actor_id=1,
                user_id=1,
                goods=goods,
                idempotency_key="retryable",
            )
            == result
        )

    assert create_once.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [0.02, 0.04]


def test_unknown_database_error_is_not_retried() -> None:
    error = operational_error("08006")
    goods = [{"good_id": 1, "quantity": 1}]
    with (
        patch("orders.services._create_order_once", side_effect=error) as create_once,
        patch("orders.services.time.sleep") as sleep,
        pytest.raises(OperationalError),
    ):
        create_order(actor_id=1, user_id=1, goods=goods, idempotency_key="not-retryable")

    create_once.assert_called_once()
    sleep.assert_not_called()


def test_empty_goods_is_rejected_before_touching_the_database() -> None:
    with (
        patch("orders.services._create_order_once") as create_once,
        pytest.raises(ValueError, match="goods must not be empty"),
    ):
        create_order(actor_id=1, user_id=1, goods=[], idempotency_key="empty-goods")

    create_once.assert_not_called()


def test_unrecognized_database_error_is_not_swallowed_by_the_view(api_client, payload) -> None:
    with (
        patch("orders.services._create_order_once", side_effect=operational_error("08006")),
        pytest.raises(OperationalError),
    ):
        post_order(api_client, payload, "db-down")

    assert not Order.objects.exists()
