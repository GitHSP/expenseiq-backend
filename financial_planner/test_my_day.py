from datetime import date, datetime, time, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from . import day_scheduler as sch
from .day_models import DayPlan, DayTask, Slot, TimeGoal


class SchedulerTests(TestCase):
    def test_fills_around_work(self):
        slots, unplaced = sch.build(time(7, 0), time(23, 0), work=[("09:00", "17:00")],
                                    fixed=[(1, "Dentist", time(18, 0), 60)],
                                    goals=[(1, "AZ-900", 60, "morning"), (2, "Read", 30, "evening")],
                                    tasks=[(2, "Groceries", 45)])
        by = {s["title"]: (sch.to_time(s["start"]).strftime("%H:%M"), sch.to_time(s["end"]).strftime("%H:%M")) for s in slots}
        self.assertEqual(by["Wake up & get ready"], ("07:00", "07:30"))
        self.assertEqual(by["AZ-900"], ("07:30", "08:30"))                 # morning goal: first free time
        self.assertEqual(by["Work"], ("09:00", "17:00"))
        self.assertEqual(by["Dentist"], ("18:00", "19:00"))
        self.assertEqual(by["Read"], ("22:30", "23:00"))                   # evening goal: just before bed
        self.assertEqual(by["Groceries"], ("17:00", "17:45"))
        self.assertEqual(unplaced, [])

    def test_too_much_is_reported_and_goals_split(self):
        slots, unplaced = sch.build(time(8, 0), time(11, 30), work=[("09:30", "10:30")],
                                    goals=[(1, "Study", 90, "any")], tasks=[(1, "Long job", 200)])
        study = [s for s in slots if s["title"] == "Study"]
        self.assertEqual(sum(s["end"] - s["start"] for s in study), 90)   # no 90-minute gap: 08:30-09:30 + 10:30-11:00
        self.assertEqual(len(study), 2)
        self.assertEqual(unplaced, [("task", 1, "Long job", 200)])


class PastMidnightTests(TestCase):
    def test_evening_shift_and_late_bedtime(self):
        slots, unplaced = sch.build(time(10, 0), time(1, 0), work=[("16:00", "00:00")],
                                    fixed=[(1, "Call family", time(0, 15), 30)],
                                    goals=[(1, "AZ-204", 60, "morning")], tasks=[(2, "Food", 45)])
        shown = [(sch.to_time(s["start"]).strftime("%H:%M"), sch.to_time(s["end"]).strftime("%H:%M"), s["title"]) for s in slots]
        self.assertEqual(shown, [("10:00", "10:30", "Wake up & get ready"), ("10:30", "11:30", "AZ-204"),
                                 ("11:40", "12:25", "Food"), ("16:00", "00:00", "Work"), ("00:15", "00:45", "Call family")])
        self.assertEqual(unplaced, [])
        self.assertEqual(sch.length(time(16), time(0)), 480)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class MyDayApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="day@example.test", username="day", password="x-Test-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        # Saturday 2026-11-14, 8 PM in Toronto
        self.now = datetime(2026, 11, 14, 20, 0, tzinfo=ZoneInfo("America/Toronto"))
        self.patch = mock.patch("financial_planner.day_views.timezone.now", return_value=self.now)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def get(self):
        return self.client.get("/api/fp/day/").data

    def test_week_goals_tasks_and_plan(self):
        d = self.get()
        self.assertTrue(d["prompts"]["set_week"])                           # first time: set your week
        self.assertTrue(d["prompts"]["plan_tomorrow"])                      # after 5 PM, tomorrow not planned
        week = {str(i): [["09:00", "17:00"]] if i < 5 else [] for i in range(7)}
        r = self.client.put("/api/fp/day/week/", {"week_start": "2026-11-16", "days": week}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.client.put("/api/fp/day/week/", {"week_start": "2026-11-09", "days": week}, format="json")
        self.assertFalse(self.get()["prompts"]["set_week"])

        self.client.post("/api/fp/day/goals/", {"name": "AZ-900", "minutes_per_day": 60, "deadline": "2027-01-15",
                                                "when": "morning"}, format="json")
        self.client.post("/api/fp/day/tasks/", {"title": "Groceries", "minutes": 45, "date": "2026-11-15"}, format="json")
        r = self.client.post("/api/fp/day/plan/", {"date": "2026-11-15", "wake_time": "08:00"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        plan = r.data["plans"]["tomorrow"]
        titles = [s["title"] for s in plan["slots"]]
        self.assertEqual(titles, ["Wake up & get ready", "AZ-900", "Groceries"])   # Sunday: no work
        self.assertFalse(r.data["prompts"]["plan_tomorrow"])

        # Tick the goal slot: it counts toward the week; tick the task: the task is done.
        goal_slot = next(s for s in plan["slots"] if s["title"] == "AZ-900")
        task_slot = next(s for s in plan["slots"] if s["title"] == "Groceries")
        self.client.patch(f"/api/fp/day/slots/{goal_slot['id']}/", {"done": True}, format="json")
        r = self.client.patch(f"/api/fp/day/slots/{task_slot['id']}/", {"done": True}, format="json")
        self.assertTrue(DayTask.objects.get().done)
        self.assertEqual(r.data["goals"][0]["total_done"], 60)

    def test_night_shift_plan_through_the_api(self):
        r = self.client.post("/api/fp/day/plan/", {"date": "2026-11-15", "wake_time": "10:00", "bed_time": "01:00",
                                                   "working": True, "work": [["16:00", "00:00"]]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        work = next(s for s in r.data["plans"]["tomorrow"]["slots"] if s["title"] == "Work")
        self.assertEqual((work["start"], work["end"], work["start_min"], work["end_min"]), ("16:00", "00:00", 960, 1440))

    def test_missed_goal_time_carries_to_the_next_day(self):
        goal = TimeGoal.objects.create(user=self.user, name="Study", minutes_per_day=60)
        plan = DayPlan.objects.create(user=self.user, date=date(2026, 11, 14), wake_time=time(7), bed_time=time(23))
        Slot.objects.create(plan=plan, start=time(8), end=time(9), title="Study", kind="goal", goal=goal)   # not done
        r = self.client.post("/api/fp/day/plan/", {"date": "2026-11-15"}, format="json")
        study = [s for s in r.data["plans"]["tomorrow"]["slots"] if s["title"] == "Study"]
        self.assertEqual(sum(int(s["end"][:2]) * 60 + int(s["end"][3:]) - int(s["start"][:2]) * 60 - int(s["start"][3:])
                             for s in study), 120)

    def test_validation_and_privacy(self):
        self.assertEqual(self.client.post("/api/fp/day/goals/", {"name": "X"}, format="json").status_code, 400)
        self.assertEqual(self.client.put("/api/fp/day/week/", {"week_start": "2026-11-16",
                         "days": {"0": [["17:00", "17:00"]]}}, format="json").status_code, 400)
        self.client.post("/api/fp/day/plan/", {"date": "2026-11-15"}, format="json")
        slot = Slot.objects.first()
        other = get_user_model().objects.create_user(email="d2@example.test", username="d2", password="x-Test-12345")
        c = APIClient()
        c.force_authenticate(other)
        self.assertEqual(c.patch(f"/api/fp/day/slots/{slot.id}/", {"done": True}, format="json").status_code, 404)

    def test_cron_needs_the_key_and_sends_the_evening_email(self):
        anon = APIClient()
        with mock.patch.dict("os.environ", {"CRON_KEY": "k" * 32}):
            self.assertEqual(anon.post("/api/fp/day/cron/", {"job": "evening"}, format="json").status_code, 404)
            self.assertEqual(anon.post("/api/fp/day/cron/", {"job": "evening"}, format="json",
                                       HTTP_X_CRON_KEY="wrong").status_code, 404)
            self.get()                                                       # creates the day profile
            r = anon.post("/api/fp/day/cron/", {"job": "evening"}, format="json", HTTP_X_CRON_KEY="k" * 32)
        self.assertEqual(r.data, {"job": "evening", "sent": 1})
        self.assertEqual(mail.outbox[0].subject, "Glacier: Plan your Sunday")
        with mock.patch.dict("os.environ", {"CRON_KEY": ""}):
            self.assertEqual(anon.post("/api/fp/day/cron/", {"job": "evening"}, format="json",
                                       HTTP_X_CRON_KEY="").status_code, 404)        # no key set: closed


class SmokeAndFocusTests(TestCase):
    def setUp(self):
        from decimal import Decimal
        from expenses.models import Expense
        self.user = get_user_model().objects.create_user(email="sf@example.test", username="sf", password="x-Test-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.now = datetime(2026, 11, 20, 9, 0, tzinfo=ZoneInfo("America/Toronto"))
        self.patch = mock.patch("financial_planner.day_views.timezone.now", return_value=self.now)
        self.patch.start()
        Expense.objects.create(user=self.user, title="Smokes", amount=Decimal("450"), category="Smoke & Vape", date=date(2026, 10, 1))

    def tearDown(self):
        self.patch.stop()

    def answer(self, day, smoked):
        return self.client.post("/api/fp/day/smoke/", {"date": day, "smoked": smoked}, format="json")

    def test_streak_best_and_saved(self):
        s = self.client.get("/api/fp/day/").data["smoke"]
        self.assertEqual((s["used"], s["streak"]), (False, 0))
        for day, smoked in (("2026-11-14", False), ("2026-11-15", False), ("2026-11-16", True),
                            ("2026-11-17", False), ("2026-11-18", False), ("2026-11-19", False)):
            self.answer(day, smoked)
        s = self.client.get("/api/fp/day/").data["smoke"]
        self.assertEqual(s["streak"], 3)                     # today not answered yet: counts to yesterday
        self.assertFalse(s["asked_today"])
        self.assertEqual(s["per_day"], "5.00")               # $450 over 90 days
        self.assertEqual(s["saved"], "15.00")
        s = self.answer("2026-11-20", False).data["smoke"]
        self.assertEqual((s["streak"], s["best"], s["asked_today"]), (4, 4, True))
        self.assertEqual([d["smoked"] for d in s["week"]], [False, False, True, False, False, False, False])
        s = self.answer("2026-11-20", True).data["smoke"]    # changed my mind
        self.assertEqual((s["streak"], s["best"]), (0, 3))           # the 17th to the 19th
        self.assertEqual(self.answer("2026-10-01", False).status_code, 400)   # too far back
        self.assertEqual(self.client.post("/api/fp/day/smoke/", {"smoked": "no"}, format="json").status_code, 400)

    def test_focus_timer_counts_real_minutes(self):
        goal = TimeGoal.objects.create(user=self.user, name="AZ-204", minutes_per_day=60)
        r = self.client.post("/api/fp/day/plan/", {"date": "2026-11-20", "wake_time": "07:00"}, format="json")
        slot = next(s for s in r.data["plans"]["today"]["slots"] if s["title"] == "AZ-204")
        r = self.client.patch(f"/api/fp/day/slots/{slot['id']}/", {"focused_minutes": 25}, format="json")
        after = next(s for s in r.data["plans"]["today"]["slots"] if s["id"] == slot["id"])
        self.assertEqual((after["focused_minutes"], after["done"]), (25, False))     # under 80%: not done yet
        self.assertEqual(r.data["goals"][0]["week_done"], 25)
        r = self.client.patch(f"/api/fp/day/slots/{slot['id']}/", {"focused_minutes": 52}, format="json")
        after = next(s for s in r.data["plans"]["today"]["slots"] if s["id"] == slot["id"])
        self.assertTrue(after["done"])                                                # 52 of 60: done
        self.assertEqual(r.data["goals"][0]["total_done"], 52)


class DebtPriorityTests(TestCase):
    def setUp(self):
        from decimal import Decimal
        from .models import Debt
        self.user = get_user_model().objects.create_user(email="pr@example.test", username="pr", password="x-Test-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        mk = lambda n, b, r: Debt.objects.create(user=self.user, name=n, current_balance=Decimal(b), annual_interest_rate=Decimal(r),
                                                 minimum_payment=Decimal("30"))
        self.high, self.mid, self.low = mk("High", "3000", "24"), mk("Mid", "2000", "20"), mk("Low", "500", "10")
        from .services import recalculate_avalanche_order
        recalculate_avalanche_order(self.user)

    def names(self, data):
        return [d["name"] for d in data]

    def test_own_order_and_back(self):
        from .outlook import _debts, simulate
        r = self.client.post("/api/fp/debts/priority/", {"order": [self.low.id, self.high.id, self.mid.id]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.names(r.data), ["Low", "High", "Mid"])
        # The extra money goes to Low first now.
        sim = simulate(_debts(self.user), budget=500, track=True)
        self.assertLess(sim["finish"][self.low.id], sim["finish"][self.mid.id])
        pay = sim["first_pay"]
        self.assertGreater(pay[self.low.id], max(pay[self.high.id], pay[self.mid.id]))   # Low gets the extra
        r = self.client.post("/api/fp/debts/priority/", {"order": None}, format="json")
        self.assertEqual(self.names(r.data), ["High", "Mid", "Low"])

    def test_bad_and_other_users(self):
        self.assertEqual(self.client.post("/api/fp/debts/priority/", {"order": "x"}, format="json").status_code, 400)
        other = get_user_model().objects.create_user(email="pr2@example.test", username="pr2", password="x-Test-12345")
        c = APIClient()
        c.force_authenticate(other)
        self.assertEqual(c.post("/api/fp/debts/priority/", {"order": [self.low.id]}, format="json").status_code, 404)

    def test_aurora(self):
        from assistant.tools import set_debt_order
        r = set_debt_order(self.user, ["mid"])
        self.assertTrue(r["ok"], r)
        self.assertIn("1. Mid, 2. High, 3. Low", r["message"])
        self.assertTrue(set_debt_order(self.user)["ok"])
        from .models import Debt
        self.assertEqual(list(Debt.objects.filter(user=self.user).order_by("avalanche_order").values_list("name", flat=True)),
                         ["High", "Mid", "Low"])
