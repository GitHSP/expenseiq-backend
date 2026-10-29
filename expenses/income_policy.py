# expenses/income_policy.py
#
# Which deposits on a bank statement count as income on their own: pay,
# government benefits and ATM/cash deposits. Anything else coming in (money
# from friends, the user's own transfers, apps) isn't income unless the user
# adds it by hand, ticks it in the import preview, or has a rule for it.
# Refunds and purchase reversals lower spending instead.

import re

ATM_RE = re.compile(r"\b(?:ATM|ABM)\b.*\bDEP|\bDEP\w*\b.*\b(?:ATM|ABM)\b|\bCASH DEP|BRANCH DEP", re.I)
PAY_RE = re.compile(r"\bPAYROLL\b|\bPAY ?DEP|\bDIRECT DEP|\bSALARY\b|\bPAYCHEQUE\b|\bPAYCHECK\b|\bWAGES?\b", re.I)
GOV_RE = re.compile(
    r"SERVICE CANADA|CANADA PRO\b|\bGOVT?\b.*\bCANADA\b|^\W*DEPOSIT\s+CANADA\b|"
    r"\bGST\b|\bHST\b|\bTPS\b|\bTVH\b|ALLOC\w*\.?\s*TRAVAIL|CANADA WORKERS|\bCWB\b|\bCCB\b|CHILD BENEFIT|"
    r"ALLOCATION CANADIENNE|EMPLOYMENT INS|\bEI\b|\bCPP\b|\bRPC\b|\bOAS\b|\bSV\b|ONTARIO TRILLIUM|\bOTB\b|"
    r"CLIMATE ACTION|\bCAIP\b|CARBON REBATE|\bCRA\b|REVENUE CANADA|TAX REFUND|INCOME TAX|"
    r"\bBENEFITS?\b|PRESTATION|ESSENTIALS|ESSENTIELS", re.I)
# A direct deposit from a company (not an e-transfer): almost always pay.
EMPLOYER_RE = re.compile(r"^(?!.*\bE-?\s?TRANSFER\b)(?!.*\bINTERAC\b).*\b(?:LTD|INC|CORP|CORPORATION|LLC|LIMITED|LTEE|ULC)\b", re.I)
# Words in a recurring income's name that don't identify who pays it.
LABEL_STOP = {"salary", "income", "wage", "wages", "tips", "tip", "pay", "payment", "monthly", "weekly",
              "biweekly", "hours", "hrs", "cleaning", "part", "time", "full", "job", "work", "bonus",
              "other", "extra", "side", "main", "estimate", "from", "with", "the", "and"}
REFUND_RE = re.compile(r"REVERSAL|REFUND|\bRETURN\b|REMBOURSEMENT|CREDIT VOUCHER|\bRETOUR\b", re.I)


def income_words(user):
    """The user's own pay sources: the words on a recurring income's
    "Use my average pay", the payer's name in a recurring income's label
    ("Salary ACME (40 hrs)" -> "acme") and any "real spending / income" rule."""
    from financial_planner.models import RecurringItem
    from .models import MerchantRule
    incomes = RecurringItem.objects.filter(user=user, category="income")
    return words_from(incomes.values_list("average_from", "label"),
                      MerchantRule.objects.filter(user=user, treat_as="spending").values_list("match", flat=True))


def words_from(income_rows, rule_matches):
    words = []
    for average_from, label in income_rows:
        if average_from:
            words.append(average_from)
        words += [t for t in re.findall(r"[A-Za-z]{4,}", label or "") if t.lower() not in LABEL_STOP]
    words += list(rule_matches)
    return sorted({w.strip().lower() for w in words if len(w.strip()) >= 3})


def deposit_class(text, words=()):
    """'income' (pay, benefits, ATM), 'refund', or None (not income on its own)."""
    text = str(text or "")
    low = text.lower()
    if (any(re.search(rf"\b{re.escape(w)}\b", low) for w in words) or ATM_RE.search(text)
            or PAY_RE.search(text) or GOV_RE.search(text) or EMPLOYER_RE.search(text)):
        return "income"
    if REFUND_RE.search(text):
        return "refund"
    return None


def counts_as_income(kind, text, words=()):
    """A deposit on a bank account counts as income when it was saved as
    income, or is pay / a benefit / an ATM deposit whatever happened to it
    on import (already recorded by hand, or not saved)."""
    return kind == "income" or (kind in ("duplicate", "skipped") and deposit_class(text, words) == "income")


# Interest and fees charged by a bank or card: not expenses (the user only
# counts what they spent on things). They still show on the card and its debt.
CHARGE_RE = re.compile(
    r"\binterest|\bint charges?\b|\bnsf\b|\boverdraft\b|service charge|monthly fee|annual fee|"
    r"late (?:payment )?fee|over ?limit fee|foreign transaction fee|cash advance fee|record-?keeping|"
    r"\bfees?\b|\bfrais\b", re.I)


def is_charge(text):
    # (older imports have a card's "PAYMENTS & INTEREST" heading glued on)
    text = re.sub(r"\bpayments\s*&\s*interest\b", "", str(text or ""), flags=re.I)
    return bool(CHARGE_RE.search(text))
