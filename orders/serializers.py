from collections.abc import Mapping
from decimal import Decimal

from rest_framework import serializers


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            unknown = set(data) - set(self.fields)
            if unknown:
                raise serializers.ValidationError(
                    {name: "Unknown field." for name in sorted(unknown)}
                )
        return super().to_internal_value(data)


class StrictIntegerField(serializers.IntegerField):
    def to_internal_value(self, data):
        if type(data) is not int:
            self.fail("invalid")
        return super().to_internal_value(data)


class StrictRegexField(serializers.RegexField):
    def to_internal_value(self, data):
        if type(data) is not str:
            self.fail("invalid")
        return super().to_internal_value(data)


class NormalizedDecimalField(serializers.DecimalField):
    def to_representation(self, value) -> str:
        return format(Decimal(value).normalize(), "f")


class OrderGoodInputSerializer(StrictSerializer):
    good_id = StrictIntegerField(min_value=1, max_value=2**63 - 1)
    quantity = StrictIntegerField(min_value=1, max_value=10_000)


class OrderCreateSerializer(StrictSerializer):
    user_id = StrictIntegerField(min_value=1, max_value=2**63 - 1)
    goods = OrderGoodInputSerializer(many=True, min_length=1, max_length=100)
    promo_code = StrictRegexField(
        r"\A[A-Za-z0-9_-]{1,64}\Z",
        required=False,
    )

    def validate_goods(self, goods):
        good_ids = [item["good_id"] for item in goods]
        if len(good_ids) != len(set(good_ids)):
            raise serializers.ValidationError("Duplicate good_id; combine quantities in one item.")
        return goods


class OrderGoodOutputSerializer(serializers.Serializer):
    good_id = serializers.IntegerField()
    quantity = serializers.IntegerField()
    price = NormalizedDecimalField(max_digits=10, decimal_places=2)
    discount = NormalizedDecimalField(max_digits=5, decimal_places=4)
    total = NormalizedDecimalField(max_digits=16, decimal_places=2)


class OrderOutputSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()
    order_id = serializers.IntegerField()
    goods = OrderGoodOutputSerializer(many=True)
    price = NormalizedDecimalField(max_digits=16, decimal_places=2)
    discount = NormalizedDecimalField(max_digits=5, decimal_places=4)
    total = NormalizedDecimalField(max_digits=16, decimal_places=2)
