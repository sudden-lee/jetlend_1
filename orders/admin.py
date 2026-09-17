from django.contrib import admin

from orders.models import (
    Category,
    Good,
    Order,
    OrderIdempotencyKey,
    OrderItem,
    PromoCode,
    PromoCodeRedemption,
)

admin.site.site_header = "Управление заказами"
admin.site.site_title = "Администрирование заказов"
admin.site.index_title = "Каталог, промокоды и заказы"


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ["id", "name"]
    search_fields = ["name"]
    ordering = ["name"]


@admin.register(Good)
class GoodAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "category", "price", "excluded_from_promotions"]
    list_filter = ["excluded_from_promotions", "category"]
    list_select_related = ["category"]
    search_fields = ["name", "category__name"]
    autocomplete_fields = ["category"]


@admin.register(PromoCode)
class PromoCodeAdmin(admin.ModelAdmin):
    list_display = ["code", "discount", "expires_at", "uses_count", "max_uses", "category"]
    list_filter = ["category"]
    list_select_related = ["category"]
    search_fields = ["=code", "category__name"]
    autocomplete_fields = ["category"]
    readonly_fields = ["uses_count"]


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    can_delete = False
    readonly_fields = ["good", "quantity", "price", "discount", "total"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("good")

    def has_add_permission(self, request, obj=None) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "user",
        "promo_code",
        "created_at",
        "price",
        "discount",
        "total",
    ]
    list_filter = ["created_at"]
    list_select_related = ["user", "promo_code"]
    search_fields = ["=id", "=user__username", "=promo_code__code"]
    ordering = ["-created_at"]
    readonly_fields = ["user", "promo_code", "created_at", "price", "discount", "total"]
    inlines = [OrderItemInline]

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(PromoCodeRedemption)
class PromoCodeRedemptionAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "promo_code", "order", "redeemed_at"]
    list_filter = ["redeemed_at"]
    list_select_related = ["user", "promo_code", "order"]
    search_fields = ["=id", "=user__username", "=promo_code__code", "=order__id"]
    ordering = ["-redeemed_at"]
    readonly_fields = ["user", "promo_code", "order", "redeemed_at"]

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(OrderIdempotencyKey)
class OrderIdempotencyKeyAdmin(admin.ModelAdmin):
    list_display = ["key", "user", "order", "created_at", "expires_at"]
    list_filter = ["created_at", "expires_at"]
    list_select_related = ["user", "order"]
    search_fields = ["=key", "=user__username", "=order__id"]
    ordering = ["-created_at"]
    readonly_fields = ["user", "key", "request_hash", "order", "created_at", "expires_at"]

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
