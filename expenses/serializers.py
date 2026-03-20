# expenses/serializers.py

from rest_framework import serializers
from .models        import Expense, Budget, Income, Debt, PaymentRecord


class ExpenseSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Expense
        fields = ['id', 'title', 'amount', 'category', 'date', 'tags', 'notes', 'created_at']
        read_only_fields = ['id', 'created_at']


class BudgetSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Budget
        fields = ['id', 'category', 'amount']
        read_only_fields = ['id']


class IncomeSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Income
        fields = ['id', 'title', 'amount', 'category', 'date', 'notes', 'created_at']
        read_only_fields = ['id', 'created_at']


class PaymentRecordSerializer(serializers.ModelSerializer):
    debt_name = serializers.CharField(source='debt.name', read_only=True)

    class Meta:
        model  = PaymentRecord
        fields = ['id', 'debt', 'debt_name', 'amount', 'date', 'note', 'created_at']
        read_only_fields = ['id', 'created_at', 'debt_name']


class DebtSerializer(serializers.ModelSerializer):
    payments = PaymentRecordSerializer(many=True, read_only=True)

    class Meta:
        model  = Debt
        fields = [
            'id', 'name', 'type', 'balance', 'original_amount',
            'interest_rate', 'monthly_payment', 'limit', 'lender',
            'next_payment_date', 'notes', 'is_paid_off', 'created_at',
            'payments',
        ]
        read_only_fields = ['id', 'created_at', 'payments']