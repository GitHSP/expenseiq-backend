# expenses/accounts.py
#
# Bank accounts built up from uploaded statements:
#   - guess_account: which bank / card / account a statement belongs to
#   - statement_period: the dates a statement covers
#   - coverage: which months each account has statements for
#   - find_recurring: pre-authorized debits, subscriptions and other
#     charges that come out on a regular schedule

import re
from calendar import monthrange
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
from statistics import median

from .pdf_statement import MONTHS

# ─────────────────────────────────────────────
# Which account is this statement for?
# ─────────────────────────────────────────────

# First match wins: Simplii statements mention CIBC, so Simplii goes first.
INSTITUTIONS = [
    ("Simplii",          r"simplii"),
    ("Tangerine",        r"tangerine"),
    ("PC Financial",     r"pc financial|pc (world )?mastercard|president'?s choice"),
    ("Canadian Tire",    r"canadian tire|triangle"),
    ("Rogers Bank",      r"rogers bank|rogers .{0,20}mastercard"),
    ("American Express", r"american express|\bamex\b"),
    ("MBNA",             r"\bmbna\b"),
    ("Capital One",      r"capital ?one"),
    ("Neo Financial",    r"\bneo financial|\bneo card"),
    ("Wealthsimple",     r"wealthsimple"),
    ("EQ Bank",          r"\beq bank"),
    ("KOHO",             r"\bkoho\b"),
    ("Desjardins",       r"desjardins"),
    ("National Bank",    r"national bank|banque nationale"),
    ("HSBC",             r"\bhsbc\b"),
    ("Scotiabank",       r"scotia"),
    ("BMO",              r"\bbmo\b|bank of montreal"),
    ("RBC",              r"\brbc\b|royal bank"),
    ("CIBC",             r"\bcibc\b"),
    ("TD",               r"td canada trust|\btd\b"),
]

CARD_WORDS = re.compile(
    r"credit limit|minimum payment|available credit|credit available|<CCACCTFROM>|annual interest rate|"
    r"payment due date|statement closing date|card statement",
    re.I,
)
# A card network named near the top (not "Visa Debit" in the transactions).
NETWORK_TOP = re.compile(r"(visa|master ?card|american express|amex)(?! debit)", re.I)
TOP = 300   # characters of the first page treated as the letterhead
SAVINGS_WORDS = re.compile(r"savings account|high interest savings|\bhisa\b|<ACCTTYPE>SAVINGS", re.I)
CHEQUING_WORDS = re.compile(r"chequing|checking|<ACCTTYPE>CHECKING|no fee (bank )?account|everyday account", re.I)
NETWORKS = [("Visa", r"\bvisa\b"), ("Mastercard", r"master ?card"), ("Amex", r"american express|\bamex\b")]

LAST4_PATTERNS = [
    re.compile(r"(?:\b\d{4}[ -]?)?(?:[X*•.]{2,}[ -]?){1,3}(\d{4})\b", re.I),             # 4500 XXXX XXXX 1234, ****1234
    re.compile(r"card (?:number|no\.?|ending(?: in)?)\s*[:#]?\s*[\dX* -]*?(\d{4})\b", re.I),
    re.compile(r"account (?:number|no\.?|#)\s*[:#]?\s*([\d][\d -]{3,22}\d)", re.I),
    re.compile(r"<ACCTID>\s*([\w-]{4,})", re.I),
]

KIND_LABEL = {"chequing": "Chequing", "savings": "Savings", "credit_card": "Card", "other": "Account"}


def _last4(text, descriptions=""):
    """Last 4 digits of the account number printed earliest on the page —
    not a card number in a transaction ("TO CARD 4505********0099")."""
    found = []
    for pattern in LAST4_PATTERNS:
        for m in pattern.finditer(text):
            digits = re.sub(r"\D", "", m.group(1))
            if len(digits) >= 4 and m.group(0).strip() not in descriptions:
                found.append((m.start(), digits[-4:]))
                break
    return min(found)[1] if found else ""


def guess_account(filename, info, rows=()):
    """{"institution", "kind", "last4", "name"} for a parsed statement.
    `rows` (its transactions) keep bank names in transactions — an
    e-transfer from Wealthsimple on a Simplii statement — from counting."""
    header = info.get("header_text") or ""
    stem = re.sub(r"\.[a-z0-9]+$", "", filename or "", flags=re.I)
    hay = f"{header} {stem.replace('_', ' ').replace('-', ' ')}"
    descriptions = " | ".join(str(r.get("description", "")) for r in rows)

    # The bank named first on the page is the letterhead; others turn up in
    # transactions ("PAYMENT CIBC VISA" on a TD statement).
    # A bank named more often on the page than in the transactions is the
    # statement's own (letterhead, footer); the rest are just payees.
    found = []
    for i, (name, pattern) in enumerate(INSTITUTIONS):
        hits = list(re.finditer(pattern, hay, re.I))
        if hits and len(hits) > len(re.findall(pattern, descriptions, re.I)):
            found.append((hits[0].start(), i, name))
    if not found:
        found = [(m.start(), i, name) for i, (name, pattern) in enumerate(INSTITUTIONS)
                 for m in [re.search(pattern, hay, re.I)] if m]
    institution = min(found)[2] if found else ""
    top = header[:TOP]
    if CARD_WORDS.search(header) or NETWORK_TOP.search(top) or re.search(r"visa|mastercard|amex|credit ?card", stem, re.I):
        kind = "credit_card"
    elif SAVINGS_WORDS.search(hay) and not CHEQUING_WORDS.search(header):
        kind = "savings"
    elif CHEQUING_WORDS.search(hay) or re.search(r"account number", header, re.I):
        kind = "chequing"
    else:
        kind = "other"
    last4 = _last4(header, descriptions)

    if kind == "credit_card":
        network = next((n for n, p in NETWORKS if re.search(p, f"{top} {stem}", re.I)), "Card")
        label = network if institution != "American Express" else "Card"
    else:
        label = KIND_LABEL[kind]
    if institution:
        name = f"{institution} {label}"
    else:
        name = re.sub(r"\s+", " ", re.sub(r"[_\-]+|\d{4,}", " ", stem)).strip().title()[:60] or "Account"
        if kind != "other" and label.lower() not in name.lower():
            name = f"{name} {label}"
    if last4:
        name = f"{name} •{last4}"
    return {"institution": institution, "kind": kind, "last4": last4, "name": name[:100]}


def match_existing_account(accounts, guess):
    """The user's existing account this guess refers to, if any."""
    for a in accounts:
        if guess["last4"] and a.last4 == guess["last4"] and a.institution == guess["institution"]:
            return a
    for a in accounts:
        if a.name.lower() == guess["name"].lower():
            return a
    # No account number on the statement (common in CSV exports): if there's
    # only one account of that bank and type, it's that one.
    if not guess["last4"] and guess["institution"]:
        same = [a for a in accounts if a.institution == guess["institution"] and a.kind == guess["kind"]]
        if len(same) == 1:
            return same[0]
    return None


# ─────────────────────────────────────────────
# What dates does the statement cover?
# ─────────────────────────────────────────────

_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_TO = r"\s*(?:to|-|–|—|through)\s*"
# "Sep 15 to Oct 14, 2026", "September 15, 2026 - October 14, 2026"
PERIOD_TEXT_RE = re.compile(rf"{_MON}\s+(\d{{1,2}})(?:,?\s+(20\d\d))?{_TO}{_MON}\s+(\d{{1,2}}),?\s+(20\d\d)", re.I)
# TD: "AUG 31/26 - SEP 30/26"
PERIOD_SHORT_RE = re.compile(rf"{_MON}\s*(\d{{1,2}})/(\d{{2}}){_TO}{_MON}\s*(\d{{1,2}})/(\d{{2}})", re.I)
PERIOD_ISO_RE = re.compile(rf"(20\d\d)-(\d{{2}})-(\d{{2}}){_TO}(20\d\d)-(\d{{2}})-(\d{{2}})")
PERIOD_NUM_RE = re.compile(rf"(\d{{1,2}})/(\d{{1,2}})/(20\d\d){_TO}(\d{{1,2}})/(\d{{1,2}})/(20\d\d)")
# MBNA: "StatementPeriod:07/09/26to08/10/26" (two-digit years, spaces missing)
PERIOD_NUM2_RE = re.compile(r"period:?\s*(\d{1,2})/(\d{1,2})/(\d{2})\s*(?:to|-)\s*(\d{1,2})/(\d{1,2})/(\d{2})\b", re.I)


def _month(token):
    return MONTHS[token[:3].lower()]


def statement_period(info, rows):
    """(start, end) the statement covers — the printed period if it fits the
    transactions, otherwise the span of the transactions themselves."""
    dates = [r["date"] for r in rows]
    first, last = min(dates), max(dates)
    header = info.get("header_text") or ""
    candidates = []
    def add(make):
        try:
            start, end = make()
        except (ValueError, KeyError):
            return
        if start > end and start.year == end.year:
            start = date(start.year - 1, start.month, start.day)     # "Dec 15 to Jan 14, 2027"
        candidates.append((start, end))

    for m in PERIOD_TEXT_RE.finditer(header):
        g = m.groups()
        add(lambda: (date(int(g[2] or g[5]), _month(g[0]), int(g[1])), date(int(g[5]), _month(g[3]), int(g[4]))))
    for m in PERIOD_SHORT_RE.finditer(header):
        g = m.groups()
        add(lambda: (date(2000 + int(g[2]), _month(g[0]), int(g[1])), date(2000 + int(g[5]), _month(g[3]), int(g[4]))))
    for m in PERIOD_ISO_RE.finditer(header):
        g = [int(x) for x in m.groups()]
        add(lambda: (date(g[0], g[1], g[2]), date(g[3], g[4], g[5])))
    for m in PERIOD_NUM_RE.finditer(header):
        g = [int(x) for x in m.groups()]
        add(lambda: (date(g[2], g[0], g[1]), date(g[5], g[3], g[4])))
    for m in PERIOD_NUM2_RE.finditer(header):
        g = [int(x) for x in m.groups()]
        add(lambda: (date(2000 + g[2], g[0], g[1]), date(2000 + g[5], g[3], g[4])))
    for start, end in candidates:
        # A real period contains (nearly) all its transactions and spans
        # at most a few months.
        inside = sum(1 for d in dates if start - timedelta(days=3) <= d <= end + timedelta(days=3))
        if inside >= 0.9 * len(dates) and 0 <= (end - start).days <= 120:
            return start, end
    # The latest date printed is usually the closing date — but on a card
    # statement it can be the payment due date weeks later, so only trust
    # it when it's close to the last transaction.
    end = info.get("end")
    if end and last <= end <= last + timedelta(days=7):
        last = end
    return first, last


# ─────────────────────────────────────────────
# Which months are covered?
# ─────────────────────────────────────────────

def _months_between(start, end):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def missing_periods(periods, slack=3):
    """Stretches between consecutive statements that no statement covers
    (e.g. a card statement that wasn't uploaded). `periods` are (start, end)."""
    gaps, covered_to = [], None
    for start, end in sorted(periods):
        if covered_to and (start - covered_to).days > slack:
            gaps.append((covered_to + timedelta(days=1), start - timedelta(days=1)))
        covered_to = max(covered_to, end) if covered_to else end
    return gaps


def coverage(accounts, statements, today=None):
    """
    {"months": ["2026-05", ...], "accounts": {account_id: {"2026-05": 1.0, ...}}}
    The fraction of each month's days covered by that account's statements.
    """
    today = today or date.today()
    if not statements:
        return {"months": [], "accounts": {}}
    start = min(s.period_start for s in statements)
    end = max(max(s.period_end for s in statements), today)
    months = list(_months_between(start, end))
    by_account = defaultdict(list)
    for s in statements:
        by_account[s.account_id].append((s.period_start, s.period_end))

    result = {}
    for a in accounts:
        periods = by_account.get(a.id, [])
        cells = {}
        for y, m in months:
            first, days = date(y, m, 1), monthrange(y, m)[1]
            last = date(y, m, days)
            covered = set()
            for ps, pe in periods:
                lo, hi = max(ps, first), min(pe, last)
                if lo <= hi:
                    covered.update(range(lo.day, hi.day + 1))
            if covered:
                cells[f"{y}-{m:02d}"] = round(len(covered) / days, 2)
        result[a.id] = cells
    return {"months": [f"{y}-{m:02d}" for y, m in months], "accounts": result}


# ─────────────────────────────────────────────
# Recurring charges
# ─────────────────────────────────────────────

# Wording banks use for charges the customer authorized to repeat. ("Bill
# payment" isn't one — that's usually a bill paid by hand in online banking.)
PAD_RE = re.compile(
    r"\bpad\b|\bpap\b|pre-?auth|pre-?authori[sz]ed|preauthori[sz]ed|auto-?pay|automatic payment|"
    r"direct debit|recurring|\bins(urance)? prem|\bmisc(ellaneous)? payment",
    re.I,
)

# Words that say nothing about who's being paid.
KEY_NOISE = re.compile(
    r"\b(pre-?authori[sz]ed|preauthori[sz]ed|pre-?auth|pad|pap|debit|credit|payment|pmt|pymt|purchase|"
    r"pos|visa|interac|retail|recurring|autopay|auto|bill|misc|miscellaneous|internet|online|"
    r"www|com|ca|inc|ltd|corp|the|to|from|on|qc|on|bc|ab|mb|sk|ns|nb|nl|pe)\b",
    re.I,
)

# Everyday spending that only counts as a fixed charge when the amount is
# identical every time (e.g. a meal-kit subscription, not weekly groceries).
EVERYDAY = {"Food & Dining", "Transport", "Shopping", "Clothing & Shopping", "Entertainment", "Other"}
# Bills whose amount naturally varies (hydro, phone overage) but still recur.
BILLS = {"Bills & Utilities", "Insurance", "Housing", "Subscriptions", "Education", "Gym & Fitness"}

FREQUENCIES = [
    # name, min gap, max gap, occurrences per month
    ("weekly",    5,   9,   Decimal(52) / 12),
    ("biweekly",  12,  17,  Decimal(26) / 12),
    ("monthly",   25,  38,  Decimal(1)),
    ("quarterly", 80,  100, Decimal(1) / 3),
    ("yearly",    340, 390, Decimal(1) / 12),
]
FREQ_DAYS = {"weekly": 7, "biweekly": 14, "monthly": 30, "quarterly": 91, "yearly": 365}


def merchant_key(title):
    t = re.sub(r"[^a-z& ]+", " ", title.lower())
    words = [w for w in KEY_NOISE.sub(" ", t).split() if len(w) > 1]
    return " ".join(words[:3])


def _add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def _stable(amounts, typical):
    tolerance = max(typical * Decimal("0.15"), Decimal("2"))
    return sum(1 for a in amounts if abs(a - typical) <= tolerance) >= 0.7 * len(amounts)


def find_recurring(transactions, account_ends, category_of):
    """
    Group money going out by who it's paid to and keep the groups that come
    out on a schedule. `transactions` are StatementTransaction objects,
    `account_ends` maps account id -> latest statement end date (to tell
    whether a charge has stopped), `category_of(tx)` names its category.
    """
    groups = defaultdict(list)
    for tx in transactions:
        if tx.amount >= 0:
            continue
        pad = bool(PAD_RE.search(tx.description))
        # Moving money between your own accounts isn't a charge — unless
        # it's set up to happen automatically (e.g. a card's autopay).
        if tx.kind == "transfer" and not pad:
            continue
        key = merchant_key(tx.title) or merchant_key(tx.description)
        if key:
            groups[key].append((tx, pad))

    items = []
    for key, entries in groups.items():
        entries.sort(key=lambda e: (e[0].date, e[0].id))
        txs = [tx for tx, _ in entries]
        pad = any(p for _, p in entries)
        amounts = [-tx.amount for tx in txs]
        typical = Decimal(median(amounts)).quantize(Decimal("0.01"))
        days = sorted({tx.date for tx in txs})
        gaps = [(b - a).days for a, b in zip(days, days[1:])]
        gap = median(gaps) if gaps else None
        frequency = next((name for name, lo, hi, _ in FREQUENCIES if gap is not None and lo <= gap <= hi), None)
        months = {(tx.date.year, tx.date.month) for tx in txs}
        category = Counter(category_of(tx) for tx in txs).most_common(1)[0][0]
        exact = len(set(amounts)) == 1

        if frequency:
            enough = len(days) >= (3 if frequency in ("weekly", "biweekly") else 2)
            if frequency == "monthly":
                enough = enough and len(months) >= 2
            if category in EVERYDAY and not pad:
                ok = enough and exact and frequency == "monthly"
            else:
                ok = enough and (_stable(amounts, typical) or pad or (category in BILLS and len(days) >= 3))
        else:
            ok = pad          # authorized to repeat, even if seen only once so far
        if not ok:
            continue

        last = txs[-1]
        latest_end = max((account_ends.get(tx.account_id) for tx in txs if account_ends.get(tx.account_id)), default=None)
        next_date, status = None, "active"
        if frequency == "monthly":
            next_date = _add_months(last.date, 1)
        elif frequency:
            next_date = last.date + timedelta(days=round(gap))
        if next_date and latest_end and next_date + timedelta(days=7) < latest_end:
            status = "stopped"          # the statements run past when it was due
        per_month = next((f for name, _, _, f in FREQUENCIES if name == frequency), None)
        monthly_amount = (typical * per_month).quantize(Decimal("0.01")) if per_month and status == "active" else None

        names = Counter(tx.title for tx in txs)
        items.append({
            "key":            key,
            "name":           names.most_common(1)[0][0],
            "category":       category,
            "pre_authorized": pad,
            # e.g. a card's autopay from chequing: the card's purchases are
            # the spending, so this isn't added to the monthly total.
            "own_transfer":   all(tx.kind == "transfer" for tx in txs),
            "frequency":      frequency or "once",
            "typical_amount": str(typical),
            "monthly_amount": str(monthly_amount) if monthly_amount is not None else None,
            "varies":         not exact,
            "count":          len(txs),
            "first_date":     txs[0].date.isoformat(),
            "last_date":      last.date.isoformat(),
            "last_amount":    str(-last.amount),
            "next_date":      next_date.isoformat() if next_date and status == "active" else None,
            "day_of_month":   round(median(tx.date.day for tx in txs)),
            "status":         status,
            "account_ids":    sorted({tx.account_id for tx in txs}),
            "occurrences":    [{"id": tx.id, "date": tx.date.isoformat(), "amount": str(-tx.amount),
                                "account_id": tx.account_id} for tx in txs],
        })
    items.sort(key=lambda i: (i["status"] != "active", -(Decimal(i["monthly_amount"] or 0)), i["name"].lower()))
    return items


# ─────────────────────────────────────────────
# Statement summary (balances, minimum payment, due date)
# ─────────────────────────────────────────────

_AMT = r"(-?\$?\s?[\d,]+\.\d\d-?)"
# First match wins. Spaces are optional because some PDFs drop them
# ("YourNew Balance", "Previousstatementbalance").
OPENING_RES = [re.compile(p + _AMT, re.I) for p in (
    r"previous\s?(?:account\s?|statement\s?)?balance[^$]{0,30}?",
    r"opening\s?balance[^$\d]{0,40}", r"balance\s?forward[^$\d]{0,30}", r"starting\s?balance[^$\d]{0,30}",
)]
CLOSING_RES = [re.compile(p + _AMT, re.I) for p in (
    r"new\s?balance[^$\d]{0,30}", r"total\s?account\s?balance[^$\d]{0,20}", r"total\s?balance[^$\d]{0,20}",
    r"closing\s?balance[^$]{0,40}?", r"ending\s?balance[^$]{0,30}?", r"account\s?balance\s?=\s?",
)]
MINIMUM_RES = [re.compile(p + _AMT, re.I) for p in (
    r"total\s?minimum\s?payment[^$]{0,40}?",                    # CIBC "Total Minimum Payment due by Oct 13, 2026 = $"
    r"minimum\s?(?:amount\s?)?(?:payment|due)\d?[^$\d]{0,25}",   # "Minimum Payment2 $66.61" (footnote 2)
)]
LIMIT_RE = re.compile(r"(?<!over )(?<!your )credit\s?limit[^$\d]{0,10}" + _AMT, re.I)   # not "amount over (your) credit limit $..."
DUE_RES = [re.compile(p + rf"{_MON}\s?(\d{{1,2}}),?\s?(20\d\d)", re.I) for p in (
    r"payment\s?due\s?date:?\s*", r"minimum\s?payment\s?due\s?by\s*", r"pay\s?this\s?amount\s?by\s*",
)]


def _amount(text):
    t = text.replace(",", "").replace("$", "").replace(" ", "")
    negative = t.startswith("-") or t.endswith("-")
    value = Decimal(t.strip("-"))
    return -value if negative else value


def statement_summary(info, rows, kind):
    """Balances and payment details printed on a statement, as far as they
    can be found. For a card, a balance that the transactions don't add up
    to is dropped rather than trusted."""
    text = info.get("header_text") or ""
    first = lambda patterns: next((_amount(m.group(1)) for p in patterns for m in [p.search(text)] if m), None)
    previous, new = first(OPENING_RES), first(CLOSING_RES)
    minimum, limit = first(MINIMUM_RES), first([LIMIT_RE])
    due = None
    m = next((m for p in DUE_RES for m in [p.search(text)] if m), None)
    if m:
        try:
            due = date(int(m.group(3)), _month(m.group(1)), int(m.group(2)))
        except (ValueError, KeyError):
            pass
    net = sum((r["amount"] for r in rows), Decimal("0"))       # + = money in to the holder
    if previous is not None and new is not None:
        expected = previous - net if kind == "credit_card" else previous + net
        if expected != new:
            previous = None            # mismatched: keep only the printed closing balance
    if kind != "credit_card":
        minimum = limit = due = None
    return {"previous_balance": previous, "new_balance": new, "minimum_payment": minimum,
            "payment_due": due, "credit_limit": limit}


# ─────────────────────────────────────────────
# Which Planner debt is this card?
# ─────────────────────────────────────────────

BANK_WORDS = {
    "Scotiabank": ("scotia",), "American Express": ("amex", "american express"), "PC Financial": ("pc ",),
    "Canadian Tire": ("triangle", "canadian tire"), "Rogers Bank": ("rogers",), "Capital One": ("capital one",),
}
NETWORK_WORDS = {"visa": ("visa",), "mastercard": ("master", " mc"), "amex": ("amex",)}


def match_debt(account, debts):
    """The user's debt that's most likely this card, or None if unclear."""
    bank = BANK_WORDS.get(account.institution, (account.institution.lower(),)) if account.institution else ()
    name = account.name.lower()
    network = next((n for n, words in NETWORK_WORDS.items() if any(w.strip() in name for w in words)), None)
    scored = []
    for d in debts:
        dn = f" {d.name.lower()} "
        if not bank or not any(b in dn for b in bank):
            continue
        score = 1
        if account.last4 and account.last4 in dn:
            score += 3
        if network and any(w in dn for w in NETWORK_WORDS[network]):
            score += 2
        elif network and any(w in dn for ws in NETWORK_WORDS.values() for w in ws):
            score -= 2                  # names a different card network
        scored.append((score, d))
    if not scored:
        return None
    scored.sort(key=lambda s: -s[0])
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1] if scored[0][0] > 0 else None
