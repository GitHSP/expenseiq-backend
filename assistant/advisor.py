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

def _shared_rules(user):
    from expenses.models import MerchantRule
    return [r.match.lower() for r in MerchantRule.objects.filter(user=user, treat_as="shared")]


def _month_figures(user, ym):
    start, end = _month_bounds(ym)
    shared = _shared_rules(user)
    income = spent = contributions = D0
    cats, merchants, names = defaultdict(lambda: D0), defaultdict(lambda: D0), {}
    interest = fees = D0
    for r in _rows(user, start, end):
        if r["transfer"]:
            continue
        if r["amount"] > 0 and r["account_kind"] != "credit_card":
            income += r["amount"]
            if shared and any(m in f"{r['title']} {r['description']}".lower() for m in shared):
                contributions += r["amount"]
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
            "interest": interest, "fees": fees, "contributions": contributions}


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
        "roommate_contributions": _money(cur["contributions"]),
        "income_excluding_contributions": _money(cur["income"] - cur["contributions"]),
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


# ─────────────────────────────────────────────
# Planned (Planner) vs actual (statements)
# ─────────────────────────────────────────────

def _planned_items(user, ym):
    """The month's Planner checklist, or the recurring payments if that
    month has no plan."""
    from financial_planner.models import ChecklistItem, MonthlyPlan, RecurringItem
    y, m = (int(x) for x in ym.split("-"))
    plan = MonthlyPlan.objects.filter(user=user, year=y, month=m).first()
    if plan:
        items = [{"label": i.label, "amount": i.amount or D0, "category": i.category, "due_day": i.due_day,
                  "debt": i.linked_debt, "done": i.is_completed}
                 for i in ChecklistItem.objects.filter(monthly_plan=plan).select_related("linked_debt")]
        if items:
            return items, "checklist"
    return ([{"label": r.label, "amount": r.amount, "category": r.category, "due_day": r.due_day, "debt": None, "done": None}
             for r in RecurringItem.objects.filter(user=user, is_active=True)], "recurring")


# Planner items that stand for a kind of spending rather than one bill.
LABEL_THEMES = [
    ("grocer", "groceries"), ("utilit", "utilities"), ("hydro", "utilities"), ("transit", "transit"),
    ("presto", "transit"), ("mobile", "phone & internet"), ("phone", "phone & internet"),
    ("internet", "phone & internet"), ("gym", "fitness"), ("fitness", "fitness"), ("insurance", "insurance"),
    ("rent", "rent & housing"), ("coffee", "coffee"), ("gas", "gas"), ("fuel", "gas"), ("dining", "restaurants"),
    ("restaurant", "restaurants"), ("eating out", "restaurants"), ("subscription", "subscriptions"),
    ("delivery", "food delivery"), ("uber", "rideshare"), ("alcohol", "alcohol"),
    ("bank charge", "bank fees"), ("bank fee", "bank fees"), ("service charge", "bank fees"),
    ("interest", "interest & fees"),
    ("abroad", "money abroad"), ("remit", "money abroad"), ("international", "money abroad"),
    ("wifi", "phone & internet"), ("wi-fi", "phone & internet"),
]
SHARED_THEMES = ("rent & housing", "utilities", "phone & internet")


def plan_vs_actual(user, month=None, **kwargs):
    """For each planned payment: what the statements show was actually paid."""
    from expenses.statement_import import _words
    ym = (month or _latest_full_month(user))[:7]
    try:
        start, end = _month_bounds(ym)
    except ValueError:
        return {"ok": False, "message": "month must look like 2026-09."}
    items, source = _planned_items(user, ym)
    if not items:
        return {"ok": True, "month": ym, "message": "No planned payments in the Planner for this month."}

    # A few days either side: a bill due on the 1st is often paid on the 30th.
    window = (start - timedelta(days=4), end + timedelta(days=4))
    txs = list(StatementTransaction.objects.filter(user=user, date__range=window).select_related("account"))
    covered = {a.id for a in Account.objects.filter(user=user, statements__period_start__lte=end,
                                                    statements__period_end__gte=start)}
    used, rows, debt_accounts = set(), [], {a.debt_id: a for a in Account.objects.filter(user=user, debt__isnull=False)}
    own_name = (user.first_name or "").strip().lower() if len((user.first_name or "").strip()) >= 3 else ""
    theme_owner = {}            # theme -> the planned item its spending is counted under

    def in_month(t):
        return start <= t.date <= end

    for item in sorted(items, key=lambda i: -i["amount"]):
        planned, label = item["amount"], item["label"]
        is_income = item["category"] == "income"
        matched, how = [], None
        card = debt_accounts.get(item["debt"].id) if item["debt"] else None
        if card:
            # A card payment: the credit on the card's own statement.
            matched = [t for t in txs if t.account_id == card.id and t.amount > 0 and in_month(t) and t.id not in used
                       and "payment" in f"{t.title} {t.description}".lower()]
            how = f"payments received on {card.name}"
        theme = next((t for key, t in LABEL_THEMES if key in label.lower()), None) if not card and not is_income else None
        if not matched and theme and theme in theme_owner:
            # Another planned item already covers this kind of spending.
            rows.append({"item": label, "category": item["category"], "theme": theme, "planned": _money(planned),
                         "actual": "0.00", "difference": _money(-planned), "status": f"counted under '{theme_owner[theme]}'",
                         "how_matched": None, "transactions": []})
            continue
        if not matched and theme:
            theme_owner[theme] = label
            pattern = re.compile(THEMES[theme], re.I)
            matched = [t for t in txs if t.amount < 0 and in_month(t) and t.id not in used and t.kind != "transfer"
                       and pattern.search(f"{t.title} {t.description}")]
            how = f"all '{theme}' spending that month"
        if not matched and not theme:
            words = _words(label) | (_words(item["debt"].name) if item["debt"] else set())
            cands = []
            for t in txs:
                if t.id in used or (t.amount > 0) != is_income:
                    continue
                if not in_month(t) and not (item["due_day"] and abs((t.date - start).days) <= 4):
                    continue
                overlap = len(words & _words(f"{t.title} {t.description}"))
                if overlap:
                    cands.append((overlap, t))
            if cands:
                best = max(c[0] for c in cands)
                matched = [t for o, t in cands if o == best]
                how = "matched by name"
        if not matched and planned > 0 and not theme:
            # Similar amount (within 15%) around the due day.
            for t in txs:
                if t.id in used or (t.amount > 0) != is_income or not in_month(t) or t.kind == "transfer":
                    continue
                text = f"{t.title} {t.description}".lower()
                if FEE_RE.search(text) or "interest" in text or (own_name and own_name in text):
                    continue
                near_due = item["due_day"] is None or abs(t.date.day - item["due_day"]) <= 5
                if near_due and abs(abs(t.amount) - planned) <= planned * Decimal("0.15"):
                    matched, how = [t], "matched by amount and date - check it's the right one"
                    break
        used.update(t.id for t in matched)
        actual = sum((abs(t.amount) for t in matched), D0)
        if matched:
            diff = actual - planned
            status = "as planned" if abs(diff) <= max(planned * Decimal("0.02"), Decimal("1")) else \
                     ("more than planned" if diff > 0 else "less than planned")
        else:
            status = "not found on statements" if covered else "no statement uploaded for this month"
        rows.append({"item": label, "category": item["category"], "theme": theme,
                     "planned": _money(planned), "actual": _money(actual),
                     "difference": _money(actual - planned), "status": status, "how_matched": how,
                     "transactions": [{"date": str(t.date), "title": t.title, "amount": _money(abs(t.amount)),
                                       "account": t.account.name} for t in matched[:6]]})

    cur = _month_figures(user, ym)
    out_items = [r for r in rows if r["category"] != "income"]
    in_items = [r for r in rows if r["category"] == "income"]
    planned_out = sum((Decimal(r["planned"]) for r in out_items), D0)
    actual_matched_out = sum((Decimal(r["actual"]) for r in out_items), D0)
    shared_block = None
    if cur["contributions"]:
        shared_rows = [r for r in rows if r.get("theme") in SHARED_THEMES]
        paid = sum((Decimal(r["actual"]) for r in shared_rows), D0)
        planned_share = sum((Decimal(r["planned"]) for r in shared_rows), D0)
        shared_block = {
            "bills": [r["item"] for r in shared_rows],
            "you_paid": _money(paid),
            "roommates_paid_you": _money(cur["contributions"]),
            "your_share": _money(paid - cur["contributions"]),
            "planned_share": _money(planned_share),
            "note": "Rent, utilities and internet the user pays in full, less what roommates/friends sent "
                    "(e-transfers marked as shared contributions). Compare your_share with planned_share.",
        }
    return {
        "ok": True, "month": ym, "planned_from": source,
        "items": rows,
        "shared_bills": shared_block,
        "planned_payments_total": _money(planned_out),
        "actual_for_those_payments": _money(actual_matched_out),
        "planned_income": _money(sum((Decimal(r["planned"]) for r in in_items), D0)),
        "actual_income_from_statements": _money(cur["income"]),
        "of_which_roommate_contributions": _money(cur["contributions"]),
        "actual_spending_from_statements": _money(cur["spent"]),
        "note": "Actual figures come only from uploaded statements. 'Actual spending' is everything spent that month "
                "(planned or not), excluding transfers between the user's own accounts.",
    }
