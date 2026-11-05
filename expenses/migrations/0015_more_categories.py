"""More expense categories (drinks & bars, smoke & vape, transfers to people,
loan & BNPL payments, household), and a one-time re-sort of what's in "Other":
each expense and statement row filed as Other gets the category its
description now matches, if any. Everything else is left as it is; the user's
own rules are applied again on deploy (apply_rules), so they still win."""

from django.db import migrations, models


def resort(apps, schema_editor):
    from expenses.statement_import import EXPENSE_RULES, _match
    Expense = apps.get_model("expenses", "Expense")
    Row = apps.get_model("expenses", "StatementTransaction")
    described = {e: d for e, d in Row.objects.filter(expense__isnull=False).values_list("expense_id", "description")}
    for e in Expense.objects.filter(category="Other"):
        new = _match(EXPENSE_RULES, f"{e.title} {described.get(e.id, '')}", "Other")
        if new != "Other":
            Expense.objects.filter(pk=e.pk).update(category=new)
    for r in Row.objects.filter(category="Other", amount__lt=0):
        new = _match(EXPENSE_RULES, f"{r.title} {r.description}", "Other")
        if new != "Other":
            Row.objects.filter(pk=r.pk).update(category=new)


CHOICES = [
    ('Food & Dining', 'Food & Dining'), ('Transport', 'Transport'), ('Shopping', 'Shopping'),
    ('Entertainment', 'Entertainment'), ('Health', 'Health'), ('Housing', 'Housing'), ('Education', 'Education'),
    ('Gym & Fitness', 'Gym & Fitness'), ('Subscriptions', 'Subscriptions'), ('Insurance', 'Insurance'),
    ('Clothing & Shopping', 'Clothing & Shopping'), ('Personal Care & Beauty', 'Personal Care & Beauty'),
    ('Gifts & Donations', 'Gifts & Donations'), ('Bills & Utilities', 'Bills & Utilities'),
    ('Medical & Health', 'Medical & Health'), ('Education & Books', 'Education & Books'),
    ('Abroad Expense', 'Abroad Expense'), ('Drinks & Bars', 'Drinks & Bars'), ('Smoke & Vape', 'Smoke & Vape'),
    ('Transfers to People', 'Transfers to People'), ('Loan & BNPL Payments', 'Loan & BNPL Payments'),
    ('Household', 'Household'), ('Other', 'Other'), ('Immigration', 'Immigration'),
]


class Migration(migrations.Migration):
    dependencies = [("expenses", "0014_roommate_transfer_sorting")]

    operations = [
        migrations.AlterField(model_name="expense", name="category", field=models.CharField(choices=CHOICES, max_length=50)),
        migrations.RunPython(resort, migrations.RunPython.noop),
    ]
