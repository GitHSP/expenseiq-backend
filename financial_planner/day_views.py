"""
My day API (/api/fp/day/): the overview the page and Home card use, the user's
week, goals, things to do, building a day's plan, ticking slots, and the
twice-a-day job a GitHub Actions schedule calls (morning reminders, evening
"plan tomorrow" email).
"""
import hmac
import os
import re
from datetime import date, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import day_scheduler as scheduler
from .day_models import DayPlan, DayProfile, DayTask, Slot, SmokeLog, TimeGoal, WorkWeek

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class Bad(ValueError):
    pass


# ── helpers ──

def profile(user):
    return DayProfile.objects.get_or_create(user=user)[0]


def local_now(user):
    try:
        tz = ZoneInfo(profile(user).timezone)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("America/Toronto")
    return timezone.now().astimezone(tz)


def monday(d):
    return d - timedelta(days=d.weekday())


def hhmm(t):
    return t.strftime("%H:%M") if t else None


def _time(v, field):
    if isinstance(v, str) and TIME_RE.match(v.strip()):
        h, m = v.strip().split(":")
        return time(int(h), int(m))
    raise Bad(f"{field} must be a time like 07:30.")


def _date(v, field):
    try:
        return date.fromisoformat(str(v))
    except (TypeError, ValueError):
        raise Bad(f"{field} must be a date like 2026-11-15.")


def _blocks(v, field="work"):
    if not isinstance(v, list) or len(v) > 4:
        raise Bad(f"{field} must be a list of [start, end] times.")
    out = []
    for pair in v:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise Bad(f"{field} must be a list of [start, end] times.")
        s, e = _time(pair[0], field), _time(pair[1], field)
        if e == s:
            raise Bad("Work can't start and end at the same time.")   # an earlier end = after midnight
        out.append([hhmm(s), hhmm(e)])
    return out


def week_for(user, d):
    """That week's working hours, or the latest week before it (hours often repeat)."""
    ww = WorkWeek.objects.filter(user=user, week_start=monday(d)).first() or \
        WorkWeek.objects.filter(user=user, week_start__lt=monday(d)).first()
    return ww


def work_on(user, d):
    ww = week_for(user, d)
    return (ww.days.get(str(d.weekday())) or []) if ww else []


def goal_today(goal, d):
    if not goal.is_active or (goal.deadline and d > goal.deadline):
        return False
    return not goal.weekdays or d.weekday() in goal.weekdays


def slot_minutes(s):
    """Time spent on a slot: what the focus timer counted, else the slot's length once ticked."""
    if s.focused_minutes:
        return s.focused_minutes
    return scheduler.length(s.start, s.end) if s.done else 0


def done_minutes(goal, start, end):
    from django.db.models import Q
    return sum(slot_minutes(s) for s in Slot.objects.filter(goal=goal, plan__date__gte=start, plan__date__lte=end)
               .filter(Q(done=True) | Q(focused_minutes__gt=0)))


def smoke_json(user, today):
    """The smoke-free streak (today counts once it's tapped), the last 7 days,
    and roughly what the streak has saved (the usual daily spend on Smoke & Vape)."""
    from decimal import Decimal
    from expenses.models import Expense
    logs = {l.date: l.smoked for l in SmokeLog.objects.filter(user=user, date__gte=today - timedelta(days=400))}
    day = today if today in logs else today - timedelta(days=1)
    streak = 0
    while logs.get(day) is False:
        streak += 1
        day -= timedelta(days=1)
    best = run = 0
    prev = None
    for d in sorted(logs):                              # longest run of smoke-free days in a row
        if logs[d] is False:
            run = run + 1 if prev and d - prev == timedelta(days=1) and logs[prev] is False else 1
        else:
            run = 0
        best, prev = max(best, run), d
    since = today - timedelta(days=90)
    spent = sum((e.amount for e in Expense.objects.filter(user=user, category="Smoke & Vape", date__gte=since, date__lte=today)),
                Decimal("0"))
    per_day = spent / 90
    return {
        "used": bool(logs),
        "asked_today": today in logs,
        "smoked_today": logs.get(today),
        "streak": streak,
        "best": max(best, streak),
        "week": [{"date": (today - timedelta(days=i)).isoformat(), "smoked": logs.get(today - timedelta(days=i))}
                 for i in range(6, -1, -1)],
        "per_day": str(per_day.quantize(Decimal("0.01"))),
        "saved": str((per_day * streak).quantize(Decimal("0.01"))),
    }


def minutes_for(goal, d):
    """Today's minutes for a goal: its daily minutes plus what was missed yesterday."""
    if not goal_today(goal, d):
        return 0
    planned = Slot.objects.filter(goal=goal, plan__date=d - timedelta(days=1))
    missed = sum(scheduler.length(s.start, s.end) for s in planned if not s.done)
    return goal.minutes_per_day + min(missed, goal.minutes_per_day)


def goal_json(goal, today):
    wk = monday(today)
    days = [wk + timedelta(days=i) for i in range(7)]
    target = sum(goal.minutes_per_day for d in days if goal_today(goal, d))
    return {
        "id": goal.id, "name": goal.name, "minutes_per_day": goal.minutes_per_day, "weekdays": goal.weekdays,
        "deadline": goal.deadline.isoformat() if goal.deadline else None, "when": goal.when, "is_active": goal.is_active,
        "days_left": (goal.deadline - today).days if goal.deadline else None,
        "week_done": done_minutes(goal, wk, wk + timedelta(days=6)), "week_target": target,
        "total_done": done_minutes(goal, date(2000, 1, 1), date(2100, 1, 1)),
    }


def plan_json(plan):
    if not plan:
        return None
    return {
        "date": plan.date.isoformat(), "wake_time": hhmm(plan.wake_time), "bed_time": hhmm(plan.bed_time),
        "working": plan.working, "work": plan.work, "unplaced": plan.unplaced,
        "slots": [{"id": s.id, "start": hhmm(s.start), "end": hhmm(s.end), "title": s.title, "kind": s.kind,
                   "start_min": s.order, "end_min": s.order + scheduler.length(s.start, s.end),
                   "focused_minutes": s.focused_minutes,
                   "goal": s.goal_id, "task": s.task_id, "done": s.done} for s in plan.slots.all()],
    }


def task_json(t):
    return {"id": t.id, "title": t.title, "minutes": t.minutes, "date": t.date.isoformat() if t.date else None,
            "at": hhmm(t.at), "done": t.done}


@transaction.atomic
def make_plan(user, d, wake, bed, working, work):
    """Build (or rebuild) a day's plan. Slots already ticked keep their tick."""
    old = DayPlan.objects.filter(user=user, date=d).first()
    was_done = set()
    if old:
        was_done = {(s.title, s.kind) for s in old.slots.filter(done=True)}
        old.delete()
    work = work if working else []
    tasks = list(DayTask.objects.filter(user=user, date=d, done=False))
    fixed = [(t.id, t.title, t.at, t.minutes) for t in tasks if t.at]
    loose = [(t.id, t.title, t.minutes) for t in tasks if not t.at]
    goals = [(g.id, g.name, minutes_for(g, d), g.when) for g in TimeGoal.objects.filter(user=user, is_active=True)]
    goals = [g for g in goals if g[2] > 0]
    slots, unplaced = scheduler.build(wake, bed, work=work, fixed=fixed, goals=goals, tasks=loose)
    plan = DayPlan.objects.create(user=user, date=d, wake_time=wake, bed_time=bed, working=bool(work), work=work,
                                  unplaced=[{"kind": k, "title": title, "minutes": m} for k, _, title, m in unplaced])
    for s in slots:
        Slot.objects.create(plan=plan, start=scheduler.to_time(s["start"]), end=scheduler.to_time(s["end"]),
                            title=s["title"], kind=s["kind"], order=s["start"],
                            goal_id=s["key"] if s["kind"] == "goal" else None,
                            task_id=s["key"] if s["kind"] == "task" else None,
                            done=(s["title"], s["kind"]) in was_done)
    return plan


def overview(user):
    now = local_now(user)
    today, tomorrow = now.date(), now.date() + timedelta(days=1)
    prof = profile(user)
    plans = {p.date: p for p in DayPlan.objects.filter(user=user, date__in=[today, tomorrow]).prefetch_related("slots")}
    this_week = WorkWeek.objects.filter(user=user, week_start=monday(today)).first()
    next_monday = monday(today) + timedelta(days=7)
    next_week = WorkWeek.objects.filter(user=user, week_start=next_monday).first()
    has_any_week = WorkWeek.objects.filter(user=user).exists()
    return {
        "now": now.strftime("%Y-%m-%dT%H:%M"),
        "today": today.isoformat(), "tomorrow": tomorrow.isoformat(),
        "profile": {"timezone": prof.timezone, "wake_time": hhmm(prof.wake_time), "bed_time": hhmm(prof.bed_time),
                    "evening_email": prof.evening_email},
        "week": {"start": monday(today).isoformat(), "days": this_week.days if this_week else None},
        "next_week": {"start": next_monday.isoformat(), "days": next_week.days if next_week else None},
        "tomorrow_work": work_on(user, tomorrow),
        "goals": [goal_json(g, today) for g in TimeGoal.objects.filter(user=user)],
        "tasks": [task_json(t) for t in DayTask.objects.filter(user=user, done=False).exclude(date__lt=today)[:50]],
        "plans": {"today": plan_json(plans.get(today)), "tomorrow": plan_json(plans.get(tomorrow))},
        "smoke": smoke_json(user, today),
        "prompts": {
            # Each Sunday (or the first time): the coming week's working hours.
            "set_week": (not has_any_week) or (today.weekday() == 6 and next_week is None),
            "set_week_start": (next_monday if today.weekday() == 6 or not this_week else monday(today)).isoformat(),
            # From 5 PM: plan tomorrow, if not planned yet.
            "plan_tomorrow": now.hour >= 17 and tomorrow not in plans,
        },
    }


class DayView(APIView):
    """GET /api/fp/day/ - everything the My day page and the Home card show."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(overview(request.user))


class ProfileView(APIView):
    """PUT /api/fp/day/profile/ {timezone?, wake_time?, bed_time?, evening_email?}"""
    permission_classes = [IsAuthenticated]

    def put(self, request):
        prof = profile(request.user)
        d = request.data
        try:
            if "timezone" in d:
                ZoneInfo(str(d["timezone"]))
                prof.timezone = str(d["timezone"])[:60]
            if "wake_time" in d:
                prof.wake_time = _time(d["wake_time"], "wake_time")
            if "bed_time" in d:
                prof.bed_time = _time(d["bed_time"], "bed_time")
            if "evening_email" in d:
                prof.evening_email = bool(d["evening_email"])
        except (Bad, ZoneInfoNotFoundError, ValueError) as e:
            return Response({"error": str(e) or "Unknown timezone."}, status=status.HTTP_400_BAD_REQUEST)
        prof.save()
        return Response(overview(request.user))


class WeekView(APIView):
    """PUT /api/fp/day/week/ {week_start, days: {"0": [["09:00","17:00"]], ...}}"""
    permission_classes = [IsAuthenticated]

    def put(self, request):
        try:
            start = monday(_date(request.data.get("week_start"), "week_start"))
            raw = request.data.get("days")
            if not isinstance(raw, dict):
                raise Bad("days must be an object.")
            days = {str(i): _blocks(raw.get(str(i), []), DAY_NAMES[i]) for i in range(7)}
        except Bad as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        WorkWeek.objects.update_or_create(user=request.user, week_start=start, defaults={"days": days})
        return Response(overview(request.user))


def _goal_fields(d, partial=False):
    out = {}
    if "name" in d or not partial:
        name = str(d.get("name", "")).strip()[:80]
        if len(name) < 2:
            raise Bad("Give the goal a name.")
        out["name"] = name
    if "minutes_per_day" in d or not partial:
        try:
            m = int(d.get("minutes_per_day", 60))
        except (TypeError, ValueError):
            raise Bad("minutes_per_day must be a number.")
        if not 5 <= m <= 600:
            raise Bad("Minutes a day must be between 5 and 600.")
        out["minutes_per_day"] = m
    if "weekdays" in d:
        w = d["weekdays"]
        if not isinstance(w, list) or not all(isinstance(x, int) and 0 <= x <= 6 for x in w):
            raise Bad("weekdays must be a list of 0 (Monday) to 6 (Sunday).")
        out["weekdays"] = sorted(set(w))
    if "deadline" in d:
        out["deadline"] = _date(d["deadline"], "deadline") if d["deadline"] else None
    if "when" in d:
        if d["when"] not in ("any", "morning", "evening"):
            raise Bad("when must be any, morning or evening.")
        out["when"] = d["when"]
    if "is_active" in d:
        out["is_active"] = bool(d["is_active"])
    return out


class GoalListView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            TimeGoal.objects.create(user=request.user, **_goal_fields(request.data))
        except Bad as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(overview(request.user), status=status.HTTP_201_CREATED)


class GoalDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        goal = TimeGoal.objects.filter(user=request.user, pk=pk).first()
        if not goal:
            return Response({"error": "Goal not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            for k, v in _goal_fields(request.data, partial=True).items():
                setattr(goal, k, v)
        except Bad as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        goal.save()
        return Response(overview(request.user))

    def delete(self, request, pk):
        TimeGoal.objects.filter(user=request.user, pk=pk).delete()
        return Response(overview(request.user))


def _task_fields(d, partial=False):
    out = {}
    if "title" in d or not partial:
        title = str(d.get("title", "")).strip()[:120]
        if not title:
            raise Bad("What's the task?")
        out["title"] = title
    if "minutes" in d or not partial:
        try:
            m = int(d.get("minutes", 30))
        except (TypeError, ValueError):
            raise Bad("minutes must be a number.")
        if not 5 <= m <= 720:
            raise Bad("A task takes between 5 and 720 minutes.")
        out["minutes"] = m
    if "date" in d:
        out["date"] = _date(d["date"], "date") if d["date"] else None
    if "at" in d:
        out["at"] = _time(d["at"], "at") if d["at"] else None
    if "done" in d:
        out["done"] = bool(d["done"])
    return out


class TaskListView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            DayTask.objects.create(user=request.user, **_task_fields(request.data))
        except Bad as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(overview(request.user), status=status.HTTP_201_CREATED)


class TaskDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        task = DayTask.objects.filter(user=request.user, pk=pk).first()
        if not task:
            return Response({"error": "Task not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            for k, v in _task_fields(request.data, partial=True).items():
                setattr(task, k, v)
        except Bad as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        task.save()
        return Response(overview(request.user))

    def delete(self, request, pk):
        DayTask.objects.filter(user=request.user, pk=pk).delete()
        return Response(overview(request.user))


class PlanView(APIView):
    """POST /api/fp/day/plan/ {date, wake_time, bed_time, working, work?} - build that day's
    slots from the week's work hours (or the ones given), goals and its tasks."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        d = request.data
        prof = profile(request.user)
        try:
            day = _date(d.get("date"), "date")
            wake = _time(d["wake_time"], "wake_time") if d.get("wake_time") else prof.wake_time
            bed = _time(d["bed_time"], "bed_time") if d.get("bed_time") else prof.bed_time
            work = _blocks(d["work"]) if d.get("work") is not None else work_on(request.user, day)
        except Bad as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        working = bool(d.get("working", bool(work)))
        today = local_now(request.user).date()
        if not today - timedelta(days=1) <= day <= today + timedelta(days=14):
            return Response({"error": "Plan a day from yesterday to two weeks ahead."}, status=status.HTTP_400_BAD_REQUEST)
        make_plan(request.user, day, wake, bed, working, work)
        return Response(overview(request.user))


class SlotView(APIView):
    """PATCH /api/fp/day/slots/<id>/ {done?, title?, start?, end?} · DELETE removes it.
    POST /api/fp/day/slots/ {date, title, start, end} adds one to that day's plan."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        d = request.data
        try:
            day = _date(d.get("date"), "date")
            start, end = _time(d.get("start"), "start"), _time(d.get("end"), "end")
            title = str(d.get("title", "")).strip()[:120]
            if not title or end == start:
                raise Bad("Give a title, and a start and end time.")
        except Bad as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        plan = DayPlan.objects.filter(user=request.user, date=day).first()
        if not plan:
            return Response({"error": "Plan that day first."}, status=status.HTTP_400_BAD_REQUEST)
        order = scheduler.to_min(start) + (24 * 60 if start < plan.wake_time else 0)
        Slot.objects.create(plan=plan, start=start, end=end, title=title, kind="other", order=order)
        return Response(overview(request.user), status=status.HTTP_201_CREATED)

    def patch(self, request, pk):
        slot = Slot.objects.filter(plan__user=request.user, pk=pk).select_related("task").first()
        if not slot:
            return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        d = request.data
        try:
            if "start" in d:
                slot.start = _time(d["start"], "start")
            if "end" in d:
                slot.end = _time(d["end"], "end")
            if slot.end == slot.start:
                raise Bad("A slot can't start and end at the same time.")
            slot.order = scheduler.to_min(slot.start) + (24 * 60 if slot.start < slot.plan.wake_time else 0)
            if "title" in d:
                slot.title = str(d["title"]).strip()[:120] or slot.title
        except Bad as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        if "focused_minutes" in d:                          # from the focus timer
            try:
                focused = int(d["focused_minutes"])
            except (TypeError, ValueError):
                return Response({"error": "focused_minutes must be a number."}, status=status.HTTP_400_BAD_REQUEST)
            slot.focused_minutes = max(0, min(focused, 720))
            # Most of the slot done counts as done.
            if slot.focused_minutes >= 0.8 * scheduler.length(slot.start, slot.end):
                d = {**d, "done": True}
        if "done" in d:
            slot.done = bool(d["done"])
            if slot.task:                                   # a task's slot ticked = the task is done
                slot.task.done = slot.done
                slot.task.save(update_fields=["done"])
        slot.save()
        return Response(overview(request.user))

    def delete(self, request, pk):
        Slot.objects.filter(plan__user=request.user, pk=pk).delete()
        return Response(overview(request.user))


class BackupView(APIView):
    """GET /api/fp/backup/ - all of the user's data as one file (see backup.py)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.http import HttpResponse
        from .backup import build_backup, filename
        response = HttpResponse(build_backup(request.user), content_type="application/gzip")
        response["Content-Disposition"] = f'attachment; filename="{filename(request.user)}"'
        response["Cache-Control"] = "no-store"
        return response


class SmokeView(APIView):
    """POST /api/fp/day/smoke/ {smoked: bool, date?} - today's (or a recent day's) answer."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        today = local_now(request.user).date()
        try:
            day = _date(request.data["date"], "date") if request.data.get("date") else today
        except Bad as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        if not today - timedelta(days=7) <= day <= today:
            return Response({"error": "Answer for today or the last week."}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(request.data.get("smoked"), bool):
            return Response({"error": "smoked must be true or false."}, status=status.HTTP_400_BAD_REQUEST)
        SmokeLog.objects.update_or_create(user=request.user, date=day, defaults={"smoked": request.data["smoked"]})
        return Response(overview(request.user))


# ── the twice-a-day job (GitHub Actions) ──

class CronView(APIView):
    """POST /api/fp/day/cron/ {job: "morning" | "evening"} with header X-Cron-Key.
    morning: the daily reminders email. evening: "plan tomorrow" emails."""
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        key = os.environ.get("CRON_KEY") or getattr(settings, "CRON_KEY", "")
        given = request.headers.get("X-Cron-Key", "")
        if not key or len(key) < 20 or not hmac.compare_digest(key, given):
            return Response(status=status.HTTP_404_NOT_FOUND)
        job = request.data.get("job") if isinstance(request.data, dict) else None
        if job == "morning":
            from io import StringIO
            from django.core.management import call_command
            out = StringIO()
            call_command("send_reminders", stdout=out)
            from .backup import send_backups                  # Sundays: the weekly backup
            return Response({"job": job, "result": out.getvalue().strip()[-200:], "backups": send_backups()})
        if job == "evening":
            from .day_emails import send_evening
            return Response({"job": job, "sent": send_evening()})
        return Response({"error": "job must be morning or evening."}, status=status.HTTP_400_BAD_REQUEST)
