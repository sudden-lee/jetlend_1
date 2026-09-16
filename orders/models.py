from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone


class Category(models.Model):
    name = models.CharField("название", max_length=120, unique=True)

    class Meta:
        verbose_name = "категория"
        verbose_name_plural = "категории"

    def __str__(self) -> str:
        return self.name


class Good(models.Model):
    name = models.CharField("название", max_length=200)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, verbose_name="категория")
    price_cents = models.PositiveBigIntegerField(
        "цена в копейках",
        validators=[MinValueValidator(1), MaxValueValidator(1_000_000_000)],
        help_text="Цена одной единицы товара: 100 = 1,00 денежной единицы.",
    )
    excluded_from_promotions = models.BooleanField("исключён из акций", default=False)

    class Meta:
        verbose_name = "товар"
        verbose_name_plural = "товары"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(price_cents__gte=1, price_cents__lte=1_000_000_000),
                name="good_price_range",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class PromoCode(models.Model):
    code = models.CharField(
        "код",
        max_length=64,
        unique=True,
        validators=[RegexValidator(r"\A[A-Za-z0-9_-]{1,64}\Z")],
    )
    discount = models.DecimalField(
        "ставка скидки",
        max_digits=5,
        decimal_places=4,
        validators=[MinValueValidator(Decimal("0.0001")), MaxValueValidator(Decimal("1"))],
        help_text="Доля от 0,0001 до 1: значение 0,1 означает скидку 10%.",
    )
    expires_at = models.DateTimeField("действует до")
    max_uses = models.PositiveIntegerField(
        "максимум использований", validators=[MinValueValidator(1)]
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        verbose_name="категория товаров",
        help_text="Оставьте пустым, чтобы промокод действовал на все разрешённые товары.",
    )

    class Meta:
        verbose_name = "промокод"
        verbose_name_plural = "промокоды"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(discount__gte=Decimal("0.0001"), discount__lte=1),
                name="promo_discount_range",
            ),
            models.CheckConstraint(
                condition=models.Q(max_uses__gte=1), name="promo_positive_limit"
            ),
        ]

    def __str__(self) -> str:
        return self.code


class Order(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, verbose_name="пользователь"
    )
    promo_code = models.ForeignKey(
        PromoCode,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        verbose_name="промокод",
    )
    created_at = models.DateTimeField("создан", auto_now_add=True)
    price_cents = models.PositiveBigIntegerField("стоимость до скидки, в копейках")
    discount = models.DecimalField(
        "фактическая ставка скидки", max_digits=5, decimal_places=4, default=0
    )
    total_cents = models.PositiveBigIntegerField("итого, в копейках")

    class Meta:
        verbose_name = "заказ"
        verbose_name_plural = "заказы"
        constraints = [
            models.UniqueConstraint(fields=["user", "promo_code"], name="order_user_promo_once"),
            models.CheckConstraint(
                condition=models.Q(total_cents__lte=models.F("price_cents")),
                name="order_total_within_price",
            ),
            models.CheckConstraint(
                condition=models.Q(discount__gte=0, discount__lte=1),
                name="order_discount_range",
            ),
        ]

    def __str__(self) -> str:
        return f"Заказ №{self.pk}" if self.pk else "Новый заказ"


class PromoCodeRedemption(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, verbose_name="пользователь"
    )
    promo_code = models.ForeignKey(PromoCode, on_delete=models.PROTECT, verbose_name="промокод")
    order = models.OneToOneField(
        Order,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="promo_redemption",
        verbose_name="заказ",
    )
    redeemed_at = models.DateTimeField("погашен", default=timezone.now, editable=False)

    class Meta:
        verbose_name = "погашение промокода"
        verbose_name_plural = "погашения промокодов"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "promo_code"], name="promo_redemption_user_code_once"
            ),
        ]

    def __str__(self) -> str:
        return f"Погашение №{self.pk}" if self.pk else "Новое погашение"


class OrderItem(models.Model):
    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="items", verbose_name="заказ"
    )
    good = models.ForeignKey(Good, on_delete=models.PROTECT, verbose_name="товар")
    quantity = models.PositiveIntegerField(
        "количество", validators=[MinValueValidator(1), MaxValueValidator(10_000)]
    )
    price_cents = models.PositiveBigIntegerField(
        "цена за единицу, в копейках", help_text="Снимок цены на момент создания заказа."
    )
    discount = models.DecimalField("ставка скидки", max_digits=5, decimal_places=4, default=0)
    total_cents = models.PositiveBigIntegerField("итого, в копейках")

    class Meta:
        verbose_name = "строка заказа"
        verbose_name_plural = "строки заказа"
        constraints = [
            models.UniqueConstraint(fields=["order", "good"], name="order_good_once"),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1, quantity__lte=10_000),
                name="item_quantity_range",
            ),
            models.CheckConstraint(
                condition=models.Q(price_cents__gte=1, price_cents__lte=1_000_000_000),
                name="item_price_range",
            ),
            models.CheckConstraint(
                condition=models.Q(discount__gte=0, discount__lte=1),
                name="item_discount_range",
            ),
            models.CheckConstraint(
                condition=models.Q(total_cents__lte=models.F("price_cents") * models.F("quantity")),
                name="item_total_within_price",
            ),
        ]

    def __str__(self) -> str:
        return f"Строка заказа №{self.order_id}: товар №{self.good_id}"
