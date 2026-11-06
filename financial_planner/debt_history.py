"""
Card balances over time, from the closing balance on each uploaded statement.
Per card: one point per statement. All cards: the balance each card had at
every month end (from its latest statement by then), added up, back to the
first month every card has a statement for.
"""
from calendar import monthrange
from datetime import date
from decimal import Decimal

D0 = Decimal("0.00")
POINTS = 12


def card_history(user, today):
    from expenses.models import Account, Statement
    cards, by_account = {}, {}
    for acc in Account.objects.filter(user=user, debt__isnull=False).select_related("debt"):
        rows = list(Statement.objects.filter(account=acc, new_balance__isnull=False)
                    .order_by("period_end").values_list("period_end", "new_balance"))
        if not rows:
            continue
        by_account[acc.id] = rows
        points = cards.setdefault(acc.debt_id, {})
        for end, bal in rows:                               # two accounts for one debt: add them up
            points[end] = points.get(end, D0) + max(bal, D0)
    cards = {debt_id: [{"date": d.isoformat(), "balance": str(b)} for d, b in sorted(p.items())][-POINTS:]
             for debt_id, p in cards.items()}

    total = []
    newest = max((rows[-1][0] for rows in by_account.values()), default=today)
    y, m = min(newest, today).year, min(newest, today).month      # end at the last month a statement came in
    for _ in range(POINTS):
        end = date(y, m, monthrange(y, m)[1])
        known = [next((b for d, b in reversed(rows) if d <= end), None) for rows in by_account.values()]
        if not known or None in known:
            break                     # a card with no statement yet: the total would look too low
        known = [max(b, D0) for b in known]
        total.append({"month": f"{y}-{m:02d}", "balance": str(sum(known, D0))})
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return {"cards": cards, "total": list(reversed(total))}


def month_change(user, ym):
    """How much card balances moved over a month (end of that month vs the month before)."""
    y, m = (int(x) for x in ym.split("-"))
    rows = card_history(user, date(y, m, monthrange(y, m)[1]))["total"]
    if len(rows) < 2 or rows[-1]["month"] != ym:
        return None
    return {"end": rows[-1]["balance"], "change": str(Decimal(rows[-1]["balance"]) - Decimal(rows[-2]["balance"]))}
