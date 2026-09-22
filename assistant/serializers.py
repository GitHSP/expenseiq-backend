from rest_framework import serializers
from .models import EncryptionKey, SyncedMessage


class EncryptionKeySerializer(serializers.ModelSerializer):
    class Meta:
        model = EncryptionKey
        fields = ["wrapped_dek", "wrap_iv", "salt", "kdf_iterations", "updated_at"]
        read_only_fields = ["updated_at"]


class SyncedMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = SyncedMessage
        fields = ["client_id", "ciphertext", "iv", "created_at"]
        read_only_fields = ["created_at"]
