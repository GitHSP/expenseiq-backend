# activity/signals.py
#
# Records changes automatically whenever a tracked model is saved or
# deleted, from anywhere (views, the AI assistant's tools, statement import,
# the monthly rollover). Each model lists the fields worth recording; saves
# that touch only other fields (e.g. avalanche_order, plan totals) are
# ignored, and checklist items only log being ticked/unticked — they're
# created and synced in bulk every month, which would just be noise.

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver

from expenses.models import Expense, Income
from financial_planner.models import ChecklistItem, Debt, EmergencyFund, RecurringItem

from .context import deleting_user, is_quiet, log_activity

MONEY = "money"

# model -> (area, {field: (label, kind)}, how to get the user, how to name it)
TRACKED = {
    Debt: ("debt", {
        "name": ("Name", None),
        "current_balance": ("Balance", MONEY),
        "annual_interest_rate": ("APR %", None),
        "minimum_payment": ("Minimum payment", MONEY),
        "credit_limit": ("Credit limit", MONEY),
        "due_day": ("Due day", None),
        "is_active": ("Active", None),
    }, lambda o: o.user, lambda o: o.name),
    RecurringItem: ("recurring", {
        "label": ("Name", None),
        "amount": ("Amount", MONEY),
        "due_day": ("Due day", None),
        "category": ("Type", None),
        "is_auto_debit": ("Auto debit", None),
        "is_active": ("Active", None),
    }, lambda o: o.user, lambda o: o.label),
    EmergencyFund: ("fund", {
        "current_balance": ("Balance", MONEY),
        "target_amount": ("Target", MONEY),
        "monthly_contribution": ("Monthly contribution", MONEY),
    }, lambda o: o.user, lambda o: "Emergency fund"),
    Expense: ("expense", {
        "title": ("Title", None),
        "amount": ("Amount", MONEY),
        "category": ("Category", None),
        "date": ("Date", None),
    }, lambda o: o.user, lambda o: o.title),
    Income: ("income", {
        "title": ("Title", None),
        "amount": ("Amount", MONEY),
        "category": ("Category", None),
        "date": ("Date", None),
    }, lambda o: o.user, lambda o: o.title),
}


def _plain(value):
    if isinstance(value, Decimal):
        return str(value.quantize(Decimal("0.01")))
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _money(value):
    return None if value is None else _plain(Decimal(str(value)))


def _snapshot(instance, fields):
    # Values set in code may still be ints/floats until reloaded, so money
    # fields are normalised to "0.00" strings.
    return {
        f: _money(getattr(instance, f)) if kind == MONEY else _plain(getattr(instance, f))
        for f, (label, kind) in fields.items()
    }


@receiver(pre_save)
def _remember_previous(sender, instance, **kwargs):
    if sender is ChecklistItem:
        if instance.pk:
            instance._activity_was_completed = (
                ChecklistItem.objects.filter(pk=instance.pk).values_list("is_completed", flat=True).first()
            )
        return
    if sender not in TRACKED or not instance.pk:
        return
    fields = TRACKED[sender][1]
    old = sender.objects.filter(pk=instance.pk).values(*fields).first()
    instance._activity_before = (
        {f: _money(v) if fields[f][1] == MONEY else _plain(v) for f, v in old.items()} if old else None
    )


@receiver(post_save)
def _record_save(sender, instance, created, **kwargs):
    if sender is ChecklistItem:
        _record_checklist(instance, created)
        return
    if sender not in TRACKED or kwargs.get("raw"):
        return
    area, fields, get_user, get_title = TRACKED[sender]
    if is_quiet(area):
        return

    after = _snapshot(instance, fields)
    if created:
        changes = [
            {"field": f, "label": label, "before": None, "after": after[f], "money": kind == MONEY}
            for f, (label, kind) in fields.items() if after[f] not in (None, "", False)
        ]
        log_activity(get_user(instance), area, "created", get_title(instance), object_id=instance.pk, changes=changes)
        return

    before = getattr(instance, "_activity_before", None)
    if before is None:
        return
    changes = [
        {"field": f, "label": label, "before": before[f], "after": after[f], "money": kind == MONEY}
        for f, (label, kind) in fields.items() if before[f] != after[f]
    ]
    if changes:
        log_activity(get_user(instance), area, "updated", get_title(instance), object_id=instance.pk, changes=changes)


def _record_checklist(item, created):
    if created or is_quiet("checklist"):
        return
    was = getattr(item, "_activity_was_completed", None)
    if was is None or was == item.is_completed:
        return
    plan = item.monthly_plan
    log_activity(
        plan.user, "checklist", "ticked" if item.is_completed else "unticked", item.label,
        object_id=item.pk,
        details={
            "amount": _money(item.amount),
            "month": f"{plan.year}-{plan.month:02d}",
        },
    )


@receiver(post_delete)
def _record_delete(sender, instance, **kwargs):
    if sender not in TRACKED:
        return
    area, fields, get_user, get_title = TRACKED[sender]
    if is_quiet(area):
        return
    try:
        user = get_user(instance)
    except Exception:
        return  # the user itself is being deleted
    before = _snapshot(instance, fields)
    changes = [
        {"field": f, "label": label, "before": before[f], "after": None, "money": kind == MONEY}
        for f, (label, kind) in fields.items() if before[f] not in (None, "", False)
    ]
    log_activity(user, area, "deleted", get_title(instance), object_id=instance.pk, changes=changes)


# Deleting an account cascades through its debts, expenses etc. Logging
# those deletions would create new rows pointing at the user being deleted.
@receiver(pre_delete, sender=get_user_model())
def _user_deleting(sender, instance, **kwargs):
    deleting_user(instance.pk, True)


@receiver(post_delete, sender=get_user_model())
def _user_deleted(sender, instance, **kwargs):
    deleting_user(instance.pk, False)
