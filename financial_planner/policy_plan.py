"""
A universal life policy's plan, from its own documents: the insurer's cost of
insurance table, surrender charges, the fund values on its statements and the
advisor's illustration. The user pastes this once per policy (it's personal,
so it never goes into the code); see validate_plan for the shape.

build_plan works out:
  today         - paid in, fund value, what cancelling would pay, gain
  years         - each statement year: premiums in, insurance + fees out, growth
  projections   - the fund month by month to age 100 at a few returns, the way
                  the insurer does it: premium in (less premium tax), then the
                  policy fee, the cost of insurance on the amount at risk
                  (death benefit - fund) and any riders come out, then what's
                  left grows. It shows when the fund would run out.
  total_to_pay  - premiums paid so far plus the ones still to come
  insurance_cost- what the cover costs a year at different ages
"""
import math
from datetime import date

from .investments import add_months, months_between

MAX_AGE = 100


class PlanError(ValueError):
    pass


def _num(v, field, lo=None, hi=None, allow_none=False):
    if v is None and allow_none:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise PlanError(f"{field} must be a number.")
    v = float(v)
    if not math.isfinite(v) or (lo is not None and v < lo) or (hi is not None and v > hi):
        raise PlanError(f"{field} is out of range.")
    return v


def _date(v, field):
    try:
        return date.fromisoformat(str(v)).isoformat()
    except (TypeError, ValueError):
        raise PlanError(f"{field} must be a date like 2025-10-16.")


def _list(v, field, most):
    if not isinstance(v, list) or len(v) > most:
        raise PlanError(f"{field} must be a list of at most {most} items.")
    return v


def validate_plan(data):
    """The pasted plan, checked and cleaned; raises PlanError with a message for the user."""
    if data in (None, {}):
        return {}
    if not isinstance(data, dict):
        raise PlanError("The policy data must be a JSON object.")
    out = {
        "version": 1,
        "face": _num(data.get("face"), "face", 1, 1e9),
        "issue_age": int(_num(data.get("issue_age"), "issue_age", 0, 99)),
        "premium": _num(data.get("premium"), "premium", 0, 1e7),
        "pay_until_age": None if data.get("pay_until_age") is None
        else int(_num(data.get("pay_until_age"), "pay_until_age", 1, MAX_AGE)),
        "premium_tax": _num(data.get("premium_tax", 0), "premium_tax", 0, 0.5),
        "monthly_fee": _num(data.get("monthly_fee", 0), "monthly_fee", 0, 1e5),
        "bonus": _num(data.get("bonus", 0), "bonus", 0, 10),
        # "level": the death benefit is the cover (or the fund, if more), so the insurer's
        # risk is cover - fund. "plus_fund": the cover plus the fund is paid, so the
        # insurer's risk is always the whole cover.
        "death_benefit": data.get("death_benefit", "level"),
        "rates": [_num(r, "rates", -10, 30) for r in _list(data.get("rates", [4, 6, 8]), "rates", 5)] or [4, 6, 8],
        "note": str(data.get("note", ""))[:300],
    }
    if out["death_benefit"] not in ("level", "plus_fund"):
        raise PlanError("death_benefit must be level or plus_fund.")
    coi = data.get("coi")
    if not isinstance(coi, dict):
        raise PlanError("coi (the cost of insurance table) is missing.")
    if coi.get("unit") not in ("yearly_per_1000", "monthly_per_1000") or coi.get("by") not in ("policy_year", "age"):
        raise PlanError("coi.unit must be yearly_per_1000 or monthly_per_1000, and coi.by policy_year or age.")
    out["coi"] = {"unit": coi["unit"], "by": coi["by"], "start": int(_num(coi.get("start", 1), "coi.start", 0, 120)),
                  "rates": [_num(r, "coi.rates", 0, 1000) for r in _list(coi.get("rates"), "coi.rates", 120)]}
    out["riders"] = [{"name": str(r.get("name", "Rider"))[:80], "monthly": _num(r.get("monthly"), "riders.monthly", 0, 1e5),
                      "until_age": int(_num(r.get("until_age", MAX_AGE), "riders.until_age", 0, MAX_AGE))}
                     for r in _list(data.get("riders", []), "riders", 10) if isinstance(r, dict)]
    out["surrender"] = [_num(a, "surrender", 0, 1e8) for a in _list(data.get("surrender", []), "surrender", 30)]
    out["values"] = sorted(({"date": _date(v.get("date"), "values.date"), "fund": _num(v.get("fund"), "values.fund", -1e8, 1e9)}
                            for v in _list(data.get("values", []), "values", 600) if isinstance(v, dict)),
                           key=lambda v: v["date"])
    out["years"] = [{"from": _date(y.get("from"), "years.from"), "to": _date(y.get("to"), "years.to"),
                     **{k: _num(y.get(k), f"years.{k}", -1e8, 1e9, allow_none=True)
                        for k in ("premiums", "charges", "tax", "growth", "fund_start", "fund_end")}}
                    for y in _list(data.get("years", []), "years", 100) if isinstance(y, dict)]
    ill = data.get("illustration")
    if ill:
        if not isinstance(ill, dict):
            raise PlanError("illustration must be an object.")
        out["illustration"] = {
            "rate": _num(ill.get("rate"), "illustration.rate", -10, 30),
            "rows": [{"year": int(_num(r.get("year"), "illustration.year", 1, 120)),
                      "age": int(_num(r.get("age"), "illustration.age", 0, 120)),
                      **{k: _num(r.get(k), f"illustration.{k}", 0, 1e10, allow_none=True)
                         for k in ("deposit", "value", "surrender", "death_benefit")}}
                     for r in _list(ill.get("rows"), "illustration.rows", 120) if isinstance(r, dict)],
        }
    return out


# ── the engine ──

def _coi_month(plan, policy_year, age, at_risk):
    c = plan["coi"]
    i = (policy_year if c["by"] == "policy_year" else age) - c["start"]
    rate = c["rates"][i] if 0 <= i < len(c["rates"]) else 0.0
    per_month = rate / 12 if c["unit"] == "yearly_per_1000" else rate
    return per_month * max(at_risk, 0) / 1000


def _riders(plan, age):
    return sum(r["monthly"] for r in plan["riders"] if age < r["until_age"])


def surrender_charge(plan, policy_year):
    s = plan["surrender"]
    return s[policy_year - 1] if 1 <= policy_year <= len(s) else 0.0


def _paying(plan, age):
    return plan["pay_until_age"] is None or age < plan["pay_until_age"]


def project(plan, start, from_date, fund, paid, rate):
    """The fund from (from_date, fund) to age 100 at `rate` % a year. Returns
    yearly points at each policy anniversary and the age it runs out, if it does."""
    g = (1 + rate / 100) * (1 + plan["bonus"] / 100)
    g = g ** (1 / 12) - 1
    m0 = months_between(start, date.fromisoformat(from_date))
    points, lapse_age, cost_year = [], None, 0.0
    m = m0
    while True:
        policy_year, age = m // 12 + 1, plan["issue_age"] + m // 12
        if age >= MAX_AGE:
            break
        if _paying(plan, age):
            fund += plan["premium"] * (1 - plan["premium_tax"])
            paid += plan["premium"]
        at_risk = plan["face"] if plan.get("death_benefit") == "plus_fund" else plan["face"] - fund
        cost = plan["monthly_fee"] + _coi_month(plan, policy_year, age, at_risk) + _riders(plan, age)
        fund -= cost
        cost_year += cost
        if fund < 0:
            lapse_age = age
            break
        fund *= 1 + g
        m += 1
        if m % 12 == 0:            # a policy anniversary
            py = m // 12 + 1
            points.append({"age": plan["issue_age"] + m // 12, "date": add_months(start, m).isoformat(),
                           "paid": round(paid, 2), "fund": round(fund, 2),
                           "surrender": round(max(fund - surrender_charge(plan, py), 0), 2),
                           "cost": round(cost_year, 2)})
            cost_year = 0.0
    return points, lapse_age


def build_plan(inv, today=None):
    plan = inv.plan or {}
    if not plan.get("coi"):
        return None
    today = today or date.today()
    start = inv.start_date
    months_in = months_between(start, today) + 1 if today >= start else 0     # premiums due so far
    age_now = plan["issue_age"] + months_between(start, today) // 12
    paid_now = plan["premium"] * sum(1 for k in range(months_in) if _paying(plan, plan["issue_age"] + k // 12))

    last = plan["values"][-1] if plan["values"] else None
    if last:
        from_date, fund = last["date"], last["fund"]
        # The value is at the end of a month, so the next premium starts the following month.
        months_by_then = months_between(start, date.fromisoformat(from_date)) + 1
        from_date = add_months(start, months_by_then).isoformat()
    else:
        from_date, fund, months_by_then = start.isoformat(), 0.0, 0
    paid_by_then = plan["premium"] * sum(1 for k in range(months_by_then) if _paying(plan, plan["issue_age"] + k // 12))

    policy_year_now = months_between(start, today) // 12 + 1
    charge = surrender_charge(plan, policy_year_now)
    free_year = next((y for y in range(len(plan["surrender"]), 0, -1) if plan["surrender"][y - 1] > 0), 0) + 1

    projections = []
    for r in plan["rates"]:
        points, lapse = project(plan, start, from_date, fund, paid_by_then, r)
        at = {str(a): next((p["fund"] for p in points if p["age"] == a), None) for a in (45, 55, 65, 75, 85)}
        projections.append({"rate": r, "points": points, "lapse_age": lapse, "at": at})

    # Premiums: so far, and in total if paid until pay_until_age.
    total = None
    if plan["pay_until_age"] is not None:
        months_total = max((plan["pay_until_age"] - plan["issue_age"]) * 12, 0)
        total = {"amount": round(plan["premium"] * months_total, 2), "until_age": plan["pay_until_age"],
                 "until": add_months(start, months_total).isoformat(),
                 "left": round(plan["premium"] * max(months_total - months_in, 0), 2)}

    years = []
    for y in plan["years"]:
        avg = ((y["fund_start"] or 0) + (y["fund_end"] or 0)) / 2
        ret = round(y["growth"] / avg * 100, 1) if y["growth"] is not None and avg > 0 else None
        years.append({**y, "return_pct": ret})

    # What the cover costs a year at a few ages, on the full amount (an upper bound:
    # the fund lowers the amount at risk).
    cost_by_age = []
    for a in range(plan["issue_age"] + (10 - plan["issue_age"] % 10) % 10, MAX_AGE, 10):
        py = a - plan["issue_age"] + 1
        cost_by_age.append({"age": a, "yearly": round(12 * (plan["monthly_fee"] + _coi_month(plan, py, a, plan["face"])
                                                            + _riders(plan, a)), 2)})

    return {
        "issue_age": plan["issue_age"],
        "start": start.isoformat(),
        "age_now": age_now,
        "policy_year": policy_year_now,
        "today": {
            "paid": round(paid_now, 2),
            "fund": last["fund"] if last else None,
            "fund_date": last["date"] if last else None,
            "paid_by_fund_date": round(paid_by_then, 2),
            "gain": round(last["fund"] - paid_by_then, 2) if last else None,
            "surrender_charge": round(charge, 2),
            "surrender_value": round(max(last["fund"] - charge, 0), 2) if last else None,
            "surrender_free_from": add_months(start, (free_year - 1) * 12).isoformat() if plan["surrender"] else None,
        },
        "premium": plan["premium"],
        "pay_until_age": plan["pay_until_age"],
        "total_to_pay": total,
        "values": plan["values"],
        "years": years,
        "projections": projections,
        "insurance_cost": cost_by_age,
        "riders": plan["riders"],
        "face": plan["face"],
        "death_benefit": plan.get("death_benefit", "level"),
        "illustration": plan.get("illustration"),
        "note": plan["note"],
    }
