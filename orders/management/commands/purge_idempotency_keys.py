from django.core.management.base import BaseCommand
from django.utils.timezone import now

from orders.models import OrderIdempotencyKey


class Command(BaseCommand):
    help = "Delete expired order idempotency keys."

    def handle(self, *args, **options) -> None:
        deleted, _ = OrderIdempotencyKey.objects.filter(expires_at__lte=now()).delete()
        self.stdout.write(f"deleted={deleted}")
