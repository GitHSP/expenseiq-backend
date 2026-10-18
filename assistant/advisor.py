# assistant/advisor.py
#
# What Aurora works out for the user (all read-only; she writes the words):
#   - monthly_review: a month next to the one before
#   - get_alerts: things worth knowing (price rises, new or duplicate
#     charges, fees, cards near their limit, overdraft risk, budgets)
#   - plan_card_payments: what to pay on each card, when the cards are paid
#     off and the interest saved
#   - budget_status: spending against the user's monthly limits

from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_UP, Decimal

import re

from expenses.models import Account, SpendingBudget, StatementTransaction

from .insights import THEMES, _rows, merchant_group, search_transactions

D0 = Decimal("0")

# Bank and card fees, as whole words ("coffee" isn't a fee).
FEE_RE = re.compile(
    r"\b(nsf|overdraft|service charge|monthly fee|annual fee|late (payment )?fee|over ?limit fee|"
    r"foreign transaction fee|record-?keeping|fee|fees)\b", re.I)


def _money(v):
    return str(Decimal(v).quantize(Decimal("0.01")))


def _month_bounds(ym):
    y, m = (int(x) for x in ym.split("-"))
    return date(y, m, 1), date(y, m, monthrange(y, m)[1])


def _prev_month(ym):
    y, m = (int(x) for x in ym.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def _latest_full_month(user):
    """The latest month with transactions (default for a review)."""
    last = StatementTransaction.objects.filter(user=user).order_by("-date").values_list("date", flat=True).first()
    d = last or date.today()
    return f"{d.year}-{d.month:02d}"


# ─────────────────────────────────────────────
# Monthly review
# ─────────────────────────────────────────────

def _month_figures(user, ym):
    start, end = _month_bounds(ym)
    income = spent = D0
    cats, merchants, names = defaultdict(lambda: D0), defaultdict(lambda: D0), {}
    interest = fees = D0
    for r in _rows(user, start, end):
        if r["transfer"]:
            continue
        if r["amount"] > 0 and r["account_kind"] != "credit_card":
            income += r["amount"]
            continue
        value = -r["amount"]                       # card refunds come in negative
        spent += value
        cats[r["category"] or "Uncategorised"] += value
        key = merchant_group(r["title"])
        merchants[key] += value
        names.setdefault(key, r["title"])
        text = f"{r['title']} {r['description']}".lower()
        if "interest" in text and "reversal" not in text:
            interest += value
        elif FEE_RE.search(text):
            fees += value
    return {"income": income, "spent": spent, "categories": cats, "merchants": merchants, "names": names,
            "interest": interest, "fees": fees}


def monthly_review(user, month=None, **kwargs):
    ym = month[:7] if month else _latest_full_month(user)
    try:
        _month_bounds(ym)
    except ValueError:
        return {"ok": False, "message": "month must look like 2026-09."}
    prev = _prev_month(ym)
    cur, before = _month_figures(user, ym), _month_figures(user, prev)
    if not cur["spent"] and not cur["income"]:
        return {"ok": True, "month": ym, "message": f"No transactions found for {ym}. Upload that month's statements first."}

    def deltas(a, b, names=None, top=8):
        keys = set(a) | set(b)
        rows = [{"name": (names or {}).get(k, k), "this_month": _money(a.get(k, D0)), "last_month": _money(b.get(k, D0)),
                 "change": _money(a.get(k, D0) - b.get(k, D0))} for k in keys]
        return sorted(rows, key=lambda r: -abs(Decimal(r["change"])))[:top]

    names = {**before["names"], **cur["names"]}
    top_merchants = sorted(cur["merchants"].items(), key=lambda kv: -kv[1])[:8]
    saved = cur["income"] - cur["spent"]
    return {
        "ok": True,
        "month": ym, "compared_with": prev,
        "income": _money(cur["income"]), "income_last_month": _money(before["income"]),
        "spent": _money(cur["spent"]), "spent_last_month": _money(before["spent"]),
        "saved": _money(saved),
        "savings_rate_percent": round(float(saved / cur["income"] * 100), 1) if cur["income"] else None,
        "interest_paid": _money(cur["interest"]), "fees_paid": _money(cur["fees"]),
        "biggest_category_changes": deltas(cur["categories"], before["categories"]),
        "top_merchants": [{"name": names[k], "spent": _money(v)} for k, v in top_merchants],
        "biggest_merchant_changes": deltas(cur["merchants"], before["merchants"], names),
        "alerts": get_alerts(user, as_of_month=ym)["alerts"],
        "note": "Transfers between the user's own accounts and card payments are left out. Numbers come from "
                "uploaded statements plus hand-entered entries; a month with a missing statement will look low.",
    }


# ─────────────────────────────────────────────
# Alerts
# ─────────────────────────────────────────────

def get_alerts(user, as_of_month=None, **kwargs):
    from expenses.account_views import _card
    from expenses.accounts import find_recurring
    from expenses.statement_import import EXPENSE_RULES, _match
    from django.db.models import Max

    alerts = []
    txs = list(StatementTransaction.objects.filter(user=user).select_related("account"))
    if not txs and not SpendingBudget.objects.filter(user=user).exists():
        return {"ok": True, "alerts": []}
    latest = max((t.date for t in txs), default=date.today())
    recent_from = latest - timedelta(days=45)

    # Recurring charges: price rises, new ones.
    ends = dict(Account.objects.filter(user=user).annotate(end=Max("statements__period_end")).values_list("id", "end"))
    names = {a.id: a.name for a in Account.objects.filter(user=user)}
    for item in find_recurring([t for t in txs if t.amount < 0], ends,
                               lambda t: t.category or _match(EXPENSE_RULES, t.description, "Other")):
        occ = item["occurrences"]
        if item["status"] != "active" or item["own_transfer"]:
            continue
        if len(occ) >= 2:
            last, before = Decimal(occ[-1]["amount"]), Decimal(occ[-2]["amount"])
            steady = len({o["amount"] for o in occ[:-1]}) == 1
            if steady and last > before and (last - before) >= Decimal("0.50"):
                alerts.append({"type": "price_increase", "severity": "warning",
                               "title": f"{item['name']} went up",
                               "detail": f"${before} → ${last} on {occ[-1]['date']} ({names.get(occ[-1]['account_id'])})"})
        first = date.fromisoformat(item["first_date"])
        if first >= recent_from and item["frequency"] != "once":
            alerts.append({"type": "new_recurring", "severity": "info",
                           "title": f"New recurring charge: {item['name']}",
                           "detail": f"${item['typical_amount']} {item['frequency']}, first seen {item['first_date']}"})

    # Possible double charges: the same description and amount ($20+) twice
    # within a day on one account. (Two coffees on one day are normal.)
    seen, doubles = {}, []
    for t in sorted((t for t in txs if t.amount <= -20 and t.kind != "transfer" and t.date >= recent_from),
                    key=lambda t: (t.date, t.id)):
        key = (t.description.lower().strip(), t.amount, t.account_id)
        prev = seen.get(key)
        if prev and (t.date - prev.date).days <= 1:
            doubles.append(f"{t.title} ${-t.amount} on {prev.date}" + ("" if prev.date == t.date else f" and {t.date}")
                           + f" ({t.account.name})")
        seen[key] = t
    if doubles:
        alerts.append({"type": "possible_duplicate", "severity": "info",
                       "title": f"{len(doubles)} possible double charge{'s' if len(doubles) > 1 else ''} to check",
                       "detail": "; ".join(doubles[:5])})

    # Fees.
    fees = [t for t in txs if t.amount < 0 and t.kind != "transfer" and t.date >= recent_from
            and FEE_RE.search(f"{t.title} {t.description}")]
    if fees:
        total = sum((-t.amount for t in fees), D0)
        alerts.append({"type": "fees", "severity": "warning", "title": f"${_money(total)} in bank/card fees recently",
                       "detail": "; ".join(f"{t.date} {t.title} ${-t.amount} ({t.account.name})" for t in fees[:5])})

    # Cards near or over their limit, payments due.
    for a in Account.objects.filter(user=user, kind="credit_card"):
        card = _card(a)
        if card["statement_balance"] and card["credit_limit"]:
            used = Decimal(card["estimated_balance"]) / Decimal(card["credit_limit"]) * 100
            if used >= 90:
                alerts.append({"type": "card_limit", "severity": "warning" if used < 100 else "danger",
                               "title": f"{a.name} is {'over' if used >= 100 else 'near'} its limit",
                               "detail": f"about ${card['estimated_balance']} of ${card['credit_limit']} ({used:.0f}%)"})
        if card["payment_due"] and card["minimum_payment"]:
            due = date.fromisoformat(card["payment_due"])
            if date.today() <= due <= date.today() + timedelta(days=10) and Decimal(card["paid_since"]) < Decimal(card["minimum_payment"]):
                alerts.append({"type": "payment_due", "severity": "warning",
                               "title": f"{a.name}: ${card['minimum_payment']} due {due:%b} {due.day}",
                               "detail": f"No payment of at least the minimum found since the {card['statement_end']} statement."})

    # Overdraft risk: pre-authorized debits due before the next pay vs the chequing balance.
    alerts += _overdraft_risk(user, txs)

    # Budgets.
    for b in budget_status(user, month=as_of_month)["budgets"]:
        if b["percent_used"] >= 100:
            alerts.append({"type": "budget", "severity": "danger", "title": f"Over budget: {b['label']}",
                           "detail": f"${b['spent']} of ${b['limit']} in {b['month']}"})
        elif b["percent_used"] >= 80:
            alerts.append({"type": "budget", "severity": "warning", "title": f"Close to budget: {b['label']}",
                           "detail": f"${b['spent']} of ${b['limit']} ({b['percent_used']}%) in {b['month']}"})

    order = {"danger": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda a: order.get(a["severity"], 3))
    return {"ok": True, "alerts": alerts[:20],
            "note": "Based on the statements uploaded so far (latest transaction " + str(latest) + ")."}


def _overdraft_risk(user, txs):
    """Chequing accounts whose next pre-authorized debits (before the next
    pay) add up to more than the last statement's closing balance."""
    from expenses.accounts import find_recurring
    out = []
    for a in Account.objects.filter(user=user, kind__in=("chequing", "savings")):
        st = a.statements.exclude(new_balance=None).order_by("-period_end").first()
        if not st:
            continue
        mine = [t for t in txs if t.account_id == a.id]
        items = find_recurring([t for t in mine if t.amount < 0], {a.id: st.period_end}, lambda t: t.category or "Other")
        # Pay days: deposits of the same size on a schedule.
        pay = find_recurring([_negated(t) for t in mine if t.amount > 0 and t.kind != "transfer"],
                             {a.id: st.period_end}, lambda t: "Income")
        next_pay = min((date.fromisoformat(p["next_date"]) for p in pay if p["next_date"]), default=None)
        horizon = next_pay or st.period_end + timedelta(days=14)
        due = [i for i in items if i["pre_authorized"] and i["next_date"]
               and st.period_end < date.fromisoformat(i["next_date"]) <= horizon]
        total = sum((Decimal(i["typical_amount"]) for i in due), D0)
        if due and total > st.new_balance:
            out.append({"type": "overdraft_risk", "severity": "danger",
                        "title": f"{a.name} may not cover upcoming debits",
                        "detail": f"${_money(total)} of pre-authorized payments due by {horizon} "
                                  f"({', '.join(i['name'] for i in due[:4])}) vs ${st.new_balance} at {st.period_end}"})
    return out


class _negated:
    """A deposit dressed as a debit so find_recurring can spot pay days."""
    def __init__(self, t):
        self.id, self.date, self.account_id, self.kind = t.id, t.date, t.account_id, t.kind
        self.amount, self.title, self.description, self.category = -t.amount, t.title, t.description, "Income"


# ─────────────────────────────────────────────
# Card payoff plan
# ─────────────────────────────────────────────

def plan_card_payments(user, monthly_budget=None, **kwargs):
    from expenses.account_views import _card
    from financial_planner.models import Debt

    cards = []
    for a in Account.objects.filter(user=user, kind="credit_card").select_related("debt"):
        card = _card(a)
        if not card["statement_balance"]:
            continue
        balance = max(Decimal(card["estimated_balance"]), D0)
        apr = a.debt.annual_interest_rate if a.debt else None
        minimum = Decimal(card["minimum_payment"]) if card["minimum_payment"] else (a.debt.minimum_payment if a.debt else None)
        cards.append({"name": a.name, "balance": balance, "apr": apr, "minimum": minimum or max(Decimal("10"), balance * Decimal("0.03")),
                      "min_share": (minimum / Decimal(card["statement_balance"])) if minimum and Decimal(card["statement_balance"]) > 0 else Decimal("0.03"),
                      "due": card["payment_due"], "statement_balance": Decimal(card["statement_balance"]),
                      "paid_since": Decimal(card["paid_since"])})
    # Cards without statements but with a Planner debt.
    linked = set(Account.objects.filter(user=user, debt__isnull=False).values_list("debt_id", flat=True))
    for d in Debt.objects.filter(user=user, is_active=True, debt_type="credit_card").exclude(id__in=linked):
        if d.current_balance > 0:
            cards.append({"name": d.name, "balance": d.current_balance, "apr": d.annual_interest_rate,
                          "minimum": d.minimum_payment, "min_share": d.minimum_payment / d.current_balance, "due": f"day {d.due_day}" if d.due_day else None,
                          "statement_balance": d.current_balance, "paid_since": D0})
    if not cards:
        return {"ok": True, "message": "No card balances found — upload card statements or add your cards as debts."}

    missing_apr = [c["name"] for c in cards if c["apr"] is None]
    for c in cards:
        if c["apr"] is None:
            c["apr"] = Decimal("20.99")             # typical Canadian card rate, flagged below
    total_min = sum((c["minimum"] for c in cards), D0)
    try:
        budget = Decimal(str(monthly_budget)) if monthly_budget not in (None, "") else None
    except Exception:
        budget = None
    if budget is not None and budget < total_min:
        return {"ok": False, "message": f"${budget} is less than the minimums (${_money(total_min)}). "
                                        f"Paying less than the minimum leads to fees and credit damage."}

    def minimum_due(c, balance, interest):
        """Like the banks: at least $10, and enough to cover the interest plus
        part of the balance (the share this card's statement asked for)."""
        share = max(c["min_share"] - c["apr"] / 1200, Decimal("0.01"))
        return min(balance, max(Decimal("10"), interest + balance * share))

    def simulate(pay_per_month, avalanche=True):
        """Months to clear every card and interest paid. pay_per_month None =
        minimums only."""
        bal = {c["name"]: c["balance"] for c in cards}
        interest_total, months, first = D0, 0, None
        while any(v > 0 for v in bal.values()) and months < 600:
            months += 1
            mins = {}
            for c in cards:
                i = (bal[c["name"]] * c["apr"] / 1200).quantize(Decimal("0.01")) if bal[c["name"]] > 0 else D0
                bal[c["name"]] += i
                interest_total += i
                mins[c["name"]] = minimum_due(c, bal[c["name"]], i)
            left = sum(mins.values(), D0) if pay_per_month is None else pay_per_month
            plan = {}
            for c in cards:                          # minimums first
                p = min(bal[c["name"]], mins[c["name"]], left)
                bal[c["name"]] -= p; left -= p; plan[c["name"]] = p
            order = sorted(cards, key=lambda c: -c["apr"]) if avalanche else []
            for c in order:                          # the rest to the highest rate
                p = min(bal[c["name"]], left)
                bal[c["name"]] -= p; left -= p; plan[c["name"]] += p
            if first is None:
                first = plan
        return months, interest_total, first

    min_months, min_interest, _ = simulate(None, avalanche=False)
    result = {
        "ok": True,
        "cards": [{"name": c["name"], "owed_now_estimate": _money(c["balance"]), "apr_percent": str(c["apr"]),
                   "minimum": _money(c["minimum"]), "due": c["due"],
                   "pay_in_full_to_avoid_interest": _money(max(c["statement_balance"] - c["paid_since"], D0)),
                   "monthly_interest_now": _money(c["balance"] * c["apr"] / 1200)} for c in sorted(cards, key=lambda c: -c["apr"])],
        "total_owed": _money(sum((c["balance"] for c in cards), D0)),
        "total_minimums": _money(total_min),
        "minimums_only": {"months_to_debt_free": min_months if min_months < 600 else "50+ years",
                          "total_interest": _money(min_interest)},
        "assumed_apr_for": missing_apr,
        "method": "Avalanche: every card gets its minimum, everything extra goes to the highest interest rate.",
    }
    budgets = [budget] if budget else [total_min + Decimal(x) for x in (200, 500, 1000)]
    result["plans"] = []
    for b in budgets:
        months, interest, first = simulate(b)
        result["plans"].append({
            "monthly_payment": _money(b), "months_to_debt_free": months if months < 600 else "600+",
            "debt_free_by": (date.today().replace(day=1) + timedelta(days=31 * months)).strftime("%B %Y") if months < 600 else None,
            "total_interest": _money(interest),
            "interest_saved_vs_minimums": _money(max(min_interest - interest, D0)),
            "this_month": [{"card": k, "pay": _money(v)} for k, v in (first or {}).items()],
        })
    return result


# ─────────────────────────────────────────────
# Budgets
# ─────────────────────────────────────────────

def budget_status(user, month=None, **kwargs):
    ym = (month or f"{date.today():%Y-%m}")[:7]
    start, end = _month_bounds(ym)
    out = []
    for b in SpendingBudget.objects.filter(user=user):
        r = search_transactions(user, query=b.query or None, theme=b.theme or None, category=b.category or None,
                                date_from=str(start), date_to=str(end), limit=1)
        spent = Decimal(r.get("net") or r.get("total_spent") or "0") if r.get("count") else D0
        days, today = end.day, date.today()
        elapsed = (min(today, end) - start).days + 1 if today >= start else 0
        projected = (spent / elapsed * days).quantize(Decimal("0.01"), ROUND_UP) if elapsed and today <= end else spent
        out.append({"label": b.label, "limit": _money(b.monthly_limit), "spent": _money(spent),
                    "left": _money(b.monthly_limit - spent), "month": ym,
                    "percent_used": round(float(spent / b.monthly_limit * 100), 1) if b.monthly_limit else 0,
                    "projected_month_end": _money(projected),
                    "covers": b.category or b.theme or b.query})
    return {"ok": True, "month": ym, "budgets": out}
