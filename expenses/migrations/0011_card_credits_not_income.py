"""One-time cleanup: money coming onto a credit card was sometimes saved as
income. A card payment is the user's own money moving (a transfer); a
refund, cashback or reversal lowers spending. Neither is income."""

import re

from django.db import migrations

PAYMENT_RE = re.compile(r"\bPAYMENT\b|\bPAIEMENT\b", re.I)


def forwards(apps, schema_editor):
    StatementTransaction = apps.get_model("expenses", "StatementTransaction")
    Income = apps.get_model("expenses", "Income")
    fixed = removed = 0
    for tx in StatementTransaction.objects.filter(kind="income", amount__gt=0, account__kind="credit_card").iterator():
        if tx.income_id:
            removed += Income.objects.filter(id=tx.income_id).delete()[0]
        tx.income_id = None
        tx.kind = "transfer" if PAYMENT_RE.search(f"{tx.title} {tx.description}") else "skipped"
        tx.save(update_fields=["income", "kind"])
        fixed += 1
    if fixed:
        print(f"\n  Card payments/refunds saved as income: {fixed} rows, {removed} income entries removed")


class Migration(migrations.Migration):
    dependencies = [("expenses", "0010_income_policy_cleanup")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
