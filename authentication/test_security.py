"""
Security tests: one user attacking another's data through every endpoint,
brute-forcing login, and password/session handling.
"""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from assistant.models import EncryptionKey, SyncedMessage
from expenses.models import Expense, Income
from financial_planner.models import ChecklistItem, Debt, MonthlyPlan, RecurringItem

User = get_user_model()
PASSWORD = "Str0ng-Passphrase-41"


def make_user(email):
    return User.objects.create_user(email=email, username=email.split("@")[0], password=PASSWORD)


class CrossUserAccessTests(TestCase):
    """User B must not be able to read, change, delete or influence any of A's data."""

    def setUp(self):
        cache.clear()
        self.a = make_user("alice@example.test")
        self.b = make_user("bob@example.test")
        self.expense = Expense.objects.create(user=self.a, title="A coffee", amount=4, category="Other", date=date(2026, 10, 1))
        self.income = Income.objects.create(user=self.a, title="A pay", amount=1000, category="Salary", date=date(2026, 10, 1))
        self.debt = Debt.objects.create(user=self.a, name="A Visa", current_balance=Decimal("1000.00"),
                                        annual_interest_rate=20, minimum_payment=50)
        self.recurring = RecurringItem.objects.create(user=self.a, label="A rent", amount=650)
        self.plan = MonthlyPlan.objects.create(user=self.a, year=2026, month=10)
        self.item = ChecklistItem.objects.create(monthly_plan=self.plan, label="A rent", amount=650, category="fixed_expense")
        SyncedMessage.objects.create(user=self.a, client_id="m1", ciphertext="secret", iv="iv")
        EncryptionKey.objects.create(user=self.a, wrapped_dek="k", wrap_iv="iv", salt="s")
        self.client = APIClient()
        self.client.force_authenticate(self.b)

    def assert_blocked(self, method, url, data=None):
        res = getattr(self.client, method)(url, data or {}, format="json")
        self.assertIn(res.status_code, (403, 404), f"{method.upper()} {url} -> {res.status_code}")

    def test_detail_endpoints_hide_other_users_objects(self):
        targets = [
            f"/api/expenses/{self.expense.id}/", f"/api/income/{self.income.id}/",
            f"/api/fp/debts/{self.debt.id}/", f"/api/fp/recurring/{self.recurring.id}/",
            f"/api/fp/plans/{self.plan.id}/", f"/api/fp/checklist/{self.item.id}/",
        ]
        for url in targets:
            for method in ("get", "patch", "put", "delete"):
                with self.subTest(method=method, url=url):
                    self.assert_blocked(method, url, {"title": "hacked", "amount": "1"})
        self.assert_blocked("post", f"/api/fp/checklist/{self.item.id}/toggle/")
        self.assert_blocked("post", f"/api/fp/plans/{self.plan.id}/checklist/",
                            {"label": "x", "amount": "1", "category": "fixed_expense"})
        self.assertEqual(self.client.get(f"/api/fp/plans/{self.plan.id}/checklist/").data, [])

    def test_lists_only_show_your_own(self):
        for url in ("/api/expenses/", "/api/income/", "/api/fp/debts/", "/api/fp/recurring/",
                    "/api/fp/plans/", "/api/activity/", "/api/assistant/sync/messages/"):
            with self.subTest(url=url):
                res = self.client.get(url)
                data = res.data.get("results", res.data) if isinstance(res.data, dict) else res.data
                self.assertEqual(list(data), [], url)
        self.assertEqual(self.client.get("/api/assistant/encryption-key/").status_code, 404)

    def test_cannot_link_own_checklist_item_to_someone_elses_debt(self):
        # Otherwise ticking the item would pay down A's debt.
        plan_b = MonthlyPlan.objects.create(user=self.b, year=2026, month=10)
        res = self.client.post(f"/api/fp/plans/{plan_b.id}/checklist/",
                               {"label": "x", "amount": "500", "category": "debt_min", "linked_debt": self.debt.id}, format="json")
        self.assertEqual(res.status_code, 400)
        mine = ChecklistItem.objects.create(monthly_plan=plan_b, label="mine", amount=500, category="debt_min")
        res = self.client.patch(f"/api/fp/checklist/{mine.id}/", {"linked_debt": self.debt.id}, format="json")
        self.assertEqual(res.status_code, 400)
        self.debt.refresh_from_db()
        self.assertEqual(self.debt.current_balance, Decimal("1000.00"))

    def test_cannot_move_a_checklist_item_into_someone_elses_plan(self):
        plan_b = MonthlyPlan.objects.create(user=self.b, year=2026, month=10)
        mine = ChecklistItem.objects.create(monthly_plan=plan_b, label="mine", amount=5, category="fixed_expense")
        self.client.patch(f"/api/fp/checklist/{mine.id}/", {"monthly_plan": self.plan.id}, format="json")
        mine.refresh_from_db()
        self.assertEqual(mine.monthly_plan_id, plan_b.id)

    def test_cannot_create_records_for_someone_else(self):
        self.client.post("/api/expenses/", {"title": "x", "amount": "1", "category": "Other",
                                            "date": "2026-10-01", "user": self.a.id}, format="json")
        self.assertFalse(Expense.objects.filter(user=self.a, title="x").exists())

    def test_sync_cannot_overwrite_someone_elses_messages(self):
        self.client.post("/api/assistant/sync/messages/",
                         {"messages": [{"client_id": "m1", "ciphertext": "evil", "iv": "x"}]}, format="json")
        self.assertEqual(SyncedMessage.objects.get(user=self.a, client_id="m1").ciphertext, "secret")
        self.client.delete("/api/assistant/sync/messages/")
        self.assertTrue(SyncedMessage.objects.filter(user=self.a).exists())


class UnauthenticatedTests(TestCase):
    def test_every_data_endpoint_requires_login(self):
        client = APIClient()
        for url in ("/api/expenses/", "/api/income/", "/api/fp/debts/", "/api/fp/recurring/",
                    "/api/fp/plans/current/", "/api/activity/", "/api/assistant/chat/",
                    "/api/statements/preview/", "/api/statements/import/", "/api/auth/me/"):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 401)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class LoginProtectionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user("victim@example.test")

    def login(self, password, ip="203.0.113.7", xff=None):
        extra = {"REMOTE_ADDR": ip}
        if xff:
            extra["HTTP_X_FORWARDED_FOR"] = f"{xff}, {ip}"
        return APIClient().post("/api/auth/login/", {"email": "victim@example.test", "password": password},
                                format="json", **extra)

    def test_account_locks_after_repeated_wrong_passwords(self):
        for _ in range(5):
            self.assertEqual(self.login("wrong").status_code, 401)
        # Even the right password is refused while locked — from any address.
        res = self.login(PASSWORD, ip="198.51.100.9")
        self.assertEqual(res.status_code, 429)

    def test_spoofed_forwarded_for_does_not_dodge_rate_limit(self):
        codes = [self.login("wrong", xff=f"10.0.0.{i}").status_code for i in range(25)]
        self.assertIn(429, codes)

    def test_email_is_case_insensitive(self):
        res = APIClient().post("/api/auth/login/", {"email": "VICTIM@example.test", "password": PASSWORD}, format="json")
        self.assertEqual(res.status_code, 200)


class PasswordAndSessionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user("pw@example.test")

    def tokens(self):
        res = APIClient().post("/api/auth/login/", {"email": "pw@example.test", "password": PASSWORD}, format="json")
        return res.data["access_token"], res.data["refresh_token"]

    def test_reset_rejects_weak_passwords(self):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode
        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)
        res = APIClient().post("/api/auth/reset-password/", {"uid": uid, "token": token, "password": "123"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_change_password_uses_full_rules(self):
        client = APIClient()
        client.force_authenticate(self.user)
        res = client.post("/api/auth/change-password/", {"old_password": PASSWORD, "new_password": "password",
                                                          "new_password2": "password"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_changing_password_signs_out_other_sessions(self):
        _, stolen_refresh = self.tokens()
        client = APIClient()
        client.force_authenticate(self.user)
        new = "An0ther-Strong-Phrase"
        client.post("/api/auth/change-password/", {"old_password": PASSWORD, "new_password": new,
                                                    "new_password2": new}, format="json")
        res = APIClient().post("/api/auth/token/refresh/", {"refresh": stolen_refresh}, format="json")
        self.assertEqual(res.status_code, 401)


class LogoutAllTests(TestCase):
    def test_signs_out_every_device(self):
        cache.clear()
        user = make_user("all@example.test")
        login = lambda: APIClient().post("/api/auth/login/", {"email": "all@example.test", "password": PASSWORD}, format="json").data
        phone, laptop = login(), login()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {laptop['access_token']}")
        self.assertEqual(client.post("/api/auth/logout-all/").status_code, 200)
        for session in (phone, laptop):
            res = APIClient().post("/api/auth/token/refresh/", {"refresh": session["refresh_token"]}, format="json")
            self.assertEqual(res.status_code, 401)
