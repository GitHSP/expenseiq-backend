# assistant/insights.py
#
# Read-only tools that let Aurora answer questions about any of the user's
# money: "how much did I spend on coffee, and on which card?", "what did I
# pay Uber in September?", "how much do I owe on my cards?".
#
# The source is every transaction on the user's uploaded statements (each
# tied to its bank account or card), plus expenses/income entered by hand
# or imported before statements were kept.

import re
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal

from django.db.models import Q

from expenses.accounts import merchant_key
from expenses.models import Account, Expense, Income, StatementTransaction

# Everyday themes people ask about, matched against the merchant/description.
THEMES = {
    "coffee": r"coffee|caf[eé]|espresso|starbucks|tim hortons?|tims\b|second cup|timothy|balzac|blue bottle|"
              r"pilot coffee|dineen|aroma espresso|coffee time|country style|nespresso|tea|bubble tea|chatime|"
              r"david'?s tea|pastr(y|ies)|bakery",
    "groceries": r"loblaws|no ?frills|sobeys|\bmetro\b|food basics|freshco|superstore|safeway|save-?on|farm boy|"
                 r"t&t|longo|walmart|costco|grocer|fortinos|zehrs|\biga\b|foodland|whole foods|mart\b|supermarket|"
                 r"food ?mart|shoppers drug|dollarama",
    "restaurants": r"restaurant|grill|pizza|burger|sushi|ramen|pho|bistro|kitchen|diner|\bpub\b|bar\b|tavern|"
                   r"mcdonald|wendy|a&w|kfc|popeyes|subway|chipotle|harvey|mary brown|domino|pizza pizza|"
                   r"shawarma|thai|indian|cuisine|bbq|taco|noodle",
    "food delivery": r"uber ?eats|doordash|skip ?the ?dishes|skipthedishes|instacart|foodora",
    "rideshare": r"\buber\b(?!\s?eats)|lyft",
    "transit": r"presto|\bttc\b|go transit|translink|compass|oc transpo|\bstm\b|via rail|up express|ferry",
    "gas": r"petro|esso|shell|chevron|husky|pioneer|ultramar|fas gas|\bgas\b|fuel",
    "parking": r"parking|impark|green ?p|indigo",
    "alcohol": r"lcbo|beer store|\bbeer\b|wine|liquor|spirits|brewery|saq\b|bc liquor",
    "subscriptions": r"netflix|spotify|disney|apple\.com|itunes|youtube|prime|crave|icloud|adobe|openai|chatgpt|"
                     r"claude|anthropic|patreon|audible|microsoft|xbox|playstation|google \*|dropbox|"
                     r"subscription|membership",
    "shopping": r"amazon|amzn|best buy|winners|marshalls|homesense|ikea|canadian tire|home depot|lowe'?s|"
                r"walmart|costco|indigo|uniqlo|h&m|zara|sport chek|aritzia|apple store|etsy|ebay|aliexpress|temu|shein",
    "phone & internet": r"rogers|\bbell\b|telus|fido|koodo|virgin|freedom mobile|chatr|public mobile|lucky mobile|"
                        r"internet|teksavvy",
    "utilities": r"hydro|enbridge|alectra|toronto water|utilit|electric",
    "rent & housing": r"\brent\b|landlord|property management|realstar|capreit|mortgage|condo fee",
    "insurance": r"insurance|intact|manulife|sun life|belair|aviva|desjardins ins|wawanesa|sonnet|square one",
    "fitness": r"goodlife|fit4less|planet fitness|la fitness|anytime fitness|orangetheory|\bgym\b|ymca|crossfit|yoga",
    "travel": r"air canada|westjet|porter|flair|swoop|airbnb|expedia|booking\.com|hotel|marriott|hilton|"
              r"airline|airport|travel",
    "health": r"pharmacy|shoppers drug|rexall|dental|dentist|clinic|physio|optical|medical",
    "entertainment": r"cineplex|cinema|theatre|theater|ticketmaster|concert|steam|game|bowling|museum",
    "interest & fees": r"interest|\bfee\b|fees|nsf|overdraft|service charge|annual fee",
    "cash": r"\batm\b|cash withdrawal|cash advance",
    "transfers & e-transfers": r"e-?transfer|interac|transfer",
}


# Payment-processor prefixes and place names that don't identify the merchant.
PROCESSOR_RE = re.compile(r"^(sq|tst|sp|pp|py|paypal|ws|wl|dd|ub|fs|ckp|sumup|zettle|clover)\s*\*\s*", re.I)
PLACE_RE = re.compile(
    r"(toronto|mississauga|brampton|markham|vaughan|scarborough|etobicoke|north york|ottawa|montreal|vancouver|"
    r"calgary|edmonton|morley|winnipeg|halifax|on|ab|bc|qc|mb|sk|ns|nb|nl|pe|ca|can|canada|usa|us)\.?", re.I)


def merchant_group(title):
    """'Tim Hortons Toronto On', 'TIM HORTONS #24' -> 'tim hortons'."""
    t = PROCESSOR_RE.sub("", str(title or ""))
    t = re.sub(r"\(.*?\)|#\s*\d+|\d+", " ", t)
    t = PLACE_RE.sub(" ", t)
    words = [w for w in re.sub(r"[^a-z&' ]+", " ", t.lower()).split() if len(w) > 1]
    key = " ".join(words[:2]) or merchant_key(title) or str(title).lower()
    return next((name for prefix, name in ALIASES if key.startswith(prefix)), key)


ALIASES = [("amzn", "amazon"), ("amazon", "amazon"), ("tim horton", "tim hortons"), ("tims", "tim hortons"),
           ("mcdonald", "mcdonald's"), ("starbucks", "starbucks"), ("uber eats", "uber eats"), ("ubereats", "uber eats"),
           ("uber", "uber"), ("apple com", "apple"), ("lcbo", "lcbo"), ("presto", "presto")]


def _date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _money(v):
    return str(Decimal(v).quantize(Decimal("0.01")))


def _rows(user, date_from, date_to):
    """Every transaction the user has, as dicts: statement rows first, then
    expenses/income that aren't on a statement."""
    qs = StatementTransaction.objects.filter(user=user).select_related("account", "expense", "income")
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    out, linked_e, linked_i = [], set(), set()
    for t in qs:
        if t.expense_id:
            linked_e.add(t.expense_id)
        if t.income_id:
            linked_i.add(t.income_id)
        category = (t.expense.category if t.expense else t.income.category if t.income else t.category) or ""
        out.append({
            "date": t.date, "title": (t.expense.title if t.expense else t.income.title if t.income else t.title),
            "description": t.description, "amount": t.amount, "account": t.account.name,
            "account_kind": t.account.kind, "category": category, "transfer": t.kind == "transfer",
        })
    for model, sign, linked in ((Expense, -1, linked_e), (Income, 1, linked_i)):
        qs = model.objects.filter(user=user).exclude(id__in=linked)
        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)
        for e in qs:
            out.append({
                "date": e.date, "title": e.title, "description": e.notes or "", "amount": sign * e.amount,
                "account": "Entered in Glacier (no statement)", "account_kind": "", "category": e.category,
                "transfer": False,
            })
    return out


def search_transactions(user, query=None, theme=None, category=None, account=None, date_from=None, date_to=None,
                        direction="spending", include_transfers=False, limit=40, **kwargs):
    d_from, d_to = _date(date_from), _date(date_to)
    patterns = []
    if theme:
        key = next((k for k in THEMES if k.startswith(str(theme).lower().strip()) or str(theme).lower() in k), None)
        if not key:
            return {"ok": False, "message": f"Unknown theme '{theme}'. Themes: {', '.join(THEMES)}. Or search with `query`."}
        patterns.append(re.compile(THEMES[key], re.I))
        if key == "transfers & e-transfers":
            include_transfers = True
    if query:
        words = [re.escape(w) for w in str(query).split() if w.strip()]
        patterns.append(re.compile(r".*".join(words), re.I))

    rows = []
    for r in _rows(user, d_from, d_to):
        if r["transfer"] and not include_transfers:
            continue
        if direction == "spending" and r["amount"] >= 0 and not (r["account_kind"] == "credit_card" and patterns):
            continue                        # card refunds for the same merchant are kept to net them off
        if direction == "income" and r["amount"] <= 0:
            continue
        if direction == "income" and r["account_kind"] == "credit_card":
            continue
        text = f"{r['title']} {r['description']}"
        if any(not p.search(text) for p in patterns):
            continue
        if category and str(category).lower() not in r["category"].lower():
            continue
        if account and str(account).lower().replace("•", "") not in r["account"].lower().replace("•", ""):
            continue
        rows.append(r)

    if not rows:
        return {"ok": True, "count": 0, "message": "No matching transactions in the uploaded statements or entries.",
                "searched": {"from": str(d_from or "all"), "to": str(d_to or "all")}}

    rows.sort(key=lambda r: r["date"], reverse=True)
    spent = sum((-r["amount"] for r in rows if r["amount"] < 0), Decimal("0"))
    received = sum((r["amount"] for r in rows if r["amount"] > 0), Decimal("0"))
    by_account, by_month, by_category = defaultdict(lambda: [Decimal("0"), 0]), defaultdict(Decimal), defaultdict(Decimal)
    merchants, merchant_names = defaultdict(lambda: [Decimal("0"), 0]), defaultdict(Counter)
    for r in rows:
        value = -r["amount"] if direction != "income" else r["amount"]
        by_account[r["account"]][0] += value
        by_account[r["account"]][1] += 1
        by_month[f"{r['date']:%Y-%m}"] += value
        by_category[r["category"] or "Uncategorised"] += value
        key = merchant_group(r["title"])
        merchants[key][0] += value
        merchants[key][1] += 1
        merchant_names[key][r["title"]] += 1

    try:
        limit = max(1, min(int(limit or 40), 100))
    except (TypeError, ValueError):
        limit = 40
    return {
        "ok": True,
        "count": len(rows),
        "period": {"from": str(min(r["date"] for r in rows)), "to": str(max(r["date"] for r in rows))},
        "total_spent": _money(spent),
        "total_refunds_or_income": _money(received),
        "net": _money(spent - received) if direction != "income" else _money(received - spent),
        "by_account": [{"account": k, "total": _money(v[0]), "count": v[1]}
                       for k, v in sorted(by_account.items(), key=lambda kv: -kv[1][0])],
        "by_merchant": [{"merchant": min(merchant_names[k], key=len), "total": _money(v[0]), "count": v[1]}
                        for k, v in sorted(merchants.items(), key=lambda kv: -kv[1][0])[:15]],
        "by_month": [{"month": k, "total": _money(v)} for k, v in sorted(by_month.items())],
        "by_category": [{"category": k, "total": _money(v)} for k, v in sorted(by_category.items(), key=lambda kv: -kv[1])],
        "transactions": [{"date": str(r["date"]), "title": r["title"], "amount": _money(r["amount"]),
                          "account": r["account"], "category": r["category"]} for r in rows[:limit]],
        "transactions_shown": min(limit, len(rows)),
    }


def get_accounts_overview(user, **kwargs):
    from expenses.account_views import _card
    from expenses.accounts import find_recurring
    from expenses.statement_import import EXPENSE_RULES, _match
    from django.db.models import Max, Min, Count

    accounts = []
    for a in Account.objects.filter(user=user).select_related("debt").annotate(
            statements_count=Count("statements", distinct=True),
            first=Min("statements__period_start"), last=Max("statements__period_end")):
        item = {"name": a.name, "kind": a.get_kind_display(), "statements": a.statements_count,
                "covers": f"{a.first} to {a.last}" if a.first else None}
        if a.kind == "credit_card":
            card = _card(a)
            item.update({
                "statement_balance": card["statement_balance"], "statement_date": card["statement_end"],
                "paid_since_statement": card["paid_since"], "estimated_owed_now": card["estimated_balance"],
                "minimum_payment": card["minimum_payment"], "payment_due": card["payment_due"],
                "credit_limit": card["credit_limit"],
                "planner_debt": a.debt.name if a.debt else None,
                "history": [{k: h[k] for k in ("period_end", "paid", "spent", "interest", "new_balance")}
                            for h in card["history"][-6:]],
            })
        accounts.append(item)

    txs = list(StatementTransaction.objects.filter(user=user, amount__lt=0))
    ends = dict(Account.objects.filter(user=user).annotate(end=Max("statements__period_end")).values_list("id", "end"))
    names = dict(Account.objects.filter(user=user).values_list("id", "name"))
    recurring = [
        {"name": i["name"], "frequency": i["frequency"], "typical_amount": i["typical_amount"],
         "pre_authorized": i["pre_authorized"], "status": i["status"], "next_date": i["next_date"],
         "accounts": [names.get(x) for x in i["account_ids"]], "card_payment": i["own_transfer"]}
        for i in find_recurring(txs, ends, lambda t: t.category or _match(EXPENSE_RULES, t.description, "Other"))
    ]
    return {"ok": True, "accounts": accounts, "recurring_charges": recurring,
            "note": "Card balances are as of each card's latest statement; 'estimated_owed_now' subtracts "
                    "payments found on later bank statements (new purchases appear on the next statement)."}
