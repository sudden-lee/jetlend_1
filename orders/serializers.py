from decimal import Decimal

from rest_framework import serializers

from orders.models import PROMO_CODE_PATTERN

MAX_BIGINT = 2**63 - 1


class NormalizedDecimalField(serializers.DecimalField):
    def to_representation(self, value) -> str:
        return format(Decimal(value).normalize(), "f")


class OrderGoodInputSerializer(serializers.Serializer):
    good_id = serializers.IntegerField(min_value=1, max_value=MAX_BIGINT)
    quantity = serializers.IntegerField(min_value=1, max_value=10_000)


class OrderCreateSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(min_value=1, max_value=MAX_BIGINT)
    goods = OrderGoodInputSerializer(many=True, min_length=1, max_length=100)
    promo_code = serializers.RegexField(PROMO_CODE_PATTERN, required=False)

    def validate_goods(self, goods):
        good_ids = [item["good_id"] for item in goods]
        if len(good_ids) != len(set(good_ids)):
            raise serializers.ValidationError("Duplicate good_id; combine quantities in one item.")
        return goods


class OrderGoodOutputSerializer(serializers.Serializer):
    good_id = serializers.IntegerField()
    quantity = serializers.IntegerField()
    price = serializers.DecimalField(max_digits=10, decimal_places=2, coerce_to_string=False)
    discount = NormalizedDecimalField(max_digits=5, decimal_places=4)
    total = serializers.DecimalField(max_digits=16, decimal_places=2, coerce_to_string=False)


class OrderOutputSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()
    order_id = serializers.IntegerField()
    goods = OrderGoodOutputSerializer(many=True)
    price = serializers.DecimalField(max_digits=16, decimal_places=2, coerce_to_string=False)
    discount = NormalizedDecimalField(max_digits=5, decimal_places=4)
    total = serializers.DecimalField(max_digits=16, decimal_places=2, coerce_to_string=False)
