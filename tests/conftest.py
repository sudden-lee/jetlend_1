from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from orders.models import Category, Good, PromoCode

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fast_password_hasher(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="buyer", password="test-password-only")


@pytest.fixture
def other_user(django_user_model):
    return django_user_model.objects.create_user(
        username="other-buyer", password="test-password-only"
    )


@pytest.fixture
def catalog(db):
    category = Category.objects.create(name="Books")
    other_category = Category.objects.create(name="Games")
    return {
        "category": category,
        "other_category": other_category,
        "good": Good.objects.create(name="Book", category=category, price=Decimal("100")),
        "other_good": Good.objects.create(
            name="Game", category=other_category, price=Decimal("100")
        ),
        "excluded_good": Good.objects.create(
            name="Excluded book",
            category=category,
            price=Decimal("100"),
            excluded_from_promotions=True,
        ),
    }


@pytest.fixture
def promo(db):
    return PromoCode.objects.create(
        code="SUMMER2025",
        discount=Decimal("0.1"),
        expires_at=NOW + timedelta(days=1),
        max_uses=10,
    )


@pytest.fixture
def api_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def payload(user, catalog, promo):
    return {
        "user_id": user.pk,
        "goods": [{"good_id": catalog["good"].pk, "quantity": 2}],
        "promo_code": promo.code,
    }
