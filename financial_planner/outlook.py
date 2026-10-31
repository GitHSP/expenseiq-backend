"""
A look ahead: when the debts finish, what it takes to finish them by a chosen
date, and what could help next. Figures come from the user's own data by fixed
rules, month by month; they're estimates, not financial advice.

  minimums - every debt pays only its minimum. A card's minimum follows its
             interest (interest plus the part that's left of today's minimum),
             so it shrinks with the balance; a loan's payment stays the same.
  pay      - a fixed amount a month: current pay (the Planner's, else the
             statements' average) less average spending. Minimums come first, what's
             left goes to the highest rate, and a finished debt's payment moves on.
  planner  - the same, with the Planner's bills (recurring payments and fund
             savings) in place of what statements show was spent.
  pace     - the same, with the amount the user enters (or the pay figure).
  goal     - the lowest fixed amount that finishes by the target date.
"""
from calendar import monthrange
from datetime import date
from math import ceil

from django.utils import timezone

from .investments import add_months, summarize
from .models import Debt, EmergencyFund, Investment, RecurringItem

MAX_MONTHS = 600                      # 50 years: past this it's "not at this pace"
MIN_CARD_PRINCIPAL = 10.0             # a card's minimum always pays off at least this
PER_MONTH = {"weekly": 52 / 12, "biweekly": 26 / 12}
STALE_DAYS = 45
UTILIZATION_TARGETS = (50, 40, 30)    # % of the credit limit


def _debts(user):
    linked = {}
    for rec in RecurringItem.objects.filter(user=user, is_active=True, debt__isnull=False).exclude(category="income"):
        linked[rec.debt_id] = linked.get(rec.debt_id, 0.0) + float(rec.amount) * PER_MONTH.get(rec.frequency, 1)
    out = []
    for d in Debt.objects.filter(user=user, is_active=True, current_balance__gt=0).order_by("avalanche_order", "id"):
        balance, rate = float(d.current_balance), float(d.annual_interest_rate) / 100
        pays = linked.get(d.id, float(d.minimum_payment))
        follows_interest = d.debt_type == "credit_card" and d.id not in linked
        out.append({"id": d.id, "name": d.name, "balance": balance, "rate": rate, "pays": pays,
                    "follows_interest": follows_interest, "is_card": d.debt_type == "credit_card",
                    "principal": max(pays - balance * rate / 12, MIN_CARD_PRINCIPAL),
                    "limit": float(d.credit_limit) if d.credit_limit else None})
    return out


def simulate(debts, budget=None, max_months=MAX_MONTHS, track=False):
    """Month by month. budget=None: minimums only. Returns months (None if never
    finished), total interest, each debt's finishing month and, with track=True,
    each debt's first payment, its interest and the balances after every month."""
    bal = {d["id"]: d["balance"] for d in debts}
    finish, interest_total = {}, 0.0
    first_pay, interest_by, history = {}, {d["id"]: 0.0 for d in debts}, []
    order = sorted(debts, key=lambda d: -d["rate"])     # sort keeps avalanche order on equal rates
    if not debts:
        return {"months": 0, "interest": 0.0, "finish": finish, "first_pay": {}, "interest_by": {}, "history": []}
    for month in range(1, max_months + 1):
        due = {}
        for d in debts:
            b = bal[d["id"]]
            if b <= 0:
                continue
            i = b * d["rate"] / 12
            interest_total += i
            interest_by[d["id"]] += i
            bal[d["id"]] = b + i
            due[d["id"]] = min(b + i, i + d["principal"] if d["follows_interest"] else d["pays"])
        before = dict(bal)
        left = budget
        for d in order:
            if d["id"] not in due:
                continue
            pay = due[d["id"]] if left is None else min(due[d["id"]], left)
            bal[d["id"]] -= pay
            if left is not None:
                left -= pay
        if left:
            for d in order:
                pay = min(bal[d["id"]], left)
                bal[d["id"]] -= pay
                left -= pay
                if left <= 0:
                    break
        if month == 1:
            first_pay = {k: before[k] - bal[k] for k in bal}
        for d in debts:
            if bal[d["id"]] <= 0.005:
                bal[d["id"]] = 0.0
                finish.setdefault(d["id"], month)
        if track:
            history.append(dict(bal))
        if len(finish) == len(debts):
            break
    return {"months": month if len(finish) == len(debts) else None, "interest": interest_total, "finish": finish,
            "first_pay": first_pay, "interest_by": interest_by, "history": history}


def budget_for(debts, months):
    """The lowest whole-dollar monthly amount that finishes within the months."""
    if not debts:
        return 0
    lo, hi = 0.0, sum(d["balance"] for d in debts) * 2 + 1
    for _ in range(40):
        mid = (lo + hi) / 2
        if simulate(debts, budget=mid, max_months=months)["months"]:
            hi = mid
        else:
            lo = mid
    return ceil(hi)


def _rows(pairs):
    return [{"label": label, "amount": round(amount, 2)} for label, amount in pairs if abs(amount) >= 0.005]


def _averages(user, today):
    """Average monthly income and everyday spending over the last 3 months every
    account's statements fully cover; else what the Planner has planned."""
    from assistant.advisor import _month_figures
    from expenses.models import Account, Statement

    ends = []
    for acc in Account.objects.filter(user=user):
        st = Statement.objects.filter(account=acc).order_by("-period_end").first()
        if st and (today - st.period_end).days <= 90:
            ends.append(st.period_end)
    latest = max(ends) if ends else None
    months = []
    if ends:
        y, m = min(ends).year, min(ends).month
        if min(ends).day < monthrange(y, m)[1]:
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)       # that month isn't complete yet
        while len(months) < 3:
            if not Statement.objects.filter(user=user, period_start__lte=date(y, m, monthrange(y, m)[1]),
                                            period_end__gte=date(y, m, 1)).exists():
                break
            months.append(f"{y}-{m:02d}")
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    names = [d.name.lower() for d in Debt.objects.filter(user=user)]
    names += [r.label.lower() for r in RecurringItem.objects.filter(user=user, debt__isnull=False)]

    income = living = moved = 0.0
    by_category = {}
    for ym in months:
        fig = _month_figures(user, ym)
        for name, value in fig["categories"].items():
            by_category[name] = by_category.get(name, 0.0) + float(value)
        paying_debts = sum(float(v) for k, v in fig["merchants"].items()
                           if any(n in str(fig["names"].get(k, k)).lower() for n in names))
        income += float(fig["income"])
        living += float(fig["spent"]) - paying_debts
        moved += float(fig["spent"]) - paying_debts - sum(float(v) for v in fig["categories"].values())
    if months and income > 0:
        rows = [(name, value / len(months)) for name, value in by_category.items()]
        rows.sort(key=lambda r: -r[1])
        if abs(moved) >= 0.5:
            rows.append(("Less debt payments and roommates' shares", moved / len(months)))
        return {"source": "statements", "months": months, "income": income / len(months),
                "living": max(living / len(months), 0.0), "latest_statement": latest, "spending": _rows(rows)}

    planned = RecurringItem.objects.filter(user=user, is_active=True, debt__isnull=True)
    income = sum(float(r.amount) * PER_MONTH.get(r.frequency, 1) for r in planned if r.category == "income")
    living = sum(float(r.amount) * PER_MONTH.get(r.frequency, 1) for r in planned if r.category != "income")
    rows = [(r.label, float(r.amount) * PER_MONTH.get(r.frequency, 1)) for r in planned if r.category != "income"]
    return {"source": "plan", "months": [], "income": income, "living": living, "latest_statement": latest,
            "spending": _rows(rows)}


def _when(months, today):
    return {"months": months, "date": add_months(today, months).isoformat() if months else None}


def _utilization(debts, history, today):
    """How long until balances are under each share of the limits: all cards
    with a limit together, and each card alone. 0 months = already under."""
    cards = [d for d in debts if d["limit"]]
    if not cards:
        return None

    def reach(ids, target):
        limit = sum(d["limit"] for d in cards if d["id"] in ids)
        if sum(d["balance"] for d in cards if d["id"] in ids) / limit * 100 < target:
            return _when(0, today)
        for month, row in enumerate(history, 1):
            if sum(row[i] for i in ids) / limit * 100 < target:
                return _when(month, today)
        return _when(None, today)

    every = {d["id"] for d in cards}
    return {"overall": {str(t): reach(every, t) for t in UTILIZATION_TARGETS},
            "cards": {str(d["id"]): {str(t): reach({d["id"]}, t) for t in UTILIZATION_TARGETS} for d in cards}}


def _scenario(result, debts, today, budget=None):
    months = result["months"]
    return {**_when(months, today),
            "interest": round(result["interest"], 2) if months else None,
            "budget": budget,
            "balances": [round(sum(row.values())) for row in result["history"]],
            "debts": {str(d["id"]): {
                "payment": round(result["first_pay"].get(d["id"], 0), 2),
                "interest": round(result["interest_by"][d["id"]], 2) if months else None,
                **_when(result["finish"].get(d["id"]), today),
            } for d in debts},
            "utilization": _utilization(debts, result["history"], today)}


def _maturities(user, today):
    out = []
    for inv in Investment.objects.filter(user=user, is_active=True).prefetch_related("payments"):
        s = summarize(inv, today)
        when = s.get("maturity_date")
        if not when or s.get("matured"):
            continue
        d = date.fromisoformat(when)
        if (d - today).days <= 365:
            amount = s.get("maturity") or s.get("quoted_maturity") or s.get("projected_maturity")
            out.append({"name": inv.name, "date": when, "amount": amount, "currency": inv.currency})
    return sorted(out, key=lambda m: m["date"])


def _actions(user, debts, avg, pace_budget, pace, more, today):
    actions = []

    def add(level, title, text, **amounts):
        actions.append({"level": level, "title": title, "text": text,
                        "amounts": {k: round(v, 2) for k, v in amounts.items()}})

    latest = avg["latest_statement"]
    if latest is None or (today - latest).days > STALE_DAYS:
        add("warn", "Upload your latest statements",
            "Your newest statement is over 6 weeks old, so these figures may be out of date.")
    needed = sum(d["pays"] for d in debts)
    if debts and pace_budget < needed:
        add("warn", "Payments are more than what's left",
            "Income less spending leaves about {left} a month, but your debts need about {need}.",
            left=max(pace_budget, 0), need=needed)
    tight = [d for d in debts if d["limit"] and d["limit"] - d["balance"] <= d["limit"] * 0.05]
    if tight:
        d = min(tight, key=lambda t: t["limit"] - t["balance"])
        add("warn", f"{d['name']} is close to its limit",
            "It has {room} of room. Check what's due on it before your next pay, since going over can bring a fee.",
            room=max(d["limit"] - d["balance"], 0))
    if debts:
        top = max(d["rate"] for d in debts)
        same = [d["name"] for d in debts if top - d["rate"] < 0.0005]
        if len(debts) == 1:
            pass
        elif len(same) == len(debts):
            add("info", "Your debts cost about the same",
                "They all charge the same rate, so extra money saves the same interest on any of them.")
        elif len(same) == 1:
            add("info", f"Extra money saves the most on {same[0]}", f"It has your highest rate, {top * 100:.2f}%.")
        else:
            add("info", "Extra money saves the most on your highest-rate debts",
                f"{', '.join(same)} share the highest rate, {top * 100:.2f}%.")
    if pace["months"] and more["months"] and pace["months"] > more["months"]:
        add("info", "What an extra {extra} a month would do",
            f"You'd finish {pace['months'] - more['months']} months sooner and save about {{saved}} in interest.",
            extra=100, saved=max(pace["interest"] - more["interest"], 0))
    cards = [d for d in debts if d["follows_interest"]]
    if cards:
        d = min(cards, key=lambda c: c["principal"] / c["balance"])
        add("info", f"{d['name']}'s minimum barely moves the balance",
            "About {part} of its minimum pays down the balance each month; the rest is interest.",
            part=d["principal"])
    fund = EmergencyFund.objects.filter(user=user).first()
    if fund and not fund.is_funded and fund.target_amount:
        add("info", "Your emergency fund isn't full yet", "It has {have} of its {goal} goal.",
            have=float(fund.current_balance), goal=float(fund.target_amount))
    return actions[:5]


def build_outlook(user, target_months=36, budget=None, today=None):
    today = today or timezone.localdate()
    debts = _debts(user)
    avg = _averages(user, today)
    planned_pay = sum(float(r.amount) * PER_MONTH.get(r.frequency, 1) for r in
                      RecurringItem.objects.filter(user=user, is_active=True, category="income"))
    pay = planned_pay or avg["income"]
    estimated = pay - avg["living"]
    pay_budget = max(estimated, 0.0)
    pace_budget = budget if budget is not None else pay_budget
    active = RecurringItem.objects.filter(user=user, is_active=True)
    pay_rows = [(r.label, float(r.amount) * PER_MONTH.get(r.frequency, 1)) for r in active.filter(category="income")]
    bill_rows = [(r.label, float(r.amount) * PER_MONTH.get(r.frequency, 1))
                 for r in active.filter(debt__isnull=True).exclude(category="income")]
    bills = sum(amount for _, amount in bill_rows)
    fund = EmergencyFund.objects.filter(user=user).first()
    savings = float(fund.monthly_contribution) if fund and not fund.is_funded else 0.0
    planner_pay = planned_pay
    planner_budget = max(planner_pay - bills - savings, 0.0)

    minimum = simulate(debts, track=True)
    on_pay = simulate(debts, budget=pay_budget, track=True)
    on_planner = simulate(debts, budget=planner_budget, track=True)
    pace = simulate(debts, budget=pace_budget, track=True)
    more = simulate(debts, budget=pace_budget + 100)
    # Never below the minimums, even when a far-off date would allow less.
    goal_budget = max(budget_for(debts, target_months), ceil(sum(d["pays"] for d in debts)))
    goal = simulate(debts, budget=goal_budget, max_months=target_months, track=True)
    income_needed = goal_budget + avg["living"]
    latest_pay = "planner" if planned_pay else "statements"

    return {
        "as_of": today.isoformat(),
        "has_debts": bool(debts),
        "basis": {"source": avg["source"], "months": avg["months"], "income": round(avg["income"], 2),
                  "pay": round(pay, 2), "pay_source": latest_pay,
                  "planner_pay": round(planner_pay, 2), "planner_bills": round(bills, 2),
                  "planner_savings": round(savings, 2),
                  "breakdown": {
                      "pay": _rows(pay_rows) or _rows([("Average deposits on your statements", avg["income"])]),
                      "bills": _rows(sorted(bill_rows, key=lambda r: -r[1])),
                      "spending": avg["spending"],
                      "minimums": _rows([(d["name"], d["pays"]) for d in debts]),
                  },
                  "living": round(avg["living"], 2), "estimated_budget": round(estimated, 2),
                  "budget": round(pace_budget, 2), "entered": budget is not None},
        "total_debt": round(sum(d["balance"] for d in debts), 2),
        "monthly_interest": round(sum(d["balance"] * d["rate"] / 12 for d in debts), 2),
        "debts_need": round(sum(d["pays"] for d in debts), 2),
        "utilization_targets": list(UTILIZATION_TARGETS),
        "scenarios": {"minimums": _scenario(minimum, debts, today),
                      "pay": _scenario(on_pay, debts, today, round(pay_budget, 2)),
                      "planner": _scenario(on_planner, debts, today, round(planner_budget, 2)),
                      "pace": _scenario(pace, debts, today, round(pace_budget, 2)),
                      "goal": _scenario(goal, debts, today, goal_budget)},
        "goal": {"target_months": target_months, "target_date": add_months(today, target_months).isoformat(),
                 "budget": goal_budget, "income_needed": round(income_needed, 2),
                 "income_gap": round(income_needed - pay, 2)},
        "debts": [{"id": d["id"], "name": d["name"], "balance": round(d["balance"], 2),
                   "rate": round(d["rate"] * 100, 2),
                   "is_card": d["is_card"], "limit": round(d["limit"], 2) if d["limit"] else None,
                   "room": round(d["limit"] - d["balance"], 2) if d["limit"] else None,
                   "utilization": round(d["balance"] / d["limit"] * 100, 1) if d["limit"] else None}
                  for d in debts],
        "maturities": _maturities(user, today),
        "actions": _actions(user, debts, avg, pace_budget, pace, more, today),
    }
