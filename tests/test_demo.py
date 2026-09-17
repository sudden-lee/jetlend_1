from decimal import Decimal
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from orders.models import Category, Good, PromoCode
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
