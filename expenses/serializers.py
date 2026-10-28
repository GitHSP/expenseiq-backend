from rest_framework import serializers
from .models        import Expense, Income


class ExpenseSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Expense
        fields = ['id', 'title', 'amount', 'original_amount', 'original_currency', 'category', 'date', 'tags', 'notes', 'created_at']
        read_only_fields = ['id', 'created_at']

    def validate(self, attrs):
        return clean_original(attrs, self.instance)


class IncomeSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Income
        fields = ['id', 'title', 'amount', 'original_amount', 'original_currency', 'category', 'date', 'notes', 'created_at']
        read_only_fields = ['id', 'created_at']

    def validate(self, attrs):
        return clean_original(attrs, self.instance)


CURRENCIES = {"CAD", "USD", "GBP", "EUR", "AUD", "INR", "AED"}


def clean_original(attrs, instance=None):
    """original_currency must be a supported code; without one (or for CAD)
    there's no separate original amount. A new CAD amount sent without an
    original (e.g. edited by Aurora) drops the old original."""
    sent_original = "original_currency" in attrs or "original_amount" in attrs
    if "amount" in attrs and not sent_original and instance is not None and getattr(instance, "original_currency", ""):
        attrs["original_currency"], attrs["original_amount"] = "", None
        return attrs
    if "original_currency" in attrs or "original_amount" in attrs:
        cur = str(attrs.get("original_currency") or "").upper()
        if cur and cur not in CURRENCIES:
            raise serializers.ValidationError({"original_currency": "Unsupported currency."})
        if not cur or cur == "CAD" or attrs.get("original_amount") is None:
            attrs["original_currency"], attrs["original_amount"] = "", None
        else:
            attrs["original_currency"] = cur
            if attrs["original_amount"] < 0:
                raise serializers.ValidationError({"original_amount": "Must be positive."})
    return attrs
