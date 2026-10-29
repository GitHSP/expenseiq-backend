"""One-time cleanup: deposits saved as income from bank statements that
aren't pay, a government benefit or an ATM deposit (a friend's e-transfer,
the user's own transfer, a refund) stop counting as income. Their income
entries are removed and the statement rows marked as not saved."""

from django.db import migrations


def forwards(apps, schema_editor):
    from expenses.income_policy import deposit_class, words_from
    StatementTransaction = apps.get_model("expenses", "StatementTransaction")
    Income = apps.get_model("expenses", "Income")
    MerchantRule = apps.get_model("expenses", "MerchantRule")
    RecurringItem = apps.get_model("financial_planner", "RecurringItem")

    rows = (StatementTransaction.objects.filter(kind="income", amount__gt=0)
            .exclude(account__kind="credit_card"))
    words_by_user = {}
    fixed = removed = 0
    for tx in rows.iterator():
        words = words_by_user.get(tx.user_id)
        if words is None:
            words = words_by_user[tx.user_id] = words_from(
                RecurringItem.objects.filter(user_id=tx.user_id, category="income").values_list("average_from", "label"),
                MerchantRule.objects.filter(user_id=tx.user_id, treat_as="spending").values_list("match", flat=True))
        if deposit_class(f"{tx.title} {tx.description}", words) == "income":
            continue
        if tx.income_id:
            removed += Income.objects.filter(id=tx.income_id).delete()[0]
        tx.income_id = None
        tx.kind = "skipped"
        tx.save(update_fields=["income", "kind"])
        fixed += 1
    if fixed:
        print(f"\n  Deposits that aren't pay/benefits/ATM: {fixed} rows, {removed} income entries removed")


class Migration(migrations.Migration):
    dependencies = [
        ("expenses", "0009_shared_bill_rule"),
        ("financial_planner", "0011_recurring_pays_debt"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
