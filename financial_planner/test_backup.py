import gzip
import json
from datetime import date, datetime
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from expenses.models import Expense
from .backup import build_backup, send_backups, tables
from .models import Debt


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class BackupTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="bk@example.test", username="bk", password="x-Test-12345")
        self.other = get_user_model().objects.create_user(email="bk2@example.test", username="bk2", password="x-Test-12345")
        Expense.objects.create(user=self.user, title="Mine", amount=Decimal("5"), category="Other", date=date(2026, 1, 1))
        Expense.objects.create(user=self.other, title="Theirs", amount=Decimal("9"), category="Other", date=date(2026, 1, 1))
        Debt.objects.create(user=self.user, name="Card", current_balance=Decimal("100"), annual_interest_rate=Decimal("20"),
                            minimum_payment=Decimal("10"))

    def test_every_table_is_covered_and_only_mine(self):
        data, missed = tables(self.user)
        self.assertEqual(missed, [])                                  # a new table must be added to backup.py
        self.assertEqual([r["fields"]["title"] for r in data["expenses.Expense"]], ["Mine"])
        self.assertEqual(len(data["financial_planner.Debt"]), 1)
        self.assertNotIn("assistant.EncryptionKey", data)
        payload = json.loads(gzip.decompress(build_backup(self.user)))
        self.assertEqual(payload["counts"]["expenses.Expense"], 1)
        self.assertNotIn("password", json.dumps(payload))

    def test_download_needs_login(self):
        self.assertEqual(APIClient().get("/api/fp/backup/").status_code, 401)
        c = APIClient()
        c.force_authenticate(self.user)
        r = c.get("/api/fp/backup/")
        self.assertEqual(r["Content-Type"], "application/gzip")
        self.assertIn("glacier-backup-", r["Content-Disposition"])
        self.assertEqual(json.loads(gzip.decompress(r.content))["user"], "bk@example.test")

    def test_emailed_on_sundays_only(self):
        sunday = datetime(2026, 11, 15, 8, 0, tzinfo=ZoneInfo("America/Toronto"))
        with mock.patch("financial_planner.backup.timezone.now", return_value=sunday):
            self.assertEqual(send_backups(), 2)
        self.assertEqual(mail.outbox[0].attachments[0][0][-8:], ".json.gz")
        monday = datetime(2026, 11, 16, 8, 0, tzinfo=ZoneInfo("America/Toronto"))
        with mock.patch("financial_planner.backup.timezone.now", return_value=monday):
            self.assertEqual(send_backups(), 0)
