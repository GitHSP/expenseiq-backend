"""
Quick answers: the questions people ask most, answered straight from the
user's own figures, with no AI call. Free, instant, and the same every time.
answer(user, question) returns {"reply", "intent"} or None when the question
isn't one of these (then the AI takes it, if it's available).
"""
import re
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

D0 = Decimal("0")
SUGGESTIONS = [
    "When am I debt-free?",
    "What's due this week?",
    "How much did I spend this month?",
    "Which card should get the extra?",
    "How much can I still spend this month?",
    "How are my savings goals?",
    "What's my net worth?",
    "How much interest am I paying?",
]


def money(v):
    v = Decimal(str(v or 0))
    return f"{'-' if v < 0 else ''}${abs(v):,.2f}"


def _month(question, today):
    """(YYYY-MM, label) from "last month" / a month name; this month otherwise."""
    q = question.lower()
    if "last month" in q or "previous month" in q:
        first = today.replace(day=1) - timedelta(days=1)
        return f"{first:%Y-%m}", f"in {first:%B}"
    for i, name in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                              "september", "october", "november", "december"), 1):
        if re.search(rf"\b{name}\b|\b{name[:3]}\b", q):
            year = today.year if i <= today.month else today.year - 1
            return f"{year}-{i:02d}", f"in {name.title()}"
    return f"{today:%Y-%m}", "this month"


def _partial(user, ym, today):
    """A note when that month's statements aren't all uploaded yet."""
    from financial_planner.outlook import last_full_month
    full = last_full_month(user, today)
    return " (only part of that month's statements are uploaded so far)" if not full or ym > full else ""


# ── the answers ──

def debt_free(user, q, today):
    from financial_planner.outlook import build_outlook
    o = build_outlook(user, today=today)
    if not o["has_debts"]:
        return "You have no active debts in your Planner. You're debt-free!"
    s = o["scenarios"]["planner"]
    if not s["months"]:
        return (f"At your Planner's numbers, your debts don't get paid off: there isn't enough left after bills "
                f"to cover the minimums and interest on {money(o['total_debt'])}. Analytics → Your plan shows what it would take.")
    when = date.fromisoformat(s["date"])
    y, m = divmod(s["months"], 12)
    span = " ".join(p for p in (f"{y} year{'s' * (y != 1)}" if y else "", f"{m} month{'s' * (m != 1)}" if m else "") if p)
    return (f"You're on track to be debt-free by {when:%B %Y}: {span} from now, paying {money(s['budget'])} a month "
            f"toward {money(o['total_debt'])} of debt, with about {money(s['interest'])} more in interest.")


def due(user, q, today):
    from financial_planner.reminders import build_reminders
    days = 30 if "month" in q else 7
    r = build_reminders(user, today=today, days=days)
    if not r["due"]:
        return f"Nothing is due in the next {days} days, going by your Recurring payments and debts."
    lines = [f"• {d['label']}: {money(d['amount'])}, "
             f"{'today' if d['days'] == 0 else 'tomorrow' if d['days'] == 1 else date.fromisoformat(d['date']).strftime('%a %b ') + str(int(d['date'][8:]))}"
             + (" (automatic)" if d["auto"] else "") for d in r["due"]]
    total = sum((Decimal(d["amount"]) for d in r["due"]), D0)
    return f"Due in the next {days} days ({money(total)} in total):\n" + "\n".join(lines)


def spending(user, q, today):
    from .advisor import _month_figures
    from .insights import THEMES, search_transactions
    ym, label = _month(q, today)
    theme = next((t for t in THEMES if re.search(rf"\b{re.escape(t.split(' ')[0])}", q)), None)
    if theme:
        y, m = (int(x) for x in ym.split("-"))
        from calendar import monthrange
        r = search_transactions(user, theme=theme, date_from=f"{ym}-01", date_to=f"{ym}-{monthrange(y, m)[1]}", limit=1)
        if not r.get("count"):
            return f"I didn't find any {theme} spending {label}."
        top = (r.get("by_merchant") or [{}])[0]
        cards = r.get("by_account") or []
        return (f"You spent {money(r.get('net') or r.get('total_spent'))} on {theme} {label}, over {r['count']} purchase"
                f"{'s' * (r['count'] != 1)}" + (f". Most of it at {top['merchant']} ({money(top['total'])})" if top.get("merchant") else "")
                + (". By account: " + ", ".join(f"{a['account']} {money(a['total'])}" for a in cards[:4]) if cards else "") + ".")
    f = _month_figures(user, ym)
    if not f["spent"] and not f["income"]:
        return f"I don't have any transactions {label} yet. Upload that month's statements first."
    top = sorted(f["categories"].items(), key=lambda kv: -kv[1])[:3]
    parts = ", ".join(f"{name} {money(v)}" for name, v in top)
    return (f"You spent {money(f['spent'])} {label}" + (f" against {money(f['income'])} of income" if f["income"] else "")
            + _partial(user, ym, today) + (f". Biggest: {parts}." if parts else "."))


def income(user, q, today):
    from .advisor import _month_figures
    ym, label = _month(q, today)
    f = _month_figures(user, ym)
    return f"Your income {label} was {money(f['income'])}{_partial(user, ym, today)}" + (
        f", and you spent {money(f['spent'])}." if f["spent"] else ".")


def left_to_spend(user, q, today):
    from .advisor import _month_figures
    from financial_planner.outlook import build_outlook
    o = build_outlook(user, today=today)
    b = o["basis"]
    planned = Decimal(str(b["planner_pay"])) - Decimal(str(b["planner_bills"])) - Decimal(str(b["planner_savings"])) \
        - Decimal(str(o["debts_need"]))
    f = _month_figures(user, f"{today:%Y-%m}")
    return (f"Your Planner leaves {money(planned)} a month after bills, savings and debt minimums. "
            f"So far this month you've spent {money(f['spent'])} on everything (bills included) "
            f"against {money(f['income'])} of income received{_partial(user, f'{today:%Y-%m}', today)}. Anything left over is best put on your highest-rate debt.")


def extra(user, q, today):
    from financial_planner.outlook import build_outlook
    o = build_outlook(user, today=today)
    if not o["has_debts"]:
        return "You have no debts to pay extra on."
    s = o["scenarios"]["planner"]
    rows = sorted(((d["name"], v["extra"]) for d in o["debts"] for k, v in s["debts"].items()
                   if str(d["id"]) == k and v["extra"] >= 1), key=lambda r: -r[1])
    if not rows:
        top = max(o["debts"], key=lambda d: d["rate"])
        return (f"Your Planner has nothing extra after the minimums right now. When you do, put it on {top['name']} "
                f"({top['rate']}%), your highest rate.")
    return ("Put the extra on " + ", then ".join(f"{n} ({money(v)})" for n, v in rows)
            + ". Highest rate first saves the most interest.")


def goals(user, q, today):
    from financial_planner.models import SavingsGoal
    gs = list(SavingsGoal.objects.filter(user=user))
    if not gs:
        return "You don't have any savings goals yet. Add one in Planner → Savings."
    lines = []
    for g in gs:
        pct = min(g.saved / g.target * 100, 100) if g.target else 0
        lines.append(f"• {g.name}: {money(g.saved)} of {money(g.target)} ({pct:.0f}%)"
                     + (f", by {g.target_date:%b %Y}" if g.target_date else ""))
    return "Your savings goals:\n" + "\n".join(lines)


def net_worth(user, q, today):
    from expenses.models import Account, Statement
    from financial_planner.models import Debt, Investment
    from financial_planner.investments import summarize
    banks = D0
    for acc in Account.objects.filter(user=user, kind__in=("chequing", "savings"), closed=False):
        st = Statement.objects.filter(account=acc, new_balance__isnull=False).order_by("-period_end").first()
        banks += st.new_balance if st else D0
    debts = sum((d.current_balance for d in Debt.objects.filter(user=user, is_active=True)), D0)
    inv = {}
    for i in Investment.objects.filter(user=user, is_active=True).prefetch_related("payments"):
        inv[i.currency] = inv.get(i.currency, D0) + Decimal(str(summarize(i)["worth"] or 0))
    cad = banks + inv.pop("CAD", D0) - debts
    other = " plus " + ", ".join(f"{c} {v:,.0f} in investments" for c, v in inv.items()) if inv else ""
    return (f"Bank balances {money(banks)}, debts {money(debts)}: {money(cad)} in CAD{other}. "
            f"The Invest page converts it all to one total.")


def interest(user, q, today):
    from .advisor import _month_figures
    from financial_planner.models import Debt
    debts = list(Debt.objects.filter(user=user, is_active=True, current_balance__gt=0))
    monthly = sum((d.current_balance * d.annual_interest_rate / 1200 for d in debts), D0)
    last = (today.replace(day=1) - timedelta(days=1))
    f = _month_figures(user, f"{last:%Y-%m}")
    charged = f["interest"] + f["fees"]
    return (f"At today's balances your debts cost about {money(monthly)} a month in interest."
            + (f" Your statements show {money(charged)} of interest and fees charged in {last:%B}." if charged else ""))


def limits(user, q, today):
    from .advisor import budget_status
    bs = budget_status(user, month=f"{today:%Y-%m}")["budgets"]
    if not bs:
        return "You haven't set any spending limits. Add them in Planner → Limits."
    return "Your limits this month:\n" + "\n".join(
        f"• {b['label']}: {money(b['spent'])} of {money(b['limit'])} ({b['percent_used']:.0f}%)" for b in bs)


def credit_score(user, q, today):
    from financial_planner.reminders import REPORTED_TARGET, _closing_soon
    from financial_planner.models import Debt
    cards = Debt.objects.filter(user=user, is_active=True, credit_limit__gt=0)
    if not cards:
        return "Add your card limits in Planner → Debts and I can show how much of them you're using."
    used = sum((c.current_balance for c in cards), D0)
    limit = sum((c.credit_limit for c in cards), D0)
    soon = _closing_soon(user, today, days=31)
    tip = ("\n" + "\n".join(f"• {c['label']}: pay {money(c['to_target'])} before {date.fromisoformat(c['date']):%b %d}"
                            for c in soon[:3])) if soon else ""
    return (f"You're using {used / limit * 100:.0f}% of your card limits ({money(used)} of {money(limit)}). "
            f"Under {REPORTED_TARGET * 100:.0f}% usually helps your score. What counts is the balance on each statement date"
            + (", so these help most:" if soon else ".") + tip)


def help_text(user, q, today):
    return "I can answer these straight away:\n" + "\n".join(f"• {s}" for s in SUGGESTIONS)


# First match wins, so the more specific questions come first.
INTENTS = [
    (r"debt[- ]?free|pay(ing)? (it all |everything )?off|out of debt|when .*(done|free)", debt_free),
    (r"credit score|utili[sz]ation|30 ?%|statement date|report", credit_score),
    (r"which (card|debt)|(where|what).*(extra|put|pay first)|pay first|avalanche", extra),
    (r"\b(due|upcoming|coming up)\b|bills? (this|next)|pay (this|next) (week|month)", due),
    (r"\bgoals?\b|saving(s)? for|\bsaved\b", goals),
    (r"net ?worth|what am i worth|how much do i own", net_worth),
    (r"interest", interest),
    (r"\blimits?\b|\bbudgets?\b", limits),
    (r"left (to spend|this month|over)|can i (still )?spend|safe to spend|how much .*left", left_to_spend),
    (r"\b(income|earn(ed)?|salary|got paid|pay ?cheque)\b", income),
    (r"spen[dt]|spending|expenses?|cost me", spending),
    (r"what can you|help me|^help$|^\?$", help_text),
]


def answer(user, question, today=None):
    q = " ".join(str(question or "").lower().split())
    if not q or len(q) > 140:            # long questions need real reasoning
        return None
    today = today or timezone.localdate()
    for pattern, fn in INTENTS:
        if re.search(pattern, q):
            return {"reply": fn(user, q, today), "intent": fn.__name__}
    return None
