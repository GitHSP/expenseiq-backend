from rest_framework import serializers
from django.utils   import timezone
from expenses.serializers import CURRENCIES, clean_original
from .models import (
    Debt, EmergencyFund, MonthlyPlan, ChecklistItem, RecurringItem,
)


class DebtSerializer(serializers.ModelSerializer):
    # Read-only computed properties from model
    monthly_interest_rate  = serializers.DecimalField(max_digits=10, decimal_places=6, read_only=True)
    utilization_percent    = serializers.FloatField(read_only=True)
    monthly_interest_amount= serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model  = Debt
        fields = [
            'id', 'name', 'debt_type',
            'current_balance', 'annual_interest_rate',
            'minimum_payment', 'credit_limit',
            'due_day', 'avalanche_order',
            'is_active', 'paid_off_date', 'notes',
            'original_currency', 'original_current_balance', 'original_minimum_payment', 'original_credit_limit',
            'created_at', 'updated_at',
            # computed
            'monthly_interest_rate',
            'utilization_percent',
            'monthly_interest_amount',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    ORIGINALS = ("original_current_balance", "original_minimum_payment", "original_credit_limit")

    def validate(self, attrs):
        if "original_currency" in attrs or any(f in attrs for f in self.ORIGINALS):
            cur = str(attrs.get("original_currency") or "").upper()
            if cur and cur not in CURRENCIES:
                raise serializers.ValidationError({"original_currency": "Unsupported currency."})
            if not cur or cur == "CAD":
                attrs["original_currency"] = ""
                for f in self.ORIGINALS:
                    attrs[f] = None
            else:
                attrs["original_currency"] = cur
        # (CAD amounts edited on their own: the model moves the originals along.)
        return attrs

    def _save(self, instance, validated_data):
        instance._originals_given = "original_currency" in validated_data
        for k, v in validated_data.items():
            setattr(instance, k, v)
        instance.save()
        return instance

    def update(self, instance, validated_data):
        return self._save(instance, validated_data)


class EmergencyFundSerializer(serializers.ModelSerializer):
    is_funded        = serializers.BooleanField(read_only=True)
    progress_percent = serializers.FloatField(read_only=True)

    class Meta:
        model  = EmergencyFund
        fields = [
            'id', 'current_balance', 'target_amount',
            'monthly_contribution', 'updated_at',
            # computed
            'is_funded', 'progress_percent',
        ]
        read_only_fields = ['id', 'updated_at']


class ChecklistItemSerializer(serializers.ModelSerializer):
    class Meta:
        model  = ChecklistItem
        fields = [
            'id', 'label',
            'amount', 'original_amount', 'original_currency', 'due_day', 'category',
            'is_auto_debit', 'is_completed',
            'completed_at', 'sort_order', 'linked_debt',
            'recurring_item',
        ]
        read_only_fields = ['id', 'completed_at', 'recurring_item']

    def validate(self, attrs):
        return clean_original(attrs, self.instance)

    def validate_linked_debt(self, debt):
        # Ticking an item pays down its linked debt, so it must be the
        # caller's own debt — never someone else's, even if they guess its id.
        request = self.context.get("request")
        if debt is not None and (request is None or debt.user_id != request.user.id):
            raise serializers.ValidationError("Debt not found.")
        return debt


class RecurringItemSerializer(serializers.ModelSerializer):
    class Meta:
        model  = RecurringItem
        fields = [
            'id', 'label', 'amount', 'original_amount', 'original_currency', 'due_day', 'category',
            'frequency', 'anchor_date',
            'is_auto_debit', 'is_active', 'notes',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate(self, attrs):
        frequency = attrs.get("frequency", getattr(self.instance, "frequency", "monthly"))
        anchor = attrs.get("anchor_date", getattr(self.instance, "anchor_date", None))
        if frequency in ("weekly", "biweekly") and not anchor:
            raise serializers.ValidationError({"anchor_date": "Give one recent payday so the paydays can be worked out."})
        return clean_original(attrs, self.instance)

    def validate_due_day(self, value):
        if value is not None and not 1 <= value <= 31:
            raise serializers.ValidationError("Due day must be between 1 and 31.")
        return value


class MonthlyPlanSerializer(serializers.ModelSerializer):
    checklist_items      = ChecklistItemSerializer(many=True, read_only=True)
    effective_income     = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model  = MonthlyPlan
        fields = [
            'id', 'year', 'month',
            'freelance_income',
            'total_income', 'total_fixed_expenses',
            'total_debt_payments', 'surplus_to_avalanche',
            'emergency_fund_contribution',
            'is_finalized', 'notes',
            'created_at', 'updated_at',
            # computed
            'effective_income',
            # nested
            'checklist_items',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
