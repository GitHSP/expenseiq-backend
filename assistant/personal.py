# assistant/personal.py
#
# Things the user sets up by talking to Aurora: tidy-up rules, budgets and
# memories. Each change is confirmed back in plain words.

from decimal import Decimal, InvalidOperation

from expenses.models import Expense, Income, MerchantRule, SpendingBudget
from expenses.rules import apply_rule

from .insights import THEMES
from .models import Memory

MAX_MEMORIES = 40


# ── Rules ──

def add_rule(user, match, rename_to="", category="", treat_as="", my_share="", **kwargs):
    from expenses.rules import parse_share
    match = str(match or "").strip()[:100]
    try:
        share_amount, share_percent = parse_share(my_share) if treat_as == "bill" else (None, None)
    except (ValueError, ArithmeticError) as e:
        return {"ok": False, "message": str(e) if isinstance(e, ValueError) and str(e) else
                "My share must be an amount like 625 or a percent like 25%."}
    if len(match) < 3:
        return {"ok": False, "message": "Give at least 3 characters of the merchant/description to match."}
    if treat_as not in ("", "transfer", "spending", "shared", "bill"):
        return {"ok": False, "message": "treat_as must be 'transfer', 'spending', 'shared', 'bill' or empty."}
    valid = {c[0] for c in Expense.CATEGORIES} | {c[0] for c in Income.CATEGORIES}
    if category and category not in valid:
        return {"ok": False, "message": f"'{category}' isn't a category. Use one of: {', '.join(sorted(valid))}."}
    if not (rename_to or category or treat_as):
        return {"ok": False, "message": "Say what the rule should do: rename, set a category, or mark as own-account transfers."}
    rule, created = MerchantRule.objects.update_or_create(
        user=user, match__iexact=match,
        defaults={"match": match, "rename_to": str(rename_to or "")[:120], "category": category or "", "treat_as": treat_as or "",
                  "share_amount": share_amount, "share_percent": share_percent},
    )
    counts = apply_rule(user, rule)
    parts = [p for p in (f"renamed to '{rule.rename_to}'" if rule.rename_to else "",
                         f"category {rule.category}" if rule.category else "",
                         "treated as money moving between your own accounts" if rule.treat_as == "transfer" else
                         "counted as real spending/income" if rule.treat_as == "spending" else
                         "their share of shared bills (not income; it lowers your spending)" if rule.treat_as == "shared" else
                         ("a shared bill you pay in full (roommates pay you back)"
                          + (f"; your share {my_share} is what counts as your expense" if my_share else ""))
                         if rule.treat_as == "bill" else "") if p]
    return {"ok": True, "created": created,
            "message": f"Rule for anything mentioning '{rule.match}': {', '.join(parts)}. "
                       f"Applied to {counts['statement_rows']} statement rows; {counts['entries_updated']} entries updated"
                       + (f", {counts['entries_removed']} expense/income entries removed (they were your own money moving)"
                          if counts["entries_removed"] else "") + ". Future imports follow it too.",
            **counts}


def list_rules(user, **kwargs):
    return {"ok": True, "rules": [{"match": r.match, "rename_to": r.rename_to, "category": r.category, "treat_as": r.treat_as}
                                  for r in MerchantRule.objects.filter(user=user)]}


def delete_rule(user, match, **kwargs):
    rules = MerchantRule.objects.filter(user=user, match__icontains=str(match or "").strip())
    if rules.count() != 1:
        return {"ok": False, "message": f"Found {rules.count()} rules matching '{match}'. Be more specific."}
    rule = rules.first()
    name = rule.match
    rule.delete()
    return {"ok": True, "message": f"Deleted the rule for '{name}'. Entries it already changed stay as they are; future imports won't use it."}


# ── Budgets ──

def set_budget(user, label, monthly_limit, category="", theme="", query="", **kwargs):
    try:
        limit = Decimal(str(monthly_limit)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return {"ok": False, "message": f"'{monthly_limit}' isn't an amount."}
    if limit <= 0:
        return {"ok": False, "message": "The limit has to be more than zero."}
    if theme and theme not in THEMES:
        return {"ok": False, "message": f"Unknown theme. Themes: {', '.join(THEMES)}."}
    if not (category or theme or query):
        return {"ok": False, "message": "Say what the budget covers: a category, a theme (e.g. coffee) or a merchant."}
    label = str(label or theme or category or query).strip()[:60]
    budget, created = SpendingBudget.objects.update_or_create(
        user=user, label__iexact=label,
        defaults={"label": label, "monthly_limit": limit, "category": category or "", "theme": theme or "",
                  "query": str(query or "")[:80]},
    )
    return {"ok": True, "message": f"{'Set' if created else 'Updated'} a ${limit}/month budget for {budget.label}."}


def list_budgets(user, month=None, **kwargs):
    from .advisor import budget_status
    return budget_status(user, month=month)


def delete_budget(user, label, **kwargs):
    budgets = SpendingBudget.objects.filter(user=user, label__icontains=str(label or "").strip())
    if budgets.count() != 1:
        return {"ok": False, "message": f"Found {budgets.count()} budgets matching '{label}'. Be more specific."}
    name = budgets.first().label
    budgets.delete()
    return {"ok": True, "message": f"Deleted the {name} budget."}


# ── Memory ──

def remember(user, fact, **kwargs):
    fact = " ".join(str(fact or "").split())[:300]
    if len(fact) < 3:
        return {"ok": False, "message": "Nothing to remember."}
    if Memory.objects.filter(user=user, text__iexact=fact).exists():
        return {"ok": True, "message": "Already noted."}
    if Memory.objects.filter(user=user).count() >= MAX_MEMORIES:
        return {"ok": False, "message": f"I can keep up to {MAX_MEMORIES} notes. Ask me to forget some first."}
    Memory.objects.create(user=user, text=fact)
    return {"ok": True, "message": f"Noted: {fact}"}


def forget(user, fact, **kwargs):
    notes = Memory.objects.filter(user=user, text__icontains=str(fact or "").strip())
    if not notes.exists():
        return {"ok": False, "message": f"I don't have a note about '{fact}'."}
    texts = list(notes.values_list("text", flat=True))
    notes.delete()
    return {"ok": True, "message": "Forgot: " + "; ".join(texts)}


def memory_lines(user):
    """Notes for the start of every chat."""
    return [m.text for m in Memory.objects.filter(user=user)[:MAX_MEMORIES]]
