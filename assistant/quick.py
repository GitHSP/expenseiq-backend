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
    "What are my planned recurring payments?",
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


# ── logging from chat: "spent $20 on gas", "paid $300 on RBC 7K", "undo" ──
# Plain patterns only; anything unclear is left for the AI (or gets a hint).

AMT = r"\$?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d{1,2})?\s?(?:dollars|bucks|cad)?"
WHEN = r"(?:\s+(today|yesterday|this morning|last night|tonight))?"
CHAT_NOTE = "Added in Aurora chat"
FILLER = re.compile(r"^(?:a|an|the|some|my)\s+", re.I)

# A first guess at the category when the user hasn't logged this name before.
GUESS = [
    (r"coffee|tim ?hortons|tims|starbucks|lunch|dinner|breakfast|food|restaurant|pizza|burger|uber ?eats|doordash|skip|snack|meal|takeout", "Food & Dining"),
    (r"grocer|walmart|costco|no ?frills|sobeys|loblaws|freshco|food basics|superstore|metro", "Household"),
    (r"\bgas\b|fuel|petro|esso|shell|uber|lyft|taxi|bus|presto|transit|train|parking|car wash", "Transport"),
    (r"smoke|cigarette|vape|tobacco|juul", "Smoke & Vape"),
    (r"beer|wine|liquor|lcbo|\bbar\b|drinks?|pub", "Drinks & Bars"),
    (r"movie|cinema|netflix|game|concert|tickets?", "Entertainment"),
    (r"gym|fitness|yoga", "Gym & Fitness"),
    (r"pharmacy|medicine|doctor|dentist|shoppers", "Medical & Health"),
    (r"haircut|salon|barber|laundry", "Personal Care & Beauty"),
    (r"clothes|shoes|shirt|amazon|shopping|dollarama", "Clothing & Shopping"),
    (r"gift|donation", "Gifts & Donations"),
    (r"phone|internet|hydro|bill", "Bills & Utilities"),
    (r"\brent\b", "Housing"),
    (r"book|course|tuition", "Education & Books"),
]
QUESTION = re.compile(r"\b(how|what|when|which|why|where|is|are|was|did|do|does|can|should)\b")


def _amount(whole, cents):
    return Decimal(whole.replace(",", "") + (cents or ""))


def _day(word, today):
    return today - timedelta(days=1) if word in ("yesterday", "last night") else today


def _title(text):
    t = FILLER.sub("", text.strip(" .!,")).strip()
    return t[:1].upper() + t[1:] if t else ""


def _debts_matching(user, name):
    """Active debts whose name has every word typed ("rbc 7k" -> "RBC Visa 7K")."""
    from financial_planner.models import Debt
    words = [w for w in re.split(r"\W+", name.lower()) if w and w not in ("my", "the", "card", "credit")]
    if not words:
        return []
    return [d for d in Debt.objects.filter(user=user, is_active=True)
            if all(w in d.name.lower() for w in words)]


def _category(user, title):
    """What the user used last time for this name; otherwise a guess from the words."""
    from expenses.models import Expense
    last = Expense.objects.filter(user=user, title__iexact=title).order_by("-date", "-id").first()
    if last:
        return last.category
    low = title.lower()
    return next((cat for pattern, cat in GUESS if re.search(pattern, low)), "Other")


def _add_expense(user, title, amount, day, today):
    from activity.context import activity_context
    from expenses.models import Expense
    if not title or amount <= 0:
        return None
    cat = _category(user, title)
    with activity_context(source="assistant"):
        Expense.objects.create(user=user, title=title[:255], amount=amount, category=cat, date=day, notes=CHAT_NOTE)
    when = "today" if day == today else f"on {day:%b} {day.day}"
    msg = f"Added {title}: {money(amount)} ({cat}) {when}."
    return {"reply": msg + ' Wrong? Say "undo", or change it on the Expenses page.', "intent": "log_expense",
            "actions": [{"tool": "add_expense", "input": {"title": title, "amount": str(amount)}, "message": msg}]}


def _pay_debt(user, debt, amount, today):
    from activity.context import activity_context
    from financial_planner.models import ChecklistItem
    from financial_planner.services import recalculate_avalanche_order, sync_current_month, toggle_checklist_item
    before = debt.current_balance
    # This month's checklist line for the card: tick it with what was paid, as the app does.
    item = ChecklistItem.objects.filter(monthly_plan__user=user, monthly_plan__year=today.year,
                                        monthly_plan__month=today.month, linked_debt=debt,
                                        category="debt_min", is_completed=False).first()
    ticked = bool(item and item.amount)
    with activity_context(source="assistant"):
        if ticked:
            toggle_checklist_item(item, paid=amount)
        else:
            debt.current_balance = max(D0, debt.current_balance - amount)
            if debt.current_balance == D0:
                debt.is_active = False
                debt.paid_off_date = today
            debt.save()
        recalculate_avalanche_order(user)
        sync_current_month(user)
    debt.refresh_from_db()
    msg = f"Recorded {money(amount)} paid on {debt.name}. Balance {money(before)} → {money(debt.current_balance)}."
    if debt.current_balance == D0:
        msg += " Paid off. Well done!"
    elif ticked:
        msg += " This month's checklist is ticked too."
    return {"reply": msg + " To change it, edit the balance in Planner → Debts.", "intent": "log_payment",
            "actions": [{"tool": "update_debt", "input": {"name": debt.name}, "message": msg}]}


def log_entry(user, text, today):
    """A reply when `text` is something to record, else None."""
    t = " ".join(str(text).strip().rstrip(".!").split())
    low = t.lower()

    if re.fullmatch(r"(undo|remove|delete)( that| it| the last one)?", low):
        from activity.context import activity_context
        from expenses.models import Expense
        last = Expense.objects.filter(user=user, notes=CHAT_NOTE,
                                      created_at__gte=timezone.now() - timedelta(hours=1)).order_by("-created_at").first()
        if not last:
            return {"reply": "There's nothing added in this chat in the last hour to undo. "
                             "Card payments are changed in Planner → Debts.", "intent": "undo"}
        with activity_context(source="assistant"):
            last.delete()
        msg = f"Removed {last.title} ({money(last.amount)})."
        return {"reply": msg, "intent": "undo",
                "actions": [{"tool": "delete_expense", "input": {"title": last.title}, "message": msg}]}

    # "paid $300 on/to/towards RBC 7K": the card, when it names one; otherwise an expense.
    m = re.fullmatch(rf"(?:i\s+)?(?:just\s+)?(?:paid|pay|payed|made a payment of)\s+{AMT}\s+(?:on|to|towards|toward|for|into|at)\s+(.+?){WHEN}", low)
    if m:
        amount, thing = _amount(m.group(1), m.group(2)), m.group(3)
        debts = _debts_matching(user, thing)
        if len(debts) == 1:
            return _pay_debt(user, debts[0], amount, today)
        if len(debts) > 1:
            names = ", ".join(d.name for d in debts)
            return {"reply": f'Which one: {names}? Say e.g. "paid {money(amount)} on {debts[0].name}".', "intent": "log_payment"}
        return _add_expense(user, _title(t[m.start(3):m.end(3)]), amount, _day(m.group(4), today), today)

    # "spent $20 on gas", "spent 12 at Tim Hortons yesterday"
    m = re.fullmatch(rf"(?:i\s+)?(?:just\s+)?(?:spent|spend)\s+{AMT}\s+(?:on|for|at)\s+(.+?){WHEN}", low)
    if m:
        return _add_expense(user, _title(t[m.start(3):m.end(3)]), _amount(m.group(1), m.group(2)), _day(m.group(4), today), today)

    # "bought lunch for $15", "had coffee for 4.50 today"
    m = re.fullmatch(rf"(?:i\s+)?(?:just\s+)?(?:bought|had|got|grabbed|paid for)\s+(.+?)\s+for\s+{AMT}{WHEN}", low)
    if m:
        return _add_expense(user, _title(t[m.start(1):m.end(1)]), _amount(m.group(2), m.group(3)), _day(m.group(4), today), today)

    # "$20 on gas", "15 for laundry"
    m = re.fullmatch(rf"{AMT}\s+(?:on|for|at)\s+(.+?){WHEN}", low)
    if m and not QUESTION.search(low):
        return _add_expense(user, _title(t[m.start(3):m.end(3)]), _amount(m.group(1), m.group(2)), _day(m.group(4), today), today)

    # "coffee 4.50", "smoke $19.28 yesterday": one to three words, then the amount
    m = re.fullmatch(rf"([a-z][a-z0-9'&\- ]{{0,40}}?)\s+{AMT}{WHEN}", low)
    if m and len(m.group(1).split()) <= 3 and not QUESTION.search(m.group(1)):
        amount = _amount(m.group(2), m.group(3))
        debts = _debts_matching(user, m.group(1))
        if debts:
            name = debts[0].name if len(debts) == 1 else m.group(1).upper()
            return {"reply": f'Did you pay {money(amount)} on {name}? Say "paid {money(amount)} on {name}" and I\'ll record it.',
                    "intent": "log_payment"}
        return _add_expense(user, _title(t[m.start(1):m.end(1)]), amount, _day(m.group(4), today), today)
    return None


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
    n = re.search(r"(?:next|coming)\s+(\d{1,2})\s+days?", q)
    days = (min(int(n.group(1)), 60) if n else 14 if re.search(r"(two|2)\s+weeks|fortnight", q)
            else 30 if "month" in q else 7)
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


def recurring(user, q, today):
    """Planned recurring payments and income, a month's worth (every 2 weeks = 26 a year)."""
    from financial_planner.models import Debt, RecurringItem
    per_month = {"monthly": Decimal(1), "varies": Decimal(1), "biweekly": Decimal(26) / 12, "weekly": Decimal(52) / 12}
    items = list(RecurringItem.objects.filter(user=user, is_active=True))
    if not items:
        return "You haven't added any recurring payments yet. Add them in Planner → Recurring."

    def line(i):
        m = i.amount * per_month.get(i.frequency, Decimal(1))
        when = (f"every 2 weeks, ≈ {money(m)}/month" if i.frequency == "biweekly"
                else f"weekly, ≈ {money(m)}/month" if i.frequency == "weekly"
                else f"day {i.due_day}" if i.due_day else "no due day")
        return m, f"• {i.label}: {money(i.amount)} ({when})" + (" · by card" if i.paid_by_card else "")

    out_rows = sorted((line(i) + (i.due_day or 99,) for i in items if i.category != "income"), key=lambda r: r[2])
    in_rows = [line(i) for i in items if i.category == "income"]
    spend = sum((r[0] for r in out_rows), D0)
    income = sum((r[0] for r in in_rows), D0)
    minimums = sum((d.minimum_payment or D0 for d in Debt.objects.filter(user=user, is_active=True)), D0)
    text = f"Planned payments come to {money(spend)} a month ({len(out_rows)} items):\n" + "\n".join(r[1] for r in out_rows)
    if minimums:
        text += f"\n\nPlus {money(minimums)} a month in debt minimums."
    if in_rows:
        text += f"\n\nPlanned income: {money(income)} a month:\n" + "\n".join(r[1] for r in in_rows)
        text += f"\n\nThat leaves {money(income - spend - minimums)} a month after payments and minimums."
    return text + "\n\n(Every-2-weeks items are counted as 26 a year.)"


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
    return ("I can answer these straight away:\n" + "\n".join(f"• {s}" for s in SUGGESTIONS)
            + '\n\nAnd log things for you: "spent $20 on gas", "coffee 4.50", "paid $300 on RBC". '
              'Say "undo" to take back the last expense.')


# First match wins, so the more specific questions come first.
INTENTS = [
    (r"debt[- ]?free|pay(ing)? (it all |everything )?off|out of debt|when .*(done|free)", debt_free),
    (r"credit score|utili[sz]ation|30 ?%|statement date|report", credit_score),
    (r"which (card|debt)|(where|what).*(extra|put|pay first)|pay first|avalanche", extra),
    (r"recurring|planned (payments?|bills?|expenses?|costs?)|(monthly|regular|fixed) (bills|payments|costs|expenses)", recurring),
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
    logged = log_entry(user, question, today)
    if logged:
        return logged
    for pattern, fn in INTENTS:
        if re.search(pattern, q):
            return {"reply": fn(user, q, today), "intent": fn.__name__}
    return None
