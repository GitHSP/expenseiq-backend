"""
Figures for an investment: what's been paid, what it's worth now, and what
it comes to at the end. All amounts are in the investment's own currency.

  insurance - premiums grow at the expected return, less the yearly cost
              (insurance, fee, tax) taken monthly; when the user enters the
              cash value from a statement, the projection starts from it.
              Its worth is the cash value less any surrender charge.
  rd        - each instalment compounds (quarterly for Indian banks) until
              maturity: sum of P * (1 + r/q) ** (q * months_held / 12).
  chit      - instalments differ every month, so they're logged; what's left
              is estimated from the recent ones. A prized chit's money may sit
              in a deposit earning simple interest.
"""
from calendar import monthrange
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
PER_YEAR = {"monthly": 12, "quarterly": 4, "half_yearly": 2, "yearly": 1}
COMPOUND = {"quarterly": 4, "monthly": 12, "yearly": 1}


def _money(v):
    return None if v is None else Decimal(v).quantize(CENT, rounding=ROUND_HALF_UP)


def months_between(a, b):
    """Whole months from a to b (0 when b is before a)."""
    if b < a:
        return 0
    return (b.year - a.year) * 12 + b.month - a.month - (1 if b.day < a.day else 0)


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def summarize(inv, today=None):
    today = today or date.today()
    fn = {"insurance": _insurance, "rd": _rd, "chit": _chit}[inv.kind]
    out = fn(inv, today)
    return {k: (str(_money(v)) if isinstance(v, Decimal) else v) for k, v in out.items()}


# ── insurance ──

def _insurance(inv, today):
    step = 12 // PER_YEAR.get(inv.payment_frequency, 12)
    pay = Decimal(inv.payment)
    total_periods = inv.paying_years * 12 // step if inv.paying_years else None
    started = today >= inv.start_date
    elapsed = months_between(inv.start_date, today)
    paid = min(elapsed // step + 1, total_periods or 10 ** 6) if started else 0
    g = (1 + Decimal(inv.rate) / 100) ** (Decimal(1) / 12)
    monthly_cost = Decimal(inv.yearly_cost or 0) / 12

    def premium_month(m):
        return m % step == 0 and (total_periods is None or m // step < total_periods)

    def grow(value, from_month, to_month, g=g):
        # value at the start of from_month (its premium included) -> to_month
        for m in range(from_month + 1, to_month + 1):
            value = max(value * g - monthly_cost, Decimal(0))
            if premium_month(m):
                value += pay
        return value

    if inv.current_value is not None:
        base_month = months_between(inv.start_date, inv.current_value_date or today)
        base, basis = Decimal(inv.current_value), "statement"
    else:
        base_month, base, basis = 0, pay, "estimate"
    value_now = grow(base, base_month, elapsed) if started else Decimal(0)

    maturity_date = add_months(inv.start_date, inv.term_years * 12) if inv.term_years else None
    projected = grow(base, base_month, inv.term_years * 12) if inv.term_years else None
    months_left = months_between(today, maturity_date) if maturity_date else None
    charge = Decimal(inv.surrender_charge) if inv.surrender_charge is not None else None
    surrender_value = max(value_now - charge, Decimal(0)) if charge is not None else value_now

    # Expected value: the fund at a few points ahead, at the expected return
    # and 2% either side (index-linked returns aren't guaranteed).
    rate = Decimal(inv.rate)
    rates = sorted({max(rate - 2, Decimal(0)), rate, rate + 2})
    points = [elapsed + 12, elapsed + 60, elapsed + 120]
    if inv.term_years and inv.term_years * 12 > points[-1]:
        points.append(inv.term_years * 12)
    expected = []
    if started:
        for at in points:
            row = {"date": add_months(inv.start_date, at).isoformat(),
                   "paid": str(_money(pay * min(at // step + 1, total_periods or 10 ** 6))), "values": []}
            for r in rates:
                gr = (1 + r / 100) ** (Decimal(1) / 12)
                row["values"].append(str(_money(grow(base, base_month, at, g=gr))))
            expected.append(row)
    per_month = pay / step
    split = ({"premium": str(_money(per_month)), "cost": str(_money(monthly_cost)),
              "invested": str(_money(max(per_month - monthly_cost, Decimal(0))))}
             if inv.yearly_cost else None)
    return {
        "paid_so_far": pay * paid,
        "payments_made": paid,
        "payments_left": max(total_periods - paid, 0) if total_periods is not None else None,
        "still_to_pay": pay * max(total_periods - paid, 0) if total_periods is not None else None,
        "premiums_end": add_months(inv.start_date, inv.paying_years * 12).isoformat() if inv.paying_years else None,
        "maturity_date": maturity_date.isoformat() if maturity_date else None,
        "months_left": months_left,
        "value_now": value_now,
        "value_basis": basis,                          # "statement" or "estimate"
        "projected_maturity": projected,
        "quoted_maturity": inv.maturity_value,
        "total_premiums": pay * total_periods if total_periods is not None else None,
        "surrender_charge": charge,
        "surrender_value": surrender_value,           # what cancelling now would pay
        "yearly_cost": inv.yearly_cost,
        "expected_rates": [str(r.normalize()) for r in rates],
        "expected": expected,
        "monthly_split": split,
        "worth": surrender_value,
    }


# ── recurring deposit ──

def _rd(inv, today):
    n = inv.months or 0
    pay = Decimal(inv.payment)
    q = COMPOUND.get(inv.compounding, 4)
    r = Decimal(inv.rate) / 100

    def held(months):
        return pay * (1 + r / q) ** (Decimal(q * months) / 12)

    calculated = sum((held(n - i) for i in range(n)), Decimal(0))
    maturity = Decimal(inv.maturity_value) if inv.maturity_value is not None else calculated
    maturity_date = add_months(inv.start_date, n)
    elapsed = months_between(inv.start_date, today)
    paid = min(elapsed + 1, n) if today >= inv.start_date else 0
    if today >= maturity_date:
        value_now = maturity
    else:
        value_now = sum((held(elapsed - i) for i in range(paid)), Decimal(0))
    return {
        "paid_so_far": pay * paid,
        "payments_made": paid,
        "payments_left": n - paid,
        "still_to_pay": pay * (n - paid),
        "maturity_date": maturity_date.isoformat(),
        "months_left": months_between(today, maturity_date),
        "calculated_maturity": calculated,
        "maturity": maturity,
        "interest": maturity - pay * n,
        "value_now": value_now,
        "matured": today >= maturity_date,
        "worth": value_now,
    }


# ── chit ──

def _chit(inv, today):
    payments = list(inv.payments.all())
    paid_total = sum((p.amount for p in payments), Decimal(0))
    count = len(payments)
    n = inv.months or 0
    recent = payments[-3:]
    per_month = (sum((p.amount for p in recent), Decimal(0)) / len(recent)) if recent else Decimal(inv.payment)
    left = max(n - count, 0)
    still_to_pay = per_month * left
    end_date = add_months(inv.start_date, n - 1) if n else None
    due = min(months_between(inv.start_date, today) + 1, n) if today >= inv.start_date else 0

    deposit = Decimal(inv.deposit_amount) if inv.prized and inv.deposit_where and inv.deposit_amount else None
    interest_now = interest_end = None
    if deposit is not None:
        since = inv.deposit_start or inv.prized_date or today
        rate = Decimal(inv.deposit_rate or 0) / 100

        def interest(until):
            return deposit * rate * Decimal(max((until - since).days, 0)) / 365

        interest_now = interest(today)
        interest_end = interest(max(end_date, today)) if end_date else interest_now

    if inv.prized:
        received = Decimal(inv.prize_amount or 0) + (interest_end or 0)
        worth = (deposit + interest_now if deposit is not None else Decimal(0)) - still_to_pay
    else:
        received = Decimal(inv.chit_value or 0)          # the full value, if taken in the last month
        worth = paid_total
    return {
        "paid_so_far": paid_total,
        "payments_made": count,
        "payments_left": left,
        "months_behind": max(due - count, 0),
        "per_month_estimate": per_month,
        "still_to_pay": still_to_pay,
        "end_date": end_date.isoformat() if end_date else None,
        "months_left": max(n - due, 0),
        "deposit_interest_so_far": interest_now,
        "deposit_interest_at_end": interest_end,
        "deposit_value_now": deposit + interest_now if deposit is not None else None,
        "net_result": received - paid_total - still_to_pay,
        "worth": worth,
    }
