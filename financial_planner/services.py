# financial_planner/services.py
#
# Shared business logic used by both the DRF views (ChecklistItemToggleView)
# and the AI assistant's tools, so the two never drift out of sync.

from decimal import Decimal
from datetime import date

from django.utils import timezone

from .models import EmergencyFund


def toggle_checklist_item(item):
    """
    Toggle a ChecklistItem's is_completed flag and apply the same side
    effects the app has always had:
      - ticking ON a debt_min/debt_extra item with a linked debt reduces
        that debt's current_balance (and marks it paid off at zero)
      - ticking ON a savings item adds its amount to the user's emergency
        fund (capped at the target)
    Unticking never triggers these side effects.

    Returns (item, debt_updated_or_None, fund_updated_or_None) — the debt
    and fund are the actual model instances (callers serialize as needed).
    """
    user = item.monthly_plan.user

    was_completed = item.is_completed
    item.is_completed = not item.is_completed
    item.completed_at = timezone.now() if item.is_completed else None
    item.save()

    debt_updated = None
    fund_updated = None

    if item.is_completed and not was_completed:

        if (
            item.linked_debt and
            item.amount and
            item.category in ["debt_min", "debt_extra"]
        ):
            debt = item.linked_debt
            reduction = Decimal(str(item.amount))
            debt.current_balance = max(
                Decimal("0.00"),
                debt.current_balance - reduction
            )
            if debt.current_balance == Decimal("0.00"):
                debt.is_active = False
                debt.paid_off_date = date.today()
            debt.save()
            debt_updated = debt

        if item.category == "savings" and item.amount:
            try:
                fund = EmergencyFund.objects.get(user=user)
                fund.current_balance = min(
                    fund.current_balance + Decimal(str(item.amount)),
                    fund.target_amount
                )
                fund.save()
                fund_updated = fund
            except EmergencyFund.DoesNotExist:
                pass

    return item, debt_updated, fund_updated


def recalculate_avalanche_order(user):
    """Re-rank a user's active debts by APR descending (1 = highest priority)."""
    from .models import Debt
    debts = Debt.objects.filter(user=user, is_active=True).order_by('-annual_interest_rate')
    for i, debt in enumerate(debts, start=1):
        debt.avalanche_order = i
        debt.save(update_fields=['avalanche_order'])


def generate_checklist(user):
    """
    Auto-generate this month's checklist from the user's active debts
    (avalanche order) plus an emergency-fund savings item if it isn't
    funded yet. Replaces any existing debt_min/debt_extra/savings items
    for the current month. Returns (plan, created_items, top_debt_or_None)
    or raises ValueError if there are no active debts.
    """
    from .models import Debt, MonthlyPlan, ChecklistItem

    now = timezone.now()
    plan, _ = MonthlyPlan.objects.get_or_create(user=user, year=now.year, month=now.month)

    debts = Debt.objects.filter(user=user, is_active=True).order_by('avalanche_order')
    if not debts.exists():
        raise ValueError("No active debts found. Add debts first.")

    ChecklistItem.objects.filter(
        monthly_plan=plan,
        category__in=["debt_min", "debt_extra", "savings"]
    ).delete()

    created_items = []
    sort_order = 1

    try:
        fund = EmergencyFund.objects.get(user=user)
        if not fund.is_funded:
            item = ChecklistItem.objects.create(
                monthly_plan=plan,
                label="Emergency Savings 🏦 — transfer FIRST on pay day",
                amount=fund.monthly_contribution,
                due_day=1,
                category="savings",
                is_auto_debit=False,
                sort_order=sort_order,
            )
            created_items.append(item)
            sort_order += 1
    except EmergencyFund.DoesNotExist:
        pass

    for debt in debts:
        item = ChecklistItem.objects.create(
            monthly_plan=plan,
            label=f"{debt.name} minimum payment",
            amount=debt.minimum_payment,
            due_day=debt.due_day,
            category="debt_min",
            is_auto_debit=False,
            sort_order=sort_order,
            linked_debt=debt,
        )
        created_items.append(item)
        sort_order += 1

    top_debt = debts.first()
    if top_debt:
        item = ChecklistItem.objects.create(
            monthly_plan=plan,
            label=f"🎯 {top_debt.name} — EXTRA avalanche payment (all surplus)",
            amount=Decimal("0.00"),
            due_day=top_debt.due_day,
            category="debt_extra",
            is_auto_debit=False,
            sort_order=sort_order,
            linked_debt=top_debt,
        )
        created_items.append(item)

    return plan, created_items, top_debt


def rollover_month(user):
    """
    Apply this month's interest to all active debts and create next
    month's plan (copying checklist items, reset to incomplete).
    Returns (new_plan, debts) or raises ValueError if next month's plan
    already exists.
    """
    from .models import Debt, MonthlyPlan, ChecklistItem

    now = timezone.now()
    next_month = now.month + 1 if now.month < 12 else 1
    next_year = now.year if now.month < 12 else now.year + 1

    if MonthlyPlan.objects.filter(user=user, year=next_year, month=next_month).exists():
        raise ValueError(f"Plan for {next_year}-{next_month:02d} already exists")

    debts = Debt.objects.filter(user=user, is_active=True)
    for debt in debts:
        monthly_interest = debt.current_balance * debt.monthly_interest_rate
        debt.current_balance += monthly_interest
        debt.save()

    current_plan = MonthlyPlan.objects.filter(user=user).order_by("-year", "-month").first()

    new_plan = MonthlyPlan.objects.create(
        user=user,
        year=next_year,
        month=next_month,
        freelance_income=Decimal("0.00"),
        total_income=current_plan.total_income if current_plan else Decimal("0.00"),
        total_fixed_expenses=current_plan.total_fixed_expenses if current_plan else Decimal("0.00"),
        total_debt_payments=current_plan.total_debt_payments if current_plan else Decimal("0.00"),
        emergency_fund_contribution=current_plan.emergency_fund_contribution if current_plan else Decimal("100.00"),
    )

    if current_plan:
        for item in ChecklistItem.objects.filter(monthly_plan=current_plan):
            new_amount = item.amount
            if item.linked_debt and item.category == "debt_min":
                new_amount = item.linked_debt.minimum_payment
            ChecklistItem.objects.create(
                monthly_plan=new_plan,
                label=item.label,
                amount=new_amount,
                due_day=item.due_day,
                category=item.category,
                is_auto_debit=item.is_auto_debit,
                is_completed=False,
                sort_order=item.sort_order,
                linked_debt=item.linked_debt,
            )

    return new_plan, debts
