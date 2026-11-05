"""
What needs the user's attention soon: payments due in the next few days (from
Recurring and debts, skipping what's ticked off on this month's checklist),
cards close to their limit, spending limits nearly used up, and accounts
whose statements haven't been uploaded for a while. Shown on the Dashboard
and, with send_reminders, emailed as a daily digest.
"""
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from .models import ChecklistItem, Debt
from .services import _desired_items, _generated_key

LIMIT_NEAR = Decimal("0.95")  # a card at 95% of its limit or more
BUDGET_NEAR = 80             # a spending limit 80% used or more
UPLOAD_AFTER_DAYS = 35       # an account's newest statement is older than this


def _due_soon(user, today, days):
    end = today + timedelta(days=days)
    done = {_generated_key(i) for i in ChecklistItem.objects.filter(
        monthly_plan__user=user, monthly_plan__year=today.year, monthly_plan__month=today.month,
        is_completed=True).select_related("recurring_item")}
    out, y, m = [], today.year, today.month
    while date(y, m, 1) <= end:
        desired, _ = _desired_items(user, y, m)
        last = monthrange(y, m)[1]
        for key, f in desired:
            if key[0] == "debt_extra" or f["category"] == "income" or not f["due_day"]:
                continue
            if (y, m) == (today.year, today.month) and key in done:
                continue
            when = date(y, m, min(f["due_day"], last))
            if today <= when <= end:
                out.append({"type": "due", "label": f["label"], "amount": f["amount"], "date": when.isoformat(),
                            "days": (when - today).days, "auto": bool(f.get("is_auto_debit"))})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return sorted(out, key=lambda r: (r["date"], r["label"]))


def _near_limit(user):
    out = []
    for d in Debt.objects.filter(user=user, is_active=True, credit_limit__gt=0):
        if d.current_balance >= d.credit_limit * LIMIT_NEAR:
            out.append({"type": "limit", "label": d.name, "room": max(d.credit_limit - d.current_balance, 0),
                        "limit": d.credit_limit})
    return out


def _budgets(user, today):
    from assistant.advisor import budget_status
    out = []
    for b in budget_status(user, month=f"{today:%Y-%m}")["budgets"]:
        if b["percent_used"] >= BUDGET_NEAR:
            out.append({"type": "budget", "label": b["label"], "spent": b["spent"], "limit": b["limit"],
                        "percent": b["percent_used"]})
    return out


def _uploads(user, today):
    from expenses.models import Account, Statement
    out = []
    for acc in Account.objects.filter(user=user):
        st = Statement.objects.filter(account=acc).order_by("-period_end").first()
        if st and (today - st.period_end).days > UPLOAD_AFTER_DAYS:
            out.append({"type": "upload", "label": acc.name, "through": st.period_end.isoformat()})
    return out


def build_reminders(user, today=None, days=7):
    today = today or timezone.localdate()
    return {
        "today": today.isoformat(), "days": days,
        "due": _due_soon(user, today, days),
        "limits": _near_limit(user),
        "budgets": _budgets(user, today),
        "uploads": _uploads(user, today),
    }
