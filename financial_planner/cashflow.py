"""
The bank balance day by day: start from today's balance, add expected pay, take
off bills and debt payments on their dates. It shows where money gets tight.
Bills on a card don't leave the bank, so they're left out; a shared bill counts
at the amount in Recurring (the user's share), since roommates pay back the rest.
"""
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from .models import ChecklistItem
from .services import _desired_items, _generated_key

BUFFER_DAYS = 45          # statements older than this make the starting balance a guess


def _months(start, end):
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _start_balance(user):
    from expenses.models import Account, Statement
    total, as_of, found = Decimal("0.00"), None, False
    for acc in Account.objects.filter(user=user, kind__in=("chequing", "savings")):
        st = Statement.objects.filter(account=acc, new_balance__isnull=False).order_by("-period_end").first()
        if st:
            found = True
            total += st.new_balance
            as_of = max(as_of, st.period_end) if as_of else st.period_end
    return (total, as_of) if found else (None, None)


def build_cashflow(user, days=60, balance=None, today=None):
    today = today or timezone.localdate()
    end = today + timedelta(days=days)
    statement_balance, as_of = _start_balance(user)
    start = balance if balance is not None else (statement_balance or Decimal("0.00"))

    done = set()
    for item in ChecklistItem.objects.filter(monthly_plan__user=user, monthly_plan__year=today.year, monthly_plan__month=today.month,
                                             is_completed=True).select_related("recurring_item"):
        done.add(_generated_key(item))

    events = []
    for y, m in _months(today, end):
        desired, _ = _desired_items(user, y, m)
        last = monthrange(y, m)[1]
        for key, f in desired:
            if key[0] == "debt_extra":
                continue                                 # what's left over, not a set bill
            rec = f.get("recurring_item")
            if rec is not None and rec.paid_by_card:
                continue
            if (y, m) == (today.year, today.month) and key in done:
                continue
            day = min(f["due_day"] or 1, last)
            when = date(y, m, day)
            if when < today or when > end:
                continue
            income = f["category"] == "income"
            events.append({
                "date": when, "label": f["label"], "category": f["category"],
                "amount": f["amount"] if income else -f["amount"],
                "estimate": bool(rec is not None and (rec.average_from or rec.frequency == "varies")),
                "no_due_day": not f["due_day"],
            })
    # On the same day, pay arrives before bills go out.
    events.sort(key=lambda e: (e["date"], e["amount"] < 0, e["label"]))

    running = start
    lowest, lowest_date = start, today
    first_short = None
    money_in = money_out = Decimal("0.00")
    for e in events:
        running += e["amount"]
        e["balance"] = running
        if e["amount"] > 0:
            money_in += e["amount"]
        else:
            money_out -= e["amount"]
        if running < lowest:
            lowest, lowest_date = running, e["date"]
        if running < 0 and first_short is None:
            first_short = {"date": e["date"].isoformat(), "label": e["label"], "balance": running}

    return {
        "today": today.isoformat(), "end": end.isoformat(), "days": days,
        "start_balance": start,
        "entered": balance is not None,
        "statement_balance": statement_balance,
        "statement_date": as_of.isoformat() if as_of else None,
        "stale": as_of is None or (today - as_of).days > BUFFER_DAYS,
        "end_balance": running,
        "money_in": money_in, "money_out": money_out,
        "lowest": lowest, "lowest_date": lowest_date.isoformat(),
        "first_short": first_short,
        "events": [{**e, "date": e["date"].isoformat()} for e in events],
    }
