from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from expenses.models import MerchantRule
from expenses.rules import apply_all_rules


class Command(BaseCommand):
    help = "Re-apply every user's rules to what's already imported (safe to run again)."

    def handle(self, *args, **options):
        users = get_user_model().objects.filter(id__in=MerchantRule.objects.values("user_id").distinct())
        for user in users:
            t = apply_all_rules(user)
            if any(t.values()):
                self.stdout.write(f"user {user.id}: {t['statement_rows']} rows fixed, "
                                  f"{t['entries_removed']} entries removed, {t['entries_updated']} updated")
        self.stdout.write("Rules applied.")
