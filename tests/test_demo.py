from contextlib import redirect_stdout
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from orders.models import Category, Good, Order, OrderItem, PromoCode
from scripts import create_order as client_script
from tests.conftest import NOW


@pytest.mark.django_db
def test_seed_is_idempotent_with_duplicate_named_goods() -> None:
    category = Category.objects.create(name="Demo")
    Good.objects.create(name="Demo good", category=category, price=Decimal("0.01"))
    first = Good.objects.create(name="Demo good", category=category, price=Decimal("100"))
    Good.objects.create(name="Demo good", category=category, price=Decimal("100"))

    outputs = []
    with patch("orders.management.commands.seed_demo.now", return_value=NOW):
        for _ in range(2):
            output = StringIO()
            call_command("seed_demo", stdout=output)
            outputs.append(output.getvalue())

    assert all(f"good_id={first.pk}" in output for output in outputs)
    assert Good.objects.count() == 3
    promo = PromoCode.objects.get(code="SUMMER2025")
    assert promo.discount == Decimal("0.1")
    assert promo.category_id is None


@pytest.mark.django_db
def test_seed_rejects_conflicting_promo_without_partial_changes() -> None:
    promo = PromoCode.objects.create(
        code="SUMMER2025",
        discount=Decimal("0.1"),
        expires_at=NOW,
        max_uses=100,
    )
    with (
        patch("orders.management.commands.seed_demo.now", return_value=NOW),
        pytest.raises(CommandError, match="incompatible with the documented demo"),
    ):
        call_command("seed_demo", stdout=StringIO())

    assert not Category.objects.filter(name="Demo").exists()
    assert not Good.objects.exists()
    promo.refresh_from_db()
    assert promo.expires_at == NOW


@pytest.mark.django_db(transaction=True)
def test_documented_client_flow(live_server, django_user_model) -> None:
    with (
        patch("orders.management.commands.seed_demo.now", return_value=NOW),
        patch("orders.services.now", return_value=NOW),
    ):
        user = django_user_model.objects.create_user(
            username="local-client", password="test-password-only"
        )
        call_command("seed_demo", stdout=StringIO())
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
            patch.object(client_script, "BASE_URL", live_server.url),
            patch("sys.argv", arguments),
            patch("builtins.input", return_value=user.username),
            patch("getpass.getpass", return_value="test-password-only"),
            patch("http.cookiejar.time.time", return_value=NOW.timestamp()),
            redirect_stdout(output),
        ):
            client_script.main()

    assert "HTTP 201" in output.getvalue()
    assert Order.objects.get().total == Decimal("180")
    assert OrderItem.objects.get().quantity == 2
