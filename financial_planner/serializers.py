from rest_framework import serializers
from django.utils   import timezone
from .models import (
    Debt, EmergencyFund, MonthlyPlan,
    PaycheckAllocation, ChecklistItem, MonthlyDebtSnapshot,
    PaycheckConfig,
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


class PaycheckAllocationSerializer(serializers.ModelSerializer):
    class Meta:
        model  = PaycheckAllocation
        fields = [
            'id', 'paycheck_number', 'paycheck_date',
            'source', 'gross_amount',
            'allocated_to_savings', 'allocated_to_debts',
            'allocated_to_expenses', 'allocated_to_avalanche',
            'remaining', 'notes',
        ]
        read_only_fields = ['id']


class ChecklistItemSerializer(serializers.ModelSerializer):
    class Meta:
        model  = ChecklistItem
        fields = [
            'id', 'paycheck_allocation', 'label',
            'amount', 'due_day', 'category',
            'is_auto_debit', 'is_completed',
            'completed_at', 'sort_order', 'linked_debt',
        ]
        read_only_fields = ['id', 'completed_at']


class MonthlyDebtSnapshotSerializer(serializers.ModelSerializer):
    debt_name = serializers.CharField(source='debt.name', read_only=True)
    debt_type = serializers.CharField(source='debt.debt_type', read_only=True)

    class Meta:
        model  = MonthlyDebtSnapshot
        fields = [
            'id', 'debt', 'debt_name', 'debt_type',
            'opening_balance', 'interest_charged',
            'balance_after_interest', 'payment_made',
            'extra_payment', 'closing_balance',
            'planned_payment', 'actual_payment',
            'is_paid_off_this_month',
        ]
        read_only_fields = [
            'id', 'balance_after_interest',
            'closing_balance', 'is_paid_off_this_month',
        ]


class MonthlyPlanSerializer(serializers.ModelSerializer):
    paycheck_allocations = PaycheckAllocationSerializer(many=True, read_only=True)
    checklist_items      = ChecklistItemSerializer(many=True, read_only=True)
    debt_snapshots       = MonthlyDebtSnapshotSerializer(many=True, read_only=True)
    effective_income     = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model  = MonthlyPlan
        fields = [
            'id', 'year', 'month',
            'biweekly_income', 'second_job_income',
            'freelance_income', 'num_paychecks',
            'is_three_paycheck_month',
            'total_income', 'total_fixed_expenses',
            'total_debt_payments', 'surplus_to_avalanche',
            'emergency_fund_contribution',
            'is_finalized', 'notes',
            'created_at', 'updated_at',
            # computed
            'effective_income',
            # nested
            'paycheck_allocations',
            'checklist_items',
            'debt_snapshots',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class PaycheckConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model  = PaycheckConfig
        fields = [
            'id', 'first_pay_date', 'biweekly_amount',
            'second_job_day', 'second_job_amount',
            'second_job_tips', 'extra_payment', 'updated_at',
        ]
        read_only_fields = ['id', 'updated_at']