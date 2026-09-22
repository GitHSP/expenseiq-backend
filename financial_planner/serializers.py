from rest_framework import serializers
from django.utils   import timezone
from .models import (
    Debt, EmergencyFund, MonthlyPlan, ChecklistItem,
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
            'created_at', 'updated_at',
            # computed
            'monthly_interest_rate',
            'utilization_percent',
            'monthly_interest_amount',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


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
            'amount', 'due_day', 'category',
            'is_auto_debit', 'is_completed',
            'completed_at', 'sort_order', 'linked_debt',
        ]
        read_only_fields = ['id', 'completed_at']


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
