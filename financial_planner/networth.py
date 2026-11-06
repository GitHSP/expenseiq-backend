"""
Net worth over time. Each month keeps one snapshot: bank balances and debts
(CAD) and investments by currency (the app converts those at today's rates).
The current month's snapshot is refreshed whenever net worth is looked at.
Months before the first snapshot are estimated from the statements: bank and
card balances as printed at each month end, with loans and investments at
today's values (they're marked as estimated).
"""
from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.utils import timezone

from .models import Debt, Investment, NetWorthSnapshot

D0 = Decimal("0.00")
MONTHS_BACK = 12


def _investments_now(user):
    from .investments import summarize
    out = {}
    for inv in Investment.objects.filter(user=user, is_active=True).prefetch_related("payments"):
        out[inv.currency] = str(Decimal(out.get(inv.currency, "0")) + Decimal(summarize(inv)["worth"] or 0))
    return out


def _balance_at(account, when):
    """The account's balance on its last statement ending on or before `when`."""
    from expenses.models import Statement
    st = (Statement.objects.filter(account=account, new_balance__isnull=False, period_end__lte=when)
          .order_by("-period_end").first())
    return st.new_balance if st else None


def record(user, today=None):
    """Save (or refresh) this month's snapshot from today's figures."""
    from expenses.models import Account, Statement
    today = today or timezone.localdate()
    banks = D0
    for acc in Account.objects.filter(user=user, kind__in=("chequing", "savings"), closed=False):
        st = Statement.objects.filter(account=acc, new_balance__isnull=False).order_by("-period_end").first()
        banks += st.new_balance if st else D0
    debts = sum((d.current_balance for d in Debt.objects.filter(user=user, is_active=True, current_balance__gt=0)), D0)
    NetWorthSnapshot.objects.update_or_create(
        user=user, month=today.replace(day=1),
        defaults={"banks": banks, "debts": debts, "investments": _investments_now(user), "estimated": False})


def _estimate(user, month_end, loans, investments):
    from expenses.models import Account
    banks = cards = D0
    seen = False
    for acc in Account.objects.filter(user=user):
        bal = _balance_at(acc, month_end)
        if bal is None:
            continue
        seen = True
        if acc.kind in ("chequing", "savings"):
            banks += bal
        elif acc.kind == "credit_card":
            cards += max(bal, D0)
    return (banks, cards + loans, investments) if seen else None


def history(user, today=None):
    today = today or timezone.localdate()
    record(user, today)
    saved = {s.month: s for s in NetWorthSnapshot.objects.filter(user=user)}
    loans = sum((d.current_balance for d in Debt.objects.filter(user=user, is_active=True, current_balance__gt=0)
                 .exclude(debt_type="credit_card")), D0)
    investments = _investments_now(user)
    out = []
    y, m = today.year, today.month
    for _ in range(MONTHS_BACK):
        first = date(y, m, 1)
        snap = saved.get(first)
        if snap:
            out.append({"month": f"{first:%Y-%m}", "banks": snap.banks, "debts": snap.debts,
                        "investments": snap.investments, "estimated": snap.estimated})
        else:
            est = _estimate(user, date(y, m, monthrange(y, m)[1]), loans, investments)
            if est is None:
                break                                  # nothing on file this far back
            banks, debts, inv = est
            out.append({"month": f"{first:%Y-%m}", "banks": banks, "debts": debts, "investments": inv, "estimated": True})
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return list(reversed(out))
