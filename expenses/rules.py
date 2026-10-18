# expenses/rules.py
#
# The user's tidy-up rules ("anything mentioning Tim Hortons -> Coffee",
# "Wealthsimple is my own account"): applied to what's already imported and
# to statement previews, so future imports follow them too.

from django.db import transaction
from django.db.models import Q

from activity.context import activity_context

from .models import Expense, Income, MerchantRule, StatementTransaction


def rule_for(rules, *texts):
    """The rule that applies to a transaction (longest match wins)."""
    hay = " ".join(str(t or "") for t in texts).lower()
    hits = [r for r in rules if r.match.lower() in hay]
    return max(hits, key=lambda r: len(r.match)) if hits else None


def apply_to_preview(user, preview):
    """Statement preview rows follow the user's rules."""
    rules = list(MerchantRule.objects.filter(user=user))
    if not rules:
        return preview
    for row in preview:
        rule = rule_for(rules, row["raw_description"], row["title"])
        if not rule:
            continue
        row["rule"] = rule.match
        if rule.rename_to:
            row["title"] = rule.rename_to
        if rule.category:
            row["expense_category"] = rule.category
        if rule.treat_as == "transfer":
            row["is_transfer"] = True
        elif rule.treat_as == "spending":
            row["is_transfer"] = False
            row["transfer_match"] = None
    return preview


def apply_rule(user, rule):
    """Apply one rule to everything already imported. Returns counts.
    'Own money' removes expenses/income that were saved from matching
    statement rows (they weren't really spending or income)."""
    text = Q(description__icontains=rule.match) | Q(title__icontains=rule.match)
    rows = StatementTransaction.objects.filter(user=user).filter(text).select_related("expense", "income")
    counts = {"statement_rows": 0, "entries_updated": 0, "entries_removed": 0}
    with transaction.atomic(), activity_context(source="assistant", quiet={"expense", "income"}):
        for tx in rows:
            changes = {}
            if rule.rename_to and tx.title != rule.rename_to:
                changes["title"] = rule.rename_to
            if rule.category and tx.category != rule.category:
                changes["category"] = rule.category
            if rule.treat_as == "transfer":
                for entry in (tx.expense, tx.income):
                    if entry is not None:
                        entry.delete()
                        counts["entries_removed"] += 1
                changes.update(kind="transfer", expense=None, income=None)
            elif rule.treat_as == "spending" and tx.kind == "transfer":
                changes["kind"] = "skipped"
            if rule.treat_as != "transfer":
                for entry in (tx.expense, tx.income):
                    if entry is None:
                        continue
                    fields = []
                    if rule.rename_to and entry.title != rule.rename_to:
                        entry.title = rule.rename_to
                        fields.append("title")
                    if rule.category in [c[0] for c in type(entry).CATEGORIES] and entry.category != rule.category:
                        entry.category = rule.category
                        fields.append("category")
                    if fields:
                        entry.save(update_fields=fields)
                        counts["entries_updated"] += 1
            if changes:
                for field, value in changes.items():
                    setattr(tx, field, value)
                tx.save(update_fields=list(changes))
                counts["statement_rows"] += 1
        # Entries (from statements or typed in) with matching titles.
        if rule.treat_as != "transfer" and (rule.rename_to or rule.category):
            for model in (Expense, Income):
                for entry in model.objects.filter(user=user, title__icontains=rule.match):
                    fields = []
                    if rule.rename_to and entry.title != rule.rename_to:
                        entry.title = rule.rename_to
                        fields.append("title")
                    valid = [c[0] for c in model.CATEGORIES]
                    if rule.category in valid and entry.category != rule.category:
                        entry.category = rule.category
                        fields.append("category")
                    if fields:
                        entry.save(update_fields=fields)
                        counts["entries_updated"] += 1
    return counts
