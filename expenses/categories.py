"""
The user's expense categories: the built-in ones they haven't hidden, plus any
they've added. Removing a category moves everything in it (expenses, statement
rows, rules, limits) to another one first; a built-in is then hidden, an added
one deleted. "Other" always stays.
"""
import re

from django.db import transaction

from .models import Expense, MerchantRule, SpendingBudget, StatementTransaction, UserCategory

BUILTIN = [c[0] for c in Expense.CATEGORIES]
KEEP = "Other"
ICONS = {"utensils", "car", "shopping-bag", "film", "pill", "house", "book", "dumbbell", "smartphone", "shield",
         "shirt", "sparkles", "gift", "zap", "hospital", "graduation-cap", "plane", "package", "wine", "cigarette",
         "send", "hand-coins", "washing-machine", "file-text", "coins", "briefcase", "laptop", "target",
         "piggy-bank", "calendar", "credit-card", "banknote"}
COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")


def usable(user):
    """Names the user can file an expense under."""
    hidden = set(UserCategory.objects.filter(user=user, hidden=True).values_list("name", flat=True))
    custom = list(UserCategory.objects.filter(user=user, is_custom=True, hidden=False).values_list("name", flat=True))
    return [c for c in BUILTIN if c not in hidden] + custom


def is_valid(user, name):
    """Any built-in (even hidden: old entries keep theirs) or one of the user's own."""
    return name in BUILTIN or UserCategory.objects.filter(user=user, name=name, is_custom=True).exists()


def listing(user):
    from django.db.models import Count
    counts = dict(Expense.objects.filter(user=user).values_list("category").annotate(n=Count("id")))
    rows = {r.name: r for r in UserCategory.objects.filter(user=user)}
    out = []
    for name in BUILTIN:
        r = rows.get(name)
        out.append({"name": name, "builtin": True, "hidden": bool(r and r.hidden), "icon": None, "color": None,
                    "count": counts.get(name, 0)})
    for r in sorted((r for r in rows.values() if r.is_custom), key=lambda r: r.name.lower()):
        out.append({"name": r.name, "builtin": False, "hidden": False, "icon": r.icon, "color": r.color,
                    "count": counts.get(r.name, 0)})
    return out


def add(user, name, icon, color):
    name = " ".join(str(name or "").split())[:40]
    if len(name) < 2:
        raise ValueError("Give the category a name.")
    builtin = next((b for b in BUILTIN if b.lower() == name.lower()), None)
    if builtin:                                    # a hidden built-in comes back instead
        UserCategory.objects.filter(user=user, name=builtin).delete()
        return builtin
    if UserCategory.objects.filter(user=user, name__iexact=name, is_custom=True).exists():
        raise ValueError(f"You already have a category called {name}.")
    if icon not in ICONS:
        icon = "package"
    if not COLOR_RE.fullmatch(str(color or "")):
        color = "#6b7280"
    UserCategory.objects.create(user=user, name=name, icon=icon, color=color, is_custom=True)
    return name


def show(user, name):
    UserCategory.objects.filter(user=user, name=name, is_custom=False).delete()


@transaction.atomic
def remove(user, name, move_to):
    """Move everything in `name` to `move_to`, then hide or delete `name`."""
    if name == KEEP:
        raise ValueError("Other always stays: it's where anything unsorted goes.")
    if not is_valid(user, name):
        raise ValueError("That category doesn't exist.")
    if move_to == name or move_to not in usable(user):
        raise ValueError("Choose another category to move its expenses to.")
    moved = Expense.objects.filter(user=user, category=name).update(category=move_to)
    StatementTransaction.objects.filter(user=user, category=name).update(category=move_to)
    MerchantRule.objects.filter(user=user, category=name).update(category=move_to)
    for budget in SpendingBudget.objects.filter(user=user, category=name):
        if SpendingBudget.objects.filter(user=user, category=move_to).exists():
            budget.delete()                        # the other category already has a limit
        else:
            budget.category = move_to
            budget.label = move_to
            budget.save(update_fields=["category", "label"])
    if name in BUILTIN:
        UserCategory.objects.update_or_create(user=user, name=name, defaults={"hidden": True, "is_custom": False})
    else:
        UserCategory.objects.filter(user=user, name=name, is_custom=True).delete()
    return moved
