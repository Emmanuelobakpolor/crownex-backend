"""DRF serializers for VTU (airtime/data) purchases."""

from rest_framework import serializers

from .models import VTUTransaction


class BuyAirtimeSerializer(serializers.Serializer):
    subcategory_id = serializers.CharField(max_length=10)
    network = serializers.CharField(max_length=30)
    phone = serializers.CharField(max_length=15)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    transaction_pin = serializers.CharField(max_length=4, min_length=4)


class BuyDataSerializer(serializers.Serializer):
    subcategory_id = serializers.CharField(max_length=10)
    network = serializers.CharField(max_length=30)
    plan_id = serializers.CharField(max_length=100)
    phone = serializers.CharField(max_length=15)
    transaction_pin = serializers.CharField(max_length=4, min_length=4)


class VTUTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = VTUTransaction
        fields = [
            'id',
            'service',
            'network',
            'plan_id',
            'phone',
            'amount',
            'status',
            'reference',
            'provider_response',
            'note',
            'created_at',
        ]
        read_only_fields = fields
