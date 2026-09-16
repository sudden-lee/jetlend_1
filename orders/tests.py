import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO
from threading import Barrier
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connections, transaction
from django.middleware.csrf import get_token
from django.test import Client, LiveServerTestCase, RequestFactory, TestCase, TransactionTestCase

from orders.models import Category, Good, Order, OrderItem, PromoCode, PromoCodeRedemption
from orders.services import GoodsInput, OrderError, OrderInput, OrderResponse, create_order
from scripts import create_order as client_script

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
URL = "/api/orders/"


class OrderAPITests(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = get_user_model().objects.create_user(username="buyer")
        cls.other_user = get_user_model().objects.create_user(username="other-buyer")
        cls.category = Category.objects.create(name="Books")
        cls.other_category = Category.objects.create(name="Games")
        cls.good = Good.objects.create(name="Book", category=cls.category, price_cents=10_000)
        cls.other_good = Good.objects.create(
            name="Game", category=cls.other_category, price_cents=10_000
        )
        cls.excluded_good = Good.objects.create(
            name="Excluded book",
            category=cls.category,
            price_cents=10_000,
            excluded_from_promotions=True,
        )
        cls.promo = PromoCode.objects.create(
            code="SUMMER2025",
            discount=Decimal("0.1"),
            expires_at=NOW + timedelta(days=1),
            max_uses=10,
        )

    def setUp(self) -> None:
        clock = patch("orders.services.timezone.now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.client.force_login(self.user)
        self.payload = {
            "user_id": self.user.pk,
            "goods": [{"good_id": self.good.pk, "quantity": 2}],
            "promo_code": self.promo.code,
        }

    def assert_error(self, payload: object, status: int, code: str) -> None:
        orders_before = Order.objects.count()
        items_before = OrderItem.objects.count()
        response = self.client.post(URL, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertEqual(Order.objects.count(), orders_before)
        self.assertEqual(OrderItem.objects.count(), items_before)

    def test_example_and_price_snapshot(self) -> None:
        response = self.client.post(URL, self.payload, content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)
        order = Order.objects.get()
        self.assertEqual(
            response.json(),
            {
                "user_id": self.user.pk,
                "order_id": order.pk,
                "goods": [
                    {
                        "good_id": self.good.pk,
                        "quantity": 2,
                        "price": 100,
                        "discount": "0.1",
                        "total": 180,
                    }
                ],
                "price": 200,
                "discount": "0.1",
                "total": 180,
            },
        )
        self.assertEqual(order.price_cents, 20_000)
        self.assertEqual(order.total_cents, 18_000)
        self.assertEqual(order.discount, Decimal("0.1"))
        self.assertEqual(order.promo_code_id, self.promo.pk)
        Good.objects.filter(pk=self.good.pk).update(price_cents=20_000)
        item = order.items.get()
        self.assertEqual(item.price_cents, 10_000)
        self.assertEqual(item.total_cents, 18_000)

    def test_orders_without_promo_can_be_created_repeatedly(self) -> None:
        self.payload.pop("promo_code")
        for _ in range(3):
            response = self.client.post(URL, self.payload, content_type="application/json")
            self.assertEqual(response.status_code, 201, response.content)
            self.assertEqual(response.json()["discount"], "0")
            self.assertEqual(response.json()["total"], 200)
        self.assertEqual(Order.objects.filter(promo_code__isnull=True).count(), 3)

    def test_mixed_basket_only_discounts_eligible_goods(self) -> None:
        self.promo.category = self.category
        self.promo.save(update_fields=["category"])
        self.payload["goods"] = [
            {"good_id": good.pk, "quantity": 1}
            for good in (self.good, self.other_good, self.excluded_good)
        ]
        response = self.client.post(URL, self.payload, content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual([item["discount"] for item in data["goods"]], ["0.1", "0", "0"])
        self.assertEqual([item["total"] for item in data["goods"]], [90, 100, 100])
        self.assertEqual(data["price"], 300)
        self.assertEqual(data["total"], 290)
        self.assertEqual(data["discount"], "0.0333")
        self.assertEqual(Order.objects.get().discount, Decimal("0.0333"))

    def test_excluded_goods_are_ineligible_even_for_unrestricted_promo(self) -> None:
        self.payload["goods"] = [{"good_id": self.excluded_good.pk, "quantity": 1}]
        self.assert_error(self.payload, 422, "promo_not_applicable")
        self.promo.category = self.category
        self.promo.save(update_fields=["category"])
        self.payload["goods"] = [{"good_id": self.other_good.pk, "quantity": 1}]
        self.assert_error(self.payload, 422, "promo_not_applicable")

    def test_expiration_includes_exact_boundary(self) -> None:
        for expiration in (NOW, NOW - timedelta(microseconds=1)):
            with self.subTest(expiration=expiration):
                self.promo.expires_at = expiration
                self.promo.save(update_fields=["expires_at"])
                self.assert_error(self.payload, 422, "promo_expired")

    def test_unknown_promo(self) -> None:
        self.payload["promo_code"] = "UNKNOWN"
        self.assert_error(self.payload, 422, "promo_not_found")

    def test_promo_limit(self) -> None:
        self.promo.max_uses = 1
        self.promo.save(update_fields=["max_uses"])
        create_order(
            OrderInput(self.other_user.pk, (GoodsInput(self.good.pk, 1),), self.promo.code),
            actor_id=self.other_user.pk,
        )
        self.assert_error(self.payload, 409, "promo_limit_reached")

    def test_user_cannot_reuse_promo(self) -> None:
        response = self.client.post(URL, self.payload, content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assert_error(self.payload, 409, "promo_already_used")

    def test_full_discount(self) -> None:
        self.promo.discount = Decimal("1")
        self.promo.save(update_fields=["discount"])
        response = self.client.post(URL, self.payload, content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["total"], 0)
        self.assertEqual(response.json()["discount"], "1")
        self.assertEqual(response.json()["goods"][0]["total"], 0)

    def test_fractional_amount_rounds_half_up_per_line(self) -> None:
        self.good.price_cents = 101
        self.good.save(update_fields=["price_cents"])
        self.promo.discount = Decimal("0.5")
        self.promo.save(update_fields=["discount"])
        self.payload["goods"][0]["quantity"] = 1
        response = self.client.post(URL, self.payload, content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["price"], 1.01)
        self.assertEqual(response.json()["total"], 0.51)
        self.assertEqual(response.json()["goods"][0]["discount"], "0.5")
        self.assertEqual(response.json()["discount"], "0.495")
        self.assertEqual(Order.objects.get().total_cents, 51)

    def test_rounding_can_reduce_effective_discount_to_zero(self) -> None:
        self.good.price_cents = 5
        self.good.save(update_fields=["price_cents"])
        self.payload["goods"][0]["quantity"] = 1
        response = self.client.post(URL, self.payload, content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["total"], 0.05)
        self.assertEqual(response.json()["goods"][0]["discount"], "0.1")
        self.assertEqual(response.json()["discount"], "0")
        self.assert_error(self.payload, 409, "promo_already_used")

    def test_input_dtos_enforce_service_invariants(self) -> None:
        cases = {
            "empty goods": lambda: OrderInput(self.user.pk, ()),
            "zero quantity": lambda: GoodsInput(self.good.pk, 0),
            "excessive quantity": lambda: GoodsInput(self.good.pk, 10_001),
            "duplicate good": lambda: OrderInput(
                self.user.pk,
                (GoodsInput(self.good.pk, 1), GoodsInput(self.good.pk, 2)),
            ),
        }
        for name, factory in cases.items():
            with self.subTest(name=name), self.assertRaises(OrderError) as caught:
                factory()
            self.assertEqual(caught.exception.code, "invalid_request")

    def test_invalid_payloads(self) -> None:
        cases: list[object] = [None, [], "order", {}, {**self.payload, "extra": 1}]
        for value in (None, True, False, 0, -1, 1.5, "1", 2**63):
            cases.append({**self.payload, "user_id": value})
        for value in (None, {}, "goods", [], [{}] * 101):
            cases.append({**self.payload, "goods": value})
        for field in ("good_id", "quantity"):
            for value in (None, True, False, 0, -1, 1.5, "1"):
                item = {"good_id": self.good.pk, "quantity": 1, field: value}
                cases.append({**self.payload, "goods": [item]})
        cases.extend(
            [
                {**self.payload, "goods": [{"good_id": self.good.pk}]},
                {**self.payload, "goods": [{"good_id": 2**63, "quantity": 1}]},
                {**self.payload, "goods": [{"good_id": self.good.pk, "quantity": 10_001}]},
                {**self.payload, "goods": [{"good_id": self.good.pk, "quantity": 1, "x": 1}]},
                {**self.payload, "goods": self.payload["goods"] * 2},
            ]
        )
        for value in (None, "", " ", "a" * 65, "SUMMER 2025", 1, True):
            cases.append({**self.payload, "promo_code": value})
        for payload in cases:
            with self.subTest(payload=payload):
                self.assert_error(payload, 400, "invalid_request")

    def test_unknown_goods_does_not_consume_promo(self) -> None:
        payload = deepcopy(self.payload)
        payload["goods"].append({"good_id": self.excluded_good.pk + 1000, "quantity": 1})
        self.assert_error(payload, 404, "goods_not_found")
        response = self.client.post(URL, self.payload, content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)

    def test_authentication_and_ownership(self) -> None:
        self.payload["user_id"] = self.other_user.pk
        self.assert_error(self.payload, 403, "forbidden")
        self.client.logout()
        self.assert_error(self.payload, 401, "unauthenticated")

    def test_inactive_actor_is_rejected_by_service(self) -> None:
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        with self.assertRaises(OrderError) as caught:
            create_order(
                OrderInput(self.user.pk, (GoodsInput(self.good.pk, 1),)), actor_id=self.user.pk
            )
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(Order.objects.count(), 0)

    def test_csrf_token_is_required_and_valid_token_is_accepted(self) -> None:
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(URL, self.payload, content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "csrf_failed")
        self.assertEqual(Order.objects.count(), 0)
        request = RequestFactory().get(URL)
        token = get_token(request)
        client.cookies[settings.CSRF_COOKIE_NAME] = request.META["CSRF_COOKIE"]
        response = client.post(
            URL, self.payload, content_type="application/json", HTTP_X_CSRFTOKEN=token
        )
        self.assertEqual(response.status_code, 201, response.content)

    def test_http_method_content_type_and_malformed_body(self) -> None:
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "POST")
        response = self.client.post(URL, "{}", content_type="text/plain")
        self.assertEqual(response.status_code, 415)
        for body in ("{", "", b"\xff"):
            with self.subTest(body=body):
                response = self.client.post(
                    URL, body, content_type="application/json", CONTENT_TYPE="application/json"
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "invalid_json")
        response = self.client.post(URL, " " * 65_537, content_type="application/json")
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "request_too_large")
        self.assertEqual(Order.objects.count(), 0)

    def test_item_insert_failure_rolls_back_order_and_promo_usage(self) -> None:
        data = OrderInput(self.user.pk, (GoodsInput(self.good.pk, 2),), self.promo.code)
        with patch(
            "orders.services.OrderItem.objects.bulk_create", side_effect=RuntimeError("fail")
        ):
            with self.assertRaisesRegex(RuntimeError, "fail"):
                create_order(data, actor_id=self.user.pk)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(PromoCodeRedemption.objects.count(), 0)
        result = create_order(data, actor_id=self.user.pk)
        self.assertEqual(result["total"], 180)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(PromoCodeRedemption.objects.count(), 1)

    def test_promo_redemption_survives_order_deletion(self) -> None:
        result = create_order(
            OrderInput(self.user.pk, (GoodsInput(self.good.pk, 1),), self.promo.code),
            actor_id=self.user.pk,
        )
        Order.objects.get(pk=result["order_id"]).delete()
        redemption = PromoCodeRedemption.objects.get()
        self.assertIsNone(redemption.order_id)

        with self.assertRaises(OrderError) as caught:
            create_order(
                OrderInput(self.user.pk, (GoodsInput(self.good.pk, 1),), self.promo.code),
                actor_id=self.user.pk,
            )
        self.assertEqual(caught.exception.code, "promo_already_used")
        self.assertEqual(Order.objects.count(), 0)

    def test_database_discount_lower_bound_matches_model_validator(self) -> None:
        valid = PromoCode.objects.create(
            code="MINIMUM",
            discount=Decimal("0.0001"),
            expires_at=NOW + timedelta(days=1),
            max_uses=1,
        )
        valid.refresh_from_db()
        self.assertEqual(valid.discount, Decimal("0.0001"))

        with self.assertRaises(IntegrityError), transaction.atomic():
            PromoCode.objects.create(
                code="TOO_SMALL",
                discount=Decimal("0.00001"),
                expires_at=NOW + timedelta(days=1),
                max_uses=1,
            )

    def test_database_uniqueness_defends_against_direct_promo_reuse(self) -> None:
        fields = {
            "user": self.user,
            "promo_code": self.promo,
            "price_cents": 10_000,
            "discount": Decimal("0.1"),
            "total_cents": 9000,
        }
        Order.objects.create(**fields)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Order.objects.create(**fields)
        self.assertEqual(Order.objects.count(), 1)


class ConcurrentPromoTests(TransactionTestCase):
    def setUp(self) -> None:
        self.users = [
            get_user_model().objects.create_user(username=f"buyer-{index}") for index in range(2)
        ]
        category = Category.objects.create(name="Books")
        self.good = Good.objects.create(name="Book", category=category, price_cents=10_000)
        self.promo = PromoCode.objects.create(
            code="ONLYONCE",
            discount=Decimal("0.1"),
            expires_at=NOW + timedelta(days=1),
            max_uses=1,
        )

    def run_concurrently(self, user_ids: tuple[int, int]) -> list[OrderResponse | OrderError]:
        self.assertEqual(connections["default"].vendor, "postgresql")
        database_name = str(connections["default"].settings_dict["NAME"])
        self.assertTrue(database_name)
        barrier = Barrier(2)

        def submit(user_id: int) -> tuple[int, OrderResponse | OrderError]:
            connection = connections["default"]
            connection.ensure_connection()
            connection_id = id(connection.connection)
            try:
                # Both transactions enter together; select_for_update serializes this promo.
                barrier.wait(timeout=10)
                data = OrderInput(user_id, (GoodsInput(self.good.pk, 1),), self.promo.code)
                try:
                    return connection_id, create_order(data, actor_id=user_id)
                except OrderError as exc:
                    return connection_id, exc
            finally:
                connection.close()

        with patch("orders.services.timezone.now", return_value=NOW):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(submit, user_id) for user_id in user_ids]
                results = [future.result(timeout=40) for future in futures]
        self.assertNotEqual(results[0][0], results[1][0])
        outcomes = [result for _, result in results]
        errors = [result for result in outcomes if isinstance(result, OrderError)]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].status, 409)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderItem.objects.count(), 1)
        return outcomes

    def test_concurrent_users_cannot_exceed_global_limit(self) -> None:
        outcomes = self.run_concurrently((self.users[0].pk, self.users[1].pk))
        errors = [result for result in outcomes if isinstance(result, OrderError)]
        self.assertEqual(errors[0].code, "promo_limit_reached")

    def test_concurrent_requests_from_same_user_cannot_reuse_promo(self) -> None:
        self.promo.max_uses = 10
        self.promo.save(update_fields=["max_uses"])
        outcomes = self.run_concurrently((self.users[0].pk, self.users[0].pk))
        errors = [result for result in outcomes if isinstance(result, OrderError)]
        self.assertEqual(errors[0].code, "promo_already_used")


class SeedDemoTests(TestCase):
    def test_seed_is_idempotent_with_duplicate_named_goods(self) -> None:
        category = Category.objects.create(name="Demo")
        Good.objects.create(name="Demo good", category=category, price_cents=1)
        first = Good.objects.create(name="Demo good", category=category, price_cents=10_000)
        Good.objects.create(name="Demo good", category=category, price_cents=10_000)

        outputs = []
        with patch("orders.management.commands.seed_demo.timezone.now", return_value=NOW):
            for _ in range(2):
                output = StringIO()
                call_command("seed_demo", stdout=output)
                outputs.append(output.getvalue())

        self.assertTrue(all(f"good_id={first.pk}" in output for output in outputs))
        self.assertEqual(Good.objects.count(), 3)
        promo = PromoCode.objects.get(code="SUMMER2025")
        self.assertEqual(promo.discount, Decimal("0.1"))
        self.assertEqual(promo.expires_at, NOW + timedelta(days=30))
        self.assertEqual(promo.max_uses, 100)
        self.assertIsNone(promo.category_id)

    def test_seed_rejects_conflicting_promo_without_partial_changes(self) -> None:
        promo = PromoCode.objects.create(
            code="SUMMER2025",
            discount=Decimal("0.1"),
            expires_at=NOW,
            max_uses=100,
        )
        with (
            patch("orders.management.commands.seed_demo.timezone.now", return_value=NOW),
            self.assertRaisesMessage(CommandError, "incompatible with the documented demo"),
        ):
            call_command("seed_demo", stdout=StringIO())

        self.assertFalse(Category.objects.filter(name="Demo").exists())
        self.assertFalse(Good.objects.exists())
        promo.refresh_from_db()
        self.assertEqual(promo.expires_at, NOW)


class LocalClientTests(LiveServerTestCase):
    host = "127.0.0.1"

    def test_documented_seed_login_and_client_create_an_order(self) -> None:
        with patch("django.utils.timezone.now", return_value=NOW):
            user = get_user_model().objects.create_user(
                username="local-client", password="test-password-only"
            )
            for _ in range(2):
                call_command("seed_demo", stdout=StringIO())
            self.assertEqual(Category.objects.count(), 1)
            self.assertEqual(Good.objects.count(), 1)
            self.assertEqual(PromoCode.objects.count(), 1)
            good = Good.objects.get()
            arguments = [
                "create_order.py",
                "--user-id",
                str(user.pk),
                "--good-id",
                str(good.pk),
                "--promo-code",
                "SUMMER2025",
            ]
            output = StringIO()
            with (
                patch.object(client_script, "BASE_URL", self.live_server_url),
                patch("sys.argv", arguments),
                patch("builtins.input", return_value=user.username),
                patch("getpass.getpass", return_value="test-password-only"),
                patch("http.cookiejar.time.time", return_value=NOW.timestamp()),
                redirect_stdout(output),
            ):
                client_script.main()
        self.assertIn("HTTP 201", output.getvalue())
        self.assertEqual(Order.objects.get().total_cents, 18_000)
        self.assertEqual(OrderItem.objects.get().quantity, 2)
