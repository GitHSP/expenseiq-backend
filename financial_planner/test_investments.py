from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .investments import add_months, months_between, summarize
from .models import Debt, Investment, InvestmentPayment, RecurringItem


def make_user(email="inv@example.test"):
    return get_user_model().objects.create_user(email=email, username=email.split("@")[0], password="x-Test-12345")


class InvestmentMathTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def make(self, **kw):
        return Investment.objects.create(user=self.user, **kw)

    def test_month_helpers(self):
        self.assertEqual(months_between(date(2026, 1, 15), date(2026, 3, 14)), 1)
        self.assertEqual(months_between(date(2026, 1, 15), date(2026, 3, 15)), 2)
        self.assertEqual(months_between(date(2026, 5, 1), date(2026, 3, 1)), 0)
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))

    def test_rd_matches_bank_quarterly_compounding(self):
        # ₹1,000 a month for 12 months at 7%, compounded quarterly.
        rd = self.make(kind="rd", name="RD", currency="INR", start_date=date(2025, 1, 5),
                       payment=Decimal("1000"), rate=Decimal("7"), months=12)
        s = summarize(rd, today=date(2025, 6, 10))
        self.assertEqual(s["calculated_maturity"], "12462.13")
        self.assertEqual(s["payments_made"], 6)                    # Jan..Jun
        self.assertEqual(s["still_to_pay"], "6000.00")
        self.assertEqual(s["maturity_date"], "2026-01-05")
        self.assertGreater(Decimal(s["value_now"]), Decimal("6000"))
        later = summarize(rd, today=date(2026, 2, 1))
        self.assertTrue(later["matured"])
        self.assertEqual(later["worth"], later["maturity"])

    def test_rd_quoted_maturity_wins(self):
        rd = self.make(kind="rd", name="RD", currency="INR", start_date=date(2025, 1, 5),
                       payment=Decimal("1000"), rate=Decimal("7"), months=12, maturity_value=Decimal("12500"))
        self.assertEqual(summarize(rd, today=date(2025, 2, 1))["maturity"], "12500.00")

    def test_rd_biweekly_and_weekly_deposits(self):
        rd = self.make(kind="rd", name="TFSA", start_date=date(2026, 1, 1), payment=Decimal("100"),
                       payment_frequency="biweekly", rate=Decimal("0"), months=12)
        s = summarize(rd, today=date(2026, 2, 1))
        self.assertEqual(s["payments_made"], 3)                    # Jan 1, 15, 29
        self.assertEqual(s["payments_total"], 27)                  # Jan 1, 2026 to Dec 31, 2026
        self.assertEqual(s["maturity"], "2700.00")
        rd.payment_frequency = "weekly"
        s = summarize(rd, today=date(2026, 2, 1))
        self.assertEqual(s["payments_made"], 5)
        self.assertEqual(s["payments_total"], 53)

    def test_rd_weekly_interest_is_close_to_monthly(self):
        # About the same money in over the year, so about the same interest.
        weekly = summarize(self.make(kind="rd", name="W", start_date=date(2026, 1, 1), payment=Decimal("100"),
                                     payment_frequency="weekly", rate=Decimal("6"), compounding="monthly",
                                     months=12), today=date(2026, 1, 2))
        interest = Decimal(weekly["interest"])
        self.assertGreater(interest, Decimal("150"))
        self.assertLess(interest, Decimal("190"))

    def test_rd_without_an_end_date(self):
        rd = self.make(kind="rd", name="Wealthsimple", start_date=date(2026, 1, 1), payment=Decimal("50"),
                       payment_frequency="weekly", rate=Decimal("0"))
        s = summarize(rd, today=date(2026, 1, 29))
        self.assertEqual(s["payments_made"], 5)
        self.assertEqual(s["worth"], "250.00")
        self.assertIsNone(s["maturity"])
        self.assertIsNone(s["still_to_pay"])
        self.assertFalse(s["matured"])

    def test_rd_balance_from_the_app_replaces_the_estimate(self):
        rd = self.make(kind="rd", name="Wealthsimple", start_date=date(2026, 1, 1), payment=Decimal("50"),
                       payment_frequency="weekly", rate=Decimal("0"),
                       current_value=Decimal("1000"), current_value_date=date(2026, 1, 15))
        s = summarize(rd, today=date(2026, 1, 29))
        self.assertEqual(s["value_now"], "1100.00")                # + Jan 22 and Jan 29
        self.assertEqual(s["paid_so_far"], "250.00")
        self.assertEqual(s["value_basis"], "statement")

    def test_insurance_estimate_and_statement_value(self):
        pol = self.make(kind="insurance", name="UL", currency="CAD", start_date=date(2020, 3, 1),
                        payment=Decimal("100"), payment_frequency="monthly", rate=Decimal("0"),
                        paying_years=10, term_years=20)
        s = summarize(pol, today=date(2026, 2, 15))
        self.assertEqual(s["payments_made"], 72)                   # Mar 2020 .. Feb 2026
        self.assertEqual(s["paid_so_far"], "7200.00")
        self.assertEqual(s["payments_left"], 48)
        self.assertEqual(s["projected_maturity"], "12000.00")      # 0%: just the premiums
        self.assertEqual(s["value_basis"], "estimate")
        self.assertEqual(s["maturity_date"], "2040-03-01")

        pol.current_value, pol.current_value_date, pol.rate = Decimal("5000"), date(2026, 2, 1), Decimal("0")
        s = summarize(pol, today=date(2026, 2, 15))
        self.assertEqual(s["value_basis"], "statement")
        self.assertEqual(s["worth"], "5000.00")
        self.assertEqual(s["projected_maturity"], "9800.00")       # 5,000 + 48 more premiums

    def test_insurance_growth(self):
        pol = self.make(kind="insurance", name="Endow", currency="INR", start_date=date(2020, 1, 1),
                        payment=Decimal("12000"), payment_frequency="yearly", rate=Decimal("5"),
                        paying_years=1, term_years=2)
        s = summarize(pol, today=date(2020, 6, 1))
        self.assertAlmostEqual(float(s["projected_maturity"]), 12000 * 1.05 ** 2, delta=0.05)

    def test_chit_prized_with_deposit(self):
        chit = self.make(kind="chit", name="KSFE", currency="INR", start_date=date(2026, 1, 10),
                         payment=Decimal("5000"), months=20, chit_value=Decimal("100000"),
                         prized=True, prized_date=date(2026, 3, 10), prize_amount=Decimal("80000"),
                         deposit_where="chit_company", deposit_amount=Decimal("80000"),
                         deposit_rate=Decimal("7.3"), deposit_start=date(2026, 3, 10))
        for month, amount in ((1, 4200), (2, 4300), (3, 4500)):
            InvestmentPayment.objects.create(investment=chit, date=date(2026, month, 10), amount=Decimal(amount))
        s = summarize(chit, today=date(2026, 4, 9))
        self.assertEqual(s["paid_so_far"], "13000.00")
        self.assertEqual(s["payments_left"], 17)
        self.assertEqual(s["per_month_estimate"], "4333.33")
        self.assertEqual(s["still_to_pay"], "73666.67")
        self.assertEqual(s["months_behind"], 0)
        self.assertEqual(s["deposit_interest_so_far"], "480.00")   # 80,000 × 7.3% × 30 days
        self.assertEqual(s["end_date"], "2027-08-10")
        self.assertEqual(Decimal(s["worth"]), Decimal("80480.00") - Decimal("73666.67"))

    def test_chit_not_prized_counts_paid_in(self):
        chit = self.make(kind="chit", name="Chit", currency="INR", start_date=date(2026, 1, 10),
                         payment=Decimal("5000"), months=10, chit_value=Decimal("50000"))
        InvestmentPayment.objects.create(investment=chit, date=date(2026, 1, 10), amount=Decimal("4000"))
        s = summarize(chit, today=date(2026, 3, 15))
        self.assertEqual(s["worth"], "4000.00")
        self.assertEqual(s["months_behind"], 2)
        self.assertEqual(s["still_to_pay"], "36000.00")            # 9 more at the recent 4,000


class InvestmentApiTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_create_log_payments_and_net_worth(self):
        res = self.client.post("/api/fp/investments/", {
            "kind": "chit", "name": "Chit", "currency": "inr", "start_date": "2026-01-10",
            "payment": "5000", "months": 10, "chit_value": "50000"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["currency"], "INR")
        pk = res.data["id"]
        res = self.client.post(f"/api/fp/investments/{pk}/payments/", {"date": "2026-01-10", "amount": "4100"},
                               format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["summary"]["paid_so_far"], "4100.00")
        pay_id = res.data["payments"][0]["id"]
        res = self.client.patch(f"/api/fp/investments/{pk}/payments/{pay_id}/", {"amount": "4200"}, format="json")
        self.assertEqual(res.data["summary"]["paid_so_far"], "4200.00")

        Debt.objects.create(user=self.user, name="Card", debt_type="credit_card", current_balance=Decimal("500"),
                            annual_interest_rate=Decimal("20"),
                            minimum_payment=Decimal("25"))
        res = self.client.get("/api/fp/net-worth/")
        self.assertEqual(res.data["investments"][0]["worth"], "4200.00")
        self.assertEqual(res.data["investments"][0]["currency"], "INR")
        self.assertEqual(res.data["debts"][0]["balance"], "500.00")

        res = self.client.delete(f"/api/fp/investments/{pk}/payments/{pay_id}/")
        self.assertEqual(res.data["payments"], [])

    def test_validation_and_privacy(self):
        res = self.client.post("/api/fp/investments/", {"kind": "fd", "name": "FD", "currency": "INR",
                                                         "start_date": "2026-01-01", "payment": "1000"}, format="json")
        self.assertEqual(res.status_code, 400)                     # no tenure
        other = make_user("other@example.test")
        rec = RecurringItem.objects.create(user=other, label="x", amount=1)
        res = self.client.post("/api/fp/investments/", {"kind": "rd", "name": "RD", "currency": "INR", "months": 12,
                                                         "start_date": "2026-01-01", "payment": "1000",
                                                         "recurring_item": rec.id}, format="json")
        self.assertEqual(res.status_code, 400)
        inv = Investment.objects.create(user=other, kind="rd", name="Theirs", start_date=date(2026, 1, 1),
                                        payment=1, months=1)
        self.assertEqual(self.client.get(f"/api/fp/investments/{inv.id}/").status_code, 404)
        self.assertEqual(self.client.post(f"/api/fp/investments/{inv.id}/payments/",
                                          {"date": "2026-01-01", "amount": "1"}).status_code, 404)


class AveragePayTests(TestCase):
    def setUp(self):
        from expenses.models import Account, Statement, StatementTransaction
        self.user = make_user()
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        acc = Account.objects.create(user=self.user, name="Chequing", kind="chequing")
        st = Statement.objects.create(user=self.user, account=acc, filename="s.pdf",
                                      period_start=date(2026, 8, 1), period_end=date(2026, 9, 30),
                                      new_balance=Decimal("1500"))
        for i, amt in enumerate((900, 1000, 1100, 1200, 1300)):          # the oldest drops out
            StatementTransaction.objects.create(user=self.user, statement=st, account=acc,
                                                date=date(2026, 8, 1 + i * 7), description="PAYROLL DEP ACME CO",
                                                title="Acme pay", amount=Decimal(amt), kind="income")

    def test_average_of_last_four_deposits(self):
        res = self.client.post("/api/fp/recurring/", {"label": "Pay", "amount": "1", "category": "income",
                                                       "frequency": "biweekly", "anchor_date": "2026-10-02",
                                                       "average_from": "acme"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        rec = RecurringItem.objects.get(pk=res.data["id"])
        self.assertEqual(rec.amount, Decimal("1150.00"))
        self.assertEqual(rec.average_of, 4)

    def test_net_worth_bank_balance(self):
        res = self.client.get("/api/fp/net-worth/")
        self.assertEqual(res.data["banks"][0]["balance"], "1500.00")
        self.assertEqual(res.data["banks"][0]["as_of"], "2026-09-30")


class InsuranceCostTests(TestCase):
    def test_yearly_cost_and_surrender_charge(self):
        user = make_user("cost@example.test")
        pol = Investment.objects.create(user=user, kind="insurance", name="UL", currency="CAD",
                                        start_date=date(2020, 1, 1), payment=Decimal("100"), rate=Decimal("0"),
                                        term_years=7, current_value=Decimal("4000"),
                                        current_value_date=date(2026, 1, 1), yearly_cost=Decimal("360"),
                                        surrender_charge=Decimal("4500"))
        s = summarize(pol, today=date(2026, 1, 15))
        self.assertEqual(s["value_now"], "4000.00")
        self.assertEqual(s["surrender_value"], "0.00")              # the charge is more than the fund
        self.assertEqual(s["worth"], "0.00")
        self.assertEqual(s["projected_maturity"], "4840.00")       # + 12 × (100 premium − 30 cost)
        pol.surrender_charge = Decimal("1000")
        self.assertEqual(summarize(pol, today=date(2026, 1, 15))["worth"], "3000.00")
        pol.surrender_charge = None
        self.assertEqual(summarize(pol, today=date(2026, 1, 15))["worth"], "4000.00")

    def test_expected_value_points(self):
        user = make_user("exp@example.test")
        pol = Investment.objects.create(user=user, kind="insurance", name="UL", currency="CAD",
                                        start_date=date(2020, 1, 1), payment=Decimal("100"), rate=Decimal("2"),
                                        term_years=20, current_value=Decimal("4000"),
                                        current_value_date=date(2026, 1, 1), yearly_cost=Decimal("360"))
        s = summarize(pol, today=date(2026, 1, 15))
        self.assertEqual(s["expected_rates"], ["0", "2", "4"])
        self.assertEqual([p["date"] for p in s["expected"]], ["2027-01-01", "2031-01-01", "2036-01-01", "2040-01-01"])
        self.assertEqual(s["expected"][0]["values"][0], "4840.00")      # 0%: + 12 × (100 − 30)
        low, mid, high = (Decimal(v) for v in s["expected"][-1]["values"])
        self.assertTrue(low < mid < high)
        self.assertEqual(s["monthly_split"], {"premium": "100.00", "cost": "30.00", "invested": "70.00"})


class FixedDepositTests(TestCase):
    def setUp(self):
        self.user = make_user("fd@example.test")

    def test_cumulative_fd(self):
        fd = Investment.objects.create(user=self.user, kind="fd", name="FD", currency="INR",
                                       start_date=date(2026, 1, 1), payment=Decimal("100000"),
                                       rate=Decimal("7"), months=12)
        s = summarize(fd, today=date(2026, 7, 2))
        self.assertEqual(s["maturity"], "107185.90")               # 1,00,000 × 1.0175⁴
        self.assertEqual(s["maturity_date"], "2027-01-01")
        self.assertGreater(Decimal(s["value_now"]), Decimal("103000"))
        self.assertLess(Decimal(s["value_now"]), Decimal("104000"))
        self.assertEqual(summarize(fd, today=date(2027, 2, 1))["worth"], "107185.90")

    def test_payout_fd(self):
        fd = Investment.objects.create(user=self.user, kind="fd", name="FD", currency="INR",
                                       start_date=date(2026, 1, 1), payment=Decimal("100000"),
                                       rate=Decimal("7"), months=12, interest_payout="quarterly")
        s = summarize(fd, today=date(2026, 7, 15))
        self.assertEqual(s["interest_per_payout"], "1750.00")
        self.assertEqual(s["payouts_received"], 2)
        self.assertEqual(s["interest_received"], "3500.00")
        self.assertEqual(s["interest"], "7000.00")
        self.assertEqual(s["worth"], "100000.00")                  # the interest went to your account

    def test_api_needs_amount_and_tenure(self):
        client = APIClient()
        client.force_authenticate(self.user)
        res = client.post("/api/fp/investments/", {"kind": "fd", "name": "FD", "currency": "INR",
                                                   "start_date": "2026-01-01", "months": 12}, format="json")
        self.assertEqual(res.status_code, 400)
        res = client.post("/api/fp/investments/", {"kind": "fd", "name": "FD", "currency": "INR", "rate": "7",
                                                   "start_date": "2026-01-01", "months": 12, "payment": "50000"},
                          format="json")
        self.assertEqual(res.status_code, 201, res.data)


class InsuranceValidationTests(TestCase):
    def test_unlikely_years_and_costs_are_refused(self):
        client = APIClient()
        client.force_authenticate(make_user("val@example.test"))
        base = {"kind": "insurance", "name": "UL", "currency": "CAD", "start_date": "2026-10-28", "payment": "100"}
        post = lambda **kw: client.post("/api/fp/investments/", {**base, **kw}, format="json").status_code
        self.assertEqual(post(paying_years=1272), 400)
        self.assertEqual(post(paying_years=30, term_years=20), 400)
        self.assertEqual(post(yearly_cost="5000"), 400)
        self.assertEqual(post(yearly_cost="700", term_years=40), 201)
