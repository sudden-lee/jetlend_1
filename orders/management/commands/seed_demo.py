from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.timezone import now

from orders.models import Category, Good, PromoCode


class Command(BaseCommand):
    help = "Create demo goods and a promo code without changing existing objects."

    @transaction.atomic
    def handle(self, *args: str, **options: object) -> None:
        current_time = now()
        category, _ = Category.objects.get_or_create(name="Demo")
        good = (
            Good.objects.filter(
                name="Demo good",
                category=category,
                price=Decimal("100"),
                excluded_from_promotions=False,
            )
            .order_by("pk")
            .first()
        )
        if good is None:
            good = Good.objects.create(
                name="Demo good",
                category=category,
                price=Decimal("100"),
            )
        promo, created = PromoCode.objects.get_or_create(
            code="SUMMER2025",
            defaults={
                "discount": Decimal("0.1"),
                "expires_at": current_time + timedelta(days=30),
                "max_uses": 100,
            },
        )
        if not created and (
            promo.discount != Decimal("0.1")
            or promo.expires_at <= current_time
            or promo.max_uses != 100
            or promo.category_id is not None
        ):
            raise CommandError(
                "SUMMER2025 already exists with values incompatible with the documented demo."
            )
        self.stdout.write(f"good_id={good.pk}, price={good.price}")
        self.stdout.write(f"promo_code={promo.code}, expires_at={promo.expires_at.isoformat()}")
