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
REPORTED_TARGET = Decimal("0.30")   # card usage the bureaus see on the statement date: under 30% is the usual aim
CLOSE_SOON_DAYS = 7          # remind this many days before a card's statement closes
PRICE_UP = Decimal("0.15")   # a regular charge 15% (and $3) above its usual amount
NEW_CHARGE_DAYS = 60         # a regular charge first seen this recently counts as new
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
    for acc in Account.objects.filter(user=user, closed=False):
        st = Statement.objects.filter(account=acc).order_by("-period_end").first()
        if st and (today - st.period_end).days > UPLOAD_AFTER_DAYS:
            out.append({"type": "upload", "label": acc.name, "through": st.period_end.isoformat()})
    return out


def _next_on_day(day, today):
    """The next date (today or later) falling on this day of the month."""
    y, m = today.year, today.month
    for _ in range(2):
        d = date(y, m, min(day, monthrange(y, m)[1]))
        if d >= today:
            return d
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return d


def _closing_soon(user, today, days=CLOSE_SOON_DAYS):
    """Cards whose statement closes soon while more than 30% of the limit is used:
    the balance on that day is what the credit bureaus see."""
    from expenses.models import Statement
    out = []
    for d in Debt.objects.filter(user=user, is_active=True, credit_limit__gt=0, current_balance__gt=0):
        last = Statement.objects.filter(account__debt=d).order_by("-period_end").first()
        if not last or d.current_balance <= d.credit_limit * REPORTED_TARGET:
            continue
        closes = _next_on_day(last.period_end.day, today)
        if (closes - today).days <= days:
            to_target = (d.current_balance - d.credit_limit * REPORTED_TARGET).quantize(Decimal("0.01"))
            out.append({"type": "close", "label": d.name, "date": closes.isoformat(), "days": (closes - today).days,
                        "percent": round(float(d.current_balance / d.credit_limit * 100)), "to_target": to_target})
    return sorted(out, key=lambda c: c["date"])


def _changes(user, today):
    """Regular charges that went up, and ones that are new."""
    from django.db.models import Max
    from expenses.accounts import find_recurring
    from expenses.models import Statement, StatementTransaction
    from expenses.statement_import import EXPENSE_RULES, _match
    txs = list(StatementTransaction.objects.filter(user=user, amount__lt=0))
    ends = dict(Statement.objects.filter(user=user).values("account").annotate(end=Max("period_end"))
                .values_list("account", "end"))
    out = []
    for item in find_recurring(txs, ends, lambda tx: tx.category or _match(EXPENSE_RULES, tx.description, "Other")):
        if item["status"] != "active" or item["own_transfer"] or item["frequency"] == "once":
            continue
        occ = item["occurrences"]
        last = Decimal(occ[-1]["amount"])
        recent = (today - date.fromisoformat(occ[-1]["date"])).days <= 45
        before = sorted(Decimal(o["amount"]) for o in occ[:-1])
        usual = before[len(before) // 2] if before else None
        if recent and usual and last - usual >= max(usual * PRICE_UP, Decimal("3")):
            out.append({"type": "change", "kind": "up", "label": item["name"], "amount": last, "usual": usual,
                        "date": occ[-1]["date"]})
        elif (today - date.fromisoformat(item["first_date"])).days <= NEW_CHARGE_DAYS:
            out.append({"type": "change", "kind": "new", "label": item["name"], "amount": last,
                        "frequency": item["frequency"], "date": item["first_date"]})
    return out


def build_reminders(user, today=None, days=7):
    today = today or timezone.localdate()
    return {
        "today": today.isoformat(), "days": days,
        "due": _due_soon(user, today, days),
        "closing": _closing_soon(user, today),
        "limits": _near_limit(user),
        "budgets": _budgets(user, today),
        "changes": _changes(user, today),
        "uploads": _uploads(user, today),
    }
