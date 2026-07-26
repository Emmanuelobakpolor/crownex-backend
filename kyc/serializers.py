"""DRF serializers for identity verification (KYC)."""

from rest_framework import serializers

from .models import KycVerification


class KycVerificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = KycVerification
        fields = [
            'id_type',
            'id_number',
            'full_name',
            'date_of_birth',
            'address',
            'match_score',
            'status',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields


class SubmitVerificationSerializer(serializers.Serializer):
    id_type = serializers.ChoiceField(choices=['nin', 'bvn'])
    id_number = serializers.CharField(min_length=5, max_length=32)
    address = serializers.CharField(required=False, allow_blank=True, default='')
    selfie = serializers.ImageField()
    id_document = serializers.ImageField(required=False)

    def validate_id_number(self, value: str) -> str:
        return value.strip()
