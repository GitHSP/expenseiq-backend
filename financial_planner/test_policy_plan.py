from datetime import date
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Investment
from .policy_plan import PlanError, build_plan, validate_plan

# A made-up policy: $100,000 cover, $50 a month, cost of insurance rising each year.
PLAN = {
    "face": 100000, "issue_age": 30, "premium": 50, "pay_until_age": 60,
    "premium_tax": 0.02, "monthly_fee": 5, "rates": [3, 6],
    "coi": {"unit": "yearly_per_1000", "by": "policy_year", "start": 1,
            "rates": [1.0 + 0.15 * k for k in range(70)]},
    "riders": [{"name": "Extra cover", "monthly": 2, "until_age": 50}],
    "surrender": [500, 400, 300, 200, 100],
    "values": [{"date": "2021-01-31", "fund": 300}, {"date": "2021-12-31", "fund": 400.5}],
    "years": [{"from": "2021-01-01", "to": "2021-12-31", "premiums": 600, "charges": 200, "tax": 12,
               "growth": 20, "fund_start": 0, "fund_end": 400.5}],
    "illustration": {"rate": 6, "rows": [{"year": 1, "age": 31, "deposit": 600, "value": 380, "surrender": 0,
                                          "death_benefit": 100000}]},
}


def policy(plan=PLAN, start=date(2021, 1, 1)):
    return SimpleNamespace(plan=validate_plan(plan), start_date=start)


class PolicyPlanTests(TestCase):
    def test_today_and_total(self):
        r = build_plan(policy(), today=date(2022, 3, 15))
        self.assertEqual(r["policy_year"], 2)
        self.assertEqual(r["today"]["paid"], 50 * 15)                  # Jan 2021 .. Mar 2022
        self.assertEqual(r["today"]["paid_by_fund_date"], 600)
        self.assertEqual(r["today"]["gain"], round(400.5 - 600, 2))
        self.assertEqual(r["today"]["surrender_charge"], 400)
        self.assertEqual(r["today"]["surrender_value"], 0.5)
        self.assertEqual(r["today"]["surrender_free_from"], "2026-01-01")
        self.assertEqual(r["total_to_pay"]["amount"], 50 * 12 * 30)
        self.assertEqual(r["years"][0]["return_pct"], 10.0)           # 20 on an average of ~200

    def test_projection_grows_more_at_higher_returns_and_can_run_out(self):
        r = build_plan(policy(), today=date(2022, 3, 15))
        low, high = r["projections"]
        self.assertGreater(high["at"]["55"], low["at"]["55"])
        self.assertIsNotNone(low["lapse_age"])                         # costs outgrow 3% once premiums stop
        self.assertTrue(all(p["paid"] <= 50 * 12 * 30 for p in high["points"]))
        first = high["points"][0]
        self.assertEqual(first["date"], "2023-01-01")                  # the next anniversary
        self.assertEqual(first["surrender"], round(max(first["fund"] - 300, 0), 2))

    def test_monthly_cost_matches_the_insurers_formula(self):
        plan = dict(PLAN, values=[{"date": "2021-12-31", "fund": 1000}], rates=[0], riders=[])
        r = build_plan(policy(plan), today=date(2022, 1, 15))
        cost = r["projections"][0]["points"][0]["cost"]
        # year 2: rate 1.15 per $1,000 a year on (100,000 - fund), plus $5 a month
        self.assertAlmostEqual(cost / 12, 5 + 1.15 * (100000 - 1000) / 12000, delta=1)

    def test_cover_plus_fund_costs_more_than_level(self):
        level = build_plan(policy(dict(PLAN, rates=[6])), today=date(2022, 3, 15))["projections"][0]
        plus = build_plan(policy(dict(PLAN, rates=[6], death_benefit="plus_fund")), today=date(2022, 3, 15))["projections"][0]
        self.assertLess(plus["at"]["55"], level["at"]["55"])        # insurance on the whole cover, every year
        with self.assertRaises(PlanError):
            validate_plan(dict(PLAN, death_benefit="double"))

    def test_validation(self):
        for bad in ({"face": 1}, dict(PLAN, coi={"unit": "weekly", "by": "age", "rates": []}),
                    dict(PLAN, face="lots"), dict(PLAN, values=[{"date": "soon", "fund": 1}])):
            with self.assertRaises(PlanError):
                validate_plan(bad)
        self.assertEqual(validate_plan({}), {})


class PolicyPlanApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="pp@example.test", username="pp", password="x-Test-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.inv = Investment.objects.create(user=self.user, kind="insurance", name="UL", currency="CAD",
                                             start_date=date(2021, 1, 1), payment=50)

    def test_paste_plan_sets_cash_value_and_returns_the_plan(self):
        r = self.client.patch(f"/api/fp/investments/{self.inv.id}/", {"plan": PLAN}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.inv.refresh_from_db()
        self.assertEqual(str(self.inv.current_value), "400.50")
        self.assertEqual(self.inv.current_value_date, date(2021, 12, 31))
        self.assertEqual(str(self.inv.surrender_charge), "500.00")    # policy year 1 at that date
        self.assertEqual(str(self.inv.yearly_cost), "212.00")
        self.assertIn("projections", r.data["summary"]["plan"])

    def test_bad_plan_is_rejected_and_others_cant_paste(self):
        r = self.client.patch(f"/api/fp/investments/{self.inv.id}/", {"plan": {"face": "x"}}, format="json")
        self.assertEqual(r.status_code, 400)
        other = get_user_model().objects.create_user(email="pp2@example.test", username="pp2", password="x-Test-12345")
        c = APIClient()
        c.force_authenticate(other)
        self.assertEqual(c.patch(f"/api/fp/investments/{self.inv.id}/", {"plan": PLAN}, format="json").status_code, 404)

    def test_clearing_the_plan(self):
        self.client.patch(f"/api/fp/investments/{self.inv.id}/", {"plan": PLAN}, format="json")
        r = self.client.patch(f"/api/fp/investments/{self.inv.id}/", {"plan": {}}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("plan", r.data["summary"])
