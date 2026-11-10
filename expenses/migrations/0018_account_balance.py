from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0017_user_categories"),
    ]

    operations = [
        migrations.AddField(
            model_name="account",
            name="current_balance",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="account",
            name="balance_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="account",
            name="overdraft_limit",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
    ]
