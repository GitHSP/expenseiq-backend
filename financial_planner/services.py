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
    """
    Re-rank a user's active debts by APR descending (1 = highest priority).
    Ties go to the smaller balance, which gets paid off — and freed up — sooner.
    """
    from .models import Debt
    debts = (
        Debt.objects
        .filter(user=user, is_active=True)
        .order_by('-annual_interest_rate', 'current_balance')
    )
    for i, debt in enumerate(debts, start=1):
        debt.avalanche_order = i
        debt.save(update_fields=['avalanche_order'])


# ─────────────────────────────────────────────
# Monthly checklist
#
# A month's checklist is built from the user's sources of truth:
#   - an emergency-fund transfer (until the fund is full)
#   - every active RecurringItem (rent, bills, income...)
#   - a payment for every active debt, plus an EXTRA avalanche payment
#     on the top-priority debt
# Items the user adds by hand are one-offs for that month and are never
# touched by a sync.
# ─────────────────────────────────────────────

GENERATED_DEBT_CATEGORIES = ("debt_min", "debt_extra")


def _generated_key(item):
    """Identify which source a checklist item was generated from (None = manual)."""
    if item.recurring_item_id:
        return ("recurring", item.recurring_item_id)
    if item.category == "savings":
        return ("savings",)
    if item.category in GENERATED_DEBT_CATEGORIES and item.linked_debt_id:
        return (item.category, item.linked_debt_id)
    return None


def _desired_items(user):
    """Return [(key, fields)] for everything this month's checklist should contain."""
    from .models import Debt, RecurringItem

    desired = []

    try:
        fund = EmergencyFund.objects.get(user=user)
    except EmergencyFund.DoesNotExist:
        fund = None
    if fund and not fund.is_funded and fund.monthly_contribution > 0:
        desired.append((("savings",), dict(
            label="Emergency Savings — transfer FIRST on pay day",
            amount=fund.monthly_contribution, due_day=1,
            category="savings", is_auto_debit=False,
        )))

    for rec in RecurringItem.objects.filter(user=user, is_active=True):
        desired.append((("recurring", rec.id), dict(
            label=rec.label, amount=rec.amount, due_day=rec.due_day,
            category=rec.category, is_auto_debit=rec.is_auto_debit,
            recurring_item=rec,
        )))

    debts = list(
        Debt.objects
        .filter(user=user, is_active=True, current_balance__gt=0)
        .order_by("avalanche_order")
    )
    for debt in debts:
        suffix = "minimum payment" if debt.debt_type == "credit_card" else "payment"
        desired.append((("debt_min", debt.id), dict(
            label=f"{debt.name} {suffix}",
            amount=min(debt.minimum_payment, debt.current_balance),
            due_day=debt.due_day, category="debt_min",
            is_auto_debit=False, linked_debt=debt,
        )))

    # Whatever is left after every planned payment goes to the avalanche target.
    income = sum((f["amount"] for _, f in desired if f["category"] == "income"), Decimal("0.00"))
    outgoing = sum((f["amount"] for _, f in desired if f["category"] != "income"), Decimal("0.00"))
    surplus = max(income - outgoing, Decimal("0.00"))

    top_debt = debts[0] if debts else None
    if top_debt and top_debt.annual_interest_rate > 0:
        desired.append((("debt_extra", top_debt.id), dict(
            label=f"{top_debt.name} — EXTRA avalanche payment (all surplus)",
            amount=min(surplus, top_debt.current_balance),
            due_day=top_debt.due_day, category="debt_extra",
            is_auto_debit=False, linked_debt=top_debt,
        )))

    return desired, top_debt


def _update_plan_totals(plan, desired):
    totals = {"income": Decimal("0.00"), "fixed": Decimal("0.00"),
              "debt": Decimal("0.00"), "savings": Decimal("0.00")}
    extra = Decimal("0.00")
    for (kind, *_), f in desired:
        if f["category"] == "income":
            totals["income"] += f["amount"]
        elif kind == "debt_min":
            totals["debt"] += f["amount"]
        elif kind == "debt_extra":
            extra = f["amount"]
        elif kind == "savings":
            totals["savings"] += f["amount"]
        else:
            totals["fixed"] += f["amount"]

    plan.total_income = totals["income"]
    plan.total_fixed_expenses = totals["fixed"]
    plan.total_debt_payments = totals["debt"]
    plan.emergency_fund_contribution = totals["savings"]
    plan.surplus_to_avalanche = extra
    plan.save(update_fields=[
        "total_income", "total_fixed_expenses", "total_debt_payments",
        "emergency_fund_contribution", "surplus_to_avalanche", "updated_at",
    ])


def sync_month_checklist(plan):
    """
    Bring a month's generated checklist items in line with the user's
    recurring items, debts and emergency fund:
      - missing items are created
      - unticked items are updated to match their source
      - unticked items whose source was removed/paid off are deleted
    Ticked items are left alone (they're history), as are manual items.
    Returns (items_in_sync_order, top_debt_or_None).
    """
    from .models import ChecklistItem

    desired, top_debt = _desired_items(plan.user)

    existing = {}
    for item in plan.checklist_items.all():
        key = _generated_key(item)
        if key is None:
            continue
        if key in existing and not item.is_completed:
            item.delete()  # duplicate from an older generator
            continue
        existing.setdefault(key, item)

    result = []
    wanted_keys = set()
    for sort_order, (key, fields) in enumerate(desired, start=1):
        wanted_keys.add(key)
        item = existing.get(key)
        if item is None:
            item = ChecklistItem.objects.create(monthly_plan=plan, sort_order=sort_order, **fields)
        elif not item.is_completed:
            for name, value in fields.items():
                setattr(item, name, value)
            item.sort_order = sort_order
            item.save()
        result.append(item)

    for key, item in existing.items():
        if key not in wanted_keys and not item.is_completed:
            item.delete()

    _update_plan_totals(plan, desired)
    return result, top_debt


def _apply_monthly_interest(user, months):
    """Add `months` months of interest to every active debt (compounded monthly)."""
    from .models import Debt
    debts = list(Debt.objects.filter(user=user, is_active=True))
    for debt in debts:
        if months <= 0 or debt.annual_interest_rate <= 0:
            continue
        for _ in range(months):
            debt.current_balance += (debt.current_balance * debt.monthly_interest_rate).quantize(Decimal("0.01"))
        debt.save(update_fields=["current_balance", "updated_at"])
    return debts


def get_or_create_month_plan(user, year, month):
    """
    Return (plan, rolled_over). Creating a month's plan for a user who
    already has an earlier plan rolls them over: one month of interest is
    applied per month since that plan, and the new checklist is built from
    their recurring items and debts. Each month is created exactly once,
    so interest is never applied twice.
    """
    from django.db import IntegrityError, transaction
    from django.db.models import Q
    from .models import MonthlyPlan

    plan = MonthlyPlan.objects.filter(user=user, year=year, month=month).first()
    if plan:
        return plan, False

    with transaction.atomic():
        previous = (
            MonthlyPlan.objects
            .filter(user=user)
            .filter(Q(year__lt=year) | Q(year=year, month__lt=month))
            .order_by("-year", "-month")
            .first()
        )
        try:
            with transaction.atomic():
                plan = MonthlyPlan.objects.create(user=user, year=year, month=month)
        except IntegrityError:
            # Another request created it first.
            return MonthlyPlan.objects.get(user=user, year=year, month=month), False

        if previous:
            gap = (year * 12 + month) - (previous.year * 12 + previous.month)
            _apply_monthly_interest(user, gap)
        sync_month_checklist(plan)

    return plan, previous is not None


def get_or_create_current_plan(user):
    now = timezone.now()
    return get_or_create_month_plan(user, now.year, now.month)


def sync_current_month(user):
    """Re-sync this month's checklist, if this month's plan exists yet."""
    from .models import MonthlyPlan
    now = timezone.now()
    plan = MonthlyPlan.objects.filter(user=user, year=now.year, month=now.month).first()
    if plan:
        sync_month_checklist(plan)


def delete_recurring_item(item):
    """
    Delete a recurring item along with its unticked checklist items (ticked
    ones are kept as history). Done before the delete because the FK is
    SET_NULL, which would otherwise turn them into "manual" items.
    """
    user = item.user
    item.checklist_items.filter(is_completed=False).delete()
    item.delete()
    sync_current_month(user)


def delete_debt(debt):
    """Delete a debt and its unticked checklist payments (same reason as above)."""
    user = debt.user
    debt.checklist_items.filter(is_completed=False).delete()
    debt.delete()
    recalculate_avalanche_order(user)
    sync_current_month(user)


def generate_checklist(user):
    """
    (Re)build this month's checklist from the user's recurring items and
    debts. Returns (plan, items, top_debt_or_None) or raises ValueError if
    there is nothing to build from.
    """
    from .models import Debt, RecurringItem

    if not (
        Debt.objects.filter(user=user, is_active=True).exists()
        or RecurringItem.objects.filter(user=user, is_active=True).exists()
    ):
        raise ValueError("Nothing to build a checklist from. Add debts or recurring payments first.")

    plan, _ = get_or_create_current_plan(user)
    items, top_debt = sync_month_checklist(plan)
    return plan, items, top_debt


def rollover_month(user):
    """
    Create next month's plan early (applying its interest now). Returns
    (new_plan, debts) or raises ValueError if it already exists.
    """
    from .models import Debt, MonthlyPlan

    now = timezone.now()
    next_month = now.month + 1 if now.month < 12 else 1
    next_year = now.year if now.month < 12 else now.year + 1

    if MonthlyPlan.objects.filter(user=user, year=next_year, month=next_month).exists():
        raise ValueError(f"Plan for {next_year}-{next_month:02d} already exists")

    new_plan, _ = get_or_create_month_plan(user, next_year, next_month)
    return new_plan, Debt.objects.filter(user=user, is_active=True)
