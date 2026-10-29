"""One-time cleanup: interest and fees charged by a bank or card were saved
as expenses. They aren't spending the user counts, so their expense entries
are removed (the statement rows stay, and so do the card balances)."""

from django.db import migrations


def forwards(apps, schema_editor):
    from expenses.income_policy import is_charge
    StatementTransaction = apps.get_model("expenses", "StatementTransaction")
    Expense = apps.get_model("expenses", "Expense")
    fixed = removed = 0
    for tx in StatementTransaction.objects.filter(kind="expense", amount__lt=0).iterator():
        if not is_charge(f"{tx.title} {tx.description}"):
            continue
        if tx.expense_id:
            removed += Expense.objects.filter(id=tx.expense_id).delete()[0]
        tx.expense_id = None
        tx.kind = "skipped"
        tx.save(update_fields=["expense", "kind"])
        fixed += 1
    if fixed:
        print(f"\n  Interest and fees saved as expenses: {fixed} rows, {removed} expense entries removed")


class Migration(migrations.Migration):
    dependencies = [("expenses", "0012_bill_share")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
