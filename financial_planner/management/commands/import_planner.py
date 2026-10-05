# financial_planner/management/commands/import_planner.py
#
# Load a user's debts and recurring payments from a JSON file, e.g. one
# made from their budgeting spreadsheet. Keep the JSON itself out of git —
# it's personal financial data (*.local.json is git-ignored).
#
#   python manage.py import_planner planner.local.json --email you@example.com
#   python manage.py import_planner planner.local.json --email you@example.com --dry-run
#
# JSON shape:
# {
#   "debts": [{
#       "name": "CIBC Visa", "aliases": ["CIBC"],     # aliases match older names
#       "debt_type": "credit_card", "current_balance": 3494,
#       "keep_existing_balance": false,               # true = only set balance on create
#       "annual_interest_rate": 21.99, "minimum_payment": 70,
#       "credit_limit": 3500, "due_day": 13, "notes": "..."
#   }],
#   "recurring": [{
#       "label": "Rent", "amount": 650, "due_day": 26,
#       "category": "fixed_expense", "is_auto_debit": false, "notes": "..."
#   }]
# }

import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from financial_planner.models import Debt, MonthlyPlan, RecurringItem
from financial_planner.services import recalculate_avalanche_order, sync_month_checklist


def _dec(value):
    return None if value is None else Decimal(str(value))


class Command(BaseCommand):
    help = "Import debts and recurring payments for a user from a JSON file."

    def add_arguments(self, parser):
        parser.add_argument("file", help="Path to the JSON file.")
        parser.add_argument("--email", required=True, help="Email of the user to import into.")
        parser.add_argument(
            "--keep-checklist", action="store_true",
            help="Keep this month's existing checklist items instead of rebuilding it from scratch.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Show what would change, then roll back.")

    def handle(self, *args, **opts):
        try:
            with open(opts["file"], encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise CommandError(f"Couldn't read {opts['file']}: {e}")

        User = get_user_model()
        try:
            user = User.objects.get(email__iexact=opts["email"])
        except User.DoesNotExist:
            raise CommandError(f"No user with email {opts['email']}")

        with transaction.atomic():
            self._import_debts(user, data.get("debts", []))
            self._import_recurring(user, data.get("recurring", []))
            recalculate_avalanche_order(user)
            self._rebuild_current_month(user, keep=opts["keep_checklist"])

            if opts["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("\nDry run — nothing was saved."))
            else:
                self.stdout.write(self.style.SUCCESS("\nImport complete."))

    # ── Debts ──
    def _import_debts(self, user, debts):
        self.stdout.write("Debts:")
        seen = set()
        for d in debts:
            names = [d["name"], *d.get("aliases", [])]
            debt = next(
                (x for n in names for x in Debt.objects.filter(user=user, name__iexact=n)),
                None,
            )
            created = debt is None
            if created:
                debt = Debt(user=user)

            old_name = debt.name
            debt.name = d["name"]
            debt.debt_type = d.get("debt_type", "credit_card")
            if created or not d.get("keep_existing_balance"):
                debt.current_balance = _dec(d["current_balance"])
            debt.annual_interest_rate = _dec(d.get("annual_interest_rate", 0))
            debt.minimum_payment = _dec(d.get("minimum_payment", 0))
            debt.credit_limit = _dec(d.get("credit_limit"))
            debt.due_day = d.get("due_day")
            debt.notes = d.get("notes", "")
            debt.is_active = debt.current_balance > 0
            if debt.is_active:
                debt.paid_off_date = None
            debt.save()
            seen.add(debt.id)

            if created:
                action = "added"
            elif old_name != debt.name:
                action = f"updated (renamed from '{old_name}')"
            else:
                action = "updated"
            self.stdout.write(f"  {action:<10} {debt.name}: ${debt.current_balance} @ {debt.annual_interest_rate}%")

        for other in Debt.objects.filter(user=user, is_active=True).exclude(id__in=seen):
            self.stdout.write(self.style.WARNING(
                f"  not in file, left as is: {other.name} (${other.current_balance})"
            ))

    # ── Recurring payments ──
    def _import_recurring(self, user, items):
        self.stdout.write("Recurring payments:")
        for r in items:
            item = RecurringItem.objects.filter(user=user, label__iexact=r["label"]).first()
            created = item is None
            if created:
                item = RecurringItem(user=user)
            item.label = r["label"]
            item.amount = _dec(r["amount"])
            item.due_day = r.get("due_day")
            item.category = r.get("category", "fixed_expense")
            item.is_auto_debit = bool(r.get("is_auto_debit", False))
            item.notes = r.get("notes", "")
            item.is_active = True
            item.full_clean(exclude=["user"])
            item.save()
            self.stdout.write(f"  {'added' if created else 'updated':<10} {item.label}: ${item.amount}")

    # ── This month's checklist ──
    def _rebuild_current_month(self, user, keep):
        now = timezone.now()
        # Created directly (not via the rollover path): the imported balances
        # are already current, so no catch-up interest should be applied.
        plan, _ = MonthlyPlan.objects.get_or_create(user=user, year=now.year, month=now.month)

        if not keep:
            removed = plan.checklist_items.count()
            plan.checklist_items.all().delete()
            # Plans created early for later months hold stale copies; they'll
            # be rebuilt from the new data when those months arrive.
            future = MonthlyPlan.objects.filter(user=user).filter(
                year__gt=now.year
            ) | MonthlyPlan.objects.filter(user=user, year=now.year, month__gt=now.month)
            future_count = future.count()
            future.delete()
            self.stdout.write(f"Cleared {removed} old checklist item(s) for {now.year}-{now.month:02d}"
                              + (f" and {future_count} future plan(s)." if future_count else "."))

        items, top_debt = sync_month_checklist(plan)
        plan.refresh_from_db()
        self.stdout.write(f"Checklist for {now.year}-{now.month:02d}: {len(items)} items")
        self.stdout.write(f"  Income:            ${plan.total_income}")
        self.stdout.write(f"  Recurring:         ${plan.total_fixed_expenses}")
        self.stdout.write(f"  Debt payments:     ${plan.total_debt_payments}")
        self.stdout.write(f"  Emergency savings: ${plan.emergency_fund_contribution}")
        self.stdout.write(f"  Extra to {top_debt.name if top_debt else '—'}: ${plan.surplus_to_avalanche}")
