from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Expense, MerchantRule, SpendingBudget


class CategoryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="k@example.test", username="k", password="x-Test-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        for cat in ("Health", "Health", "Medical & Health"):
            Expense.objects.create(user=self.user, title="x", amount=Decimal("10"), category=cat, date=date(2026, 9, 1))
        MerchantRule.objects.create(user=self.user, match="clinic", category="Health")
        SpendingBudget.objects.create(user=self.user, label="Health", category="Health", monthly_limit=Decimal("50"))

    def post(self, **data):
        return self.client.post("/api/categories/", data, format="json")

    def names(self, res=None):
        rows = (res or self.client.get("/api/categories/")).data["categories"]
        return {r["name"]: r for r in rows}

    def test_merge_a_builtin_into_another(self):
        res = self.post(action="remove", name="Health", move_to="Medical & Health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(Expense.objects.filter(user=self.user, category="Medical & Health").count(), 3)
        self.assertEqual(MerchantRule.objects.get(user=self.user).category, "Medical & Health")
        self.assertEqual(SpendingBudget.objects.get(user=self.user).category, "Medical & Health")
        self.assertTrue(self.names(res)["Health"]["hidden"])
        # Hidden categories can't be picked for a new expense, and come back when shown.
        self.assertEqual(self.post(action="remove", name="Shopping", move_to="Health").status_code, 400)
        self.post(action="show", name="Health")
        self.assertFalse(self.names()["Health"]["hidden"])

    def test_add_use_and_remove_my_own(self):
        self.assertEqual(self.post(name="Pets", icon="gift", color="#123456").status_code, 200)
        self.assertEqual(self.post(name="pets").status_code, 400)                    # already have it
        res = self.client.post("/api/expenses/", {"title": "Food", "amount": "20", "category": "Pets", "date": "2026-09-02"}, format="json")
        self.assertEqual(res.status_code, 201)
        bad = self.client.post("/api/expenses/", {"title": "x", "amount": "1", "category": "Nope", "date": "2026-09-02"}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.names()["Pets"]["count"], 1)
        self.post(action="remove", name="Pets", move_to="Other")
        self.assertNotIn("Pets", self.names())
        self.assertEqual(Expense.objects.get(title="Food").category, "Other")

    def test_other_stays_and_others_are_private(self):
        self.assertEqual(self.post(action="remove", name="Other", move_to="Shopping").status_code, 400)
        self.assertEqual(self.post(name="Health").status_code, 200)                  # a built-in: nothing to add
        other = get_user_model().objects.create_user(email="l@example.test", username="l", password="x-Test-12345")
        client = APIClient()
        client.force_authenticate(other)
        self.post(name="Secret")
        self.assertNotIn("Secret", {r["name"] for r in client.get("/api/categories/").data["categories"]})
