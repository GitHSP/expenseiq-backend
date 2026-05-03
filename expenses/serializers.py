from rest_framework import serializers
from .models        import Expense, Budget, Income


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