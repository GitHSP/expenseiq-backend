from django.contrib import admin
from .models import (
    Debt, EmergencyFund, MonthlyPlan, ChecklistItem,
)

@admin.register(Debt)
class DebtAdmin(admin.ModelAdmin):
    list_display  = ['name', 'user', 'current_balance', 'annual_interest_rate', 'avalanche_order', 'is_active']
    list_filter   = ['debt_type', 'is_active']
    ordering      = ['avalanche_order']

@admin.register(EmergencyFund)
class EmergencyFundAdmin(admin.ModelAdmin):
    list_display = ['user', 'current_balance', 'target_amount', 'is_funded']

@admin.register(MonthlyPlan)
class MonthlyPlanAdmin(admin.ModelAdmin):
    list_display = ['user', 'year', 'month', 'total_income', 'surplus_to_avalanche', 'is_finalized']
    list_filter  = ['year', 'is_finalized']

@admin.register(ChecklistItem)
class ChecklistItemAdmin(admin.ModelAdmin):
    list_display = ['label', 'category', 'amount', 'is_completed', 'is_auto_debit']
    list_filter  = ['category', 'is_completed', 'is_auto_debit']