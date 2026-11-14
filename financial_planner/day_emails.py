"""
The evening email (about 8 PM): plan tomorrow - or, if it's planned, what
tomorrow looks like - and on Sundays a nudge to set next week's working hours.
Same look as the reminder emails (financial_planner/reminder_email.py).
"""
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives

from financial_planner.reminder_email import _row, _section, page

from .day_models import DayPlan, DayProfile, DayTask, TimeGoal, WorkWeek
from .day_views import goal_today, local_now, minutes_for, monday, work_on

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _mins(m):
    h, r = divmod(int(m), 60)
    return f"{h} h {r} min" if h and r else f"{h} h" if h else f"{r} min"


def evening_email(user):
    """(subject, text, html) for one user, or None."""
    now = local_now(user)
    tomorrow = now.date() + timedelta(days=1)
    day = DAY_NAMES[tomorrow.weekday()]
    plan = DayPlan.objects.filter(user=user, date=tomorrow).prefetch_related("slots").first()
    app = getattr(settings, "FRONTEND_URL", "")
    name = (user.first_name or "").strip()
    parts, text = [], []

    if plan:
        subject = f"Your {day}: {plan.slots.count()} things planned"
        intro = f"here's your plan for tomorrow, {day}."
        rows = "".join(_row(s.title, s.kind.capitalize(), f"{s.start:%H:%M}–{s.end:%H:%M}") for s in plan.slots.all())
        parts.append(_section("Tomorrow", rows))
        text += [f"{s.start:%H:%M}-{s.end:%H:%M}  {s.title}" for s in plan.slots.all()]
    else:
        subject = f"Plan your {day}"
        intro = f"take two minutes to plan tomorrow, {day}. Glacier fits it around your work."
        work = work_on(user, tomorrow)
        goals = [(g, minutes_for(g, tomorrow)) for g in TimeGoal.objects.filter(user=user, is_active=True) if goal_today(g, tomorrow)]
        tasks = list(DayTask.objects.filter(user=user, date=tomorrow, done=False))
        rows = _row("Work", "from your week", ", ".join(f"{s}–{e}" for s, e in work) if work else "Day off")
        rows += "".join(_row(g.name, "goal", _mins(m)) for g, m in goals if m)
        rows += "".join(_row(t.title, f"at {t.at:%H:%M}" if t.at else "to do", _mins(t.minutes)) for t in tasks)
        parts.append(_section("Tomorrow so far", rows))
        text += [f"Work: {', '.join(f'{s}-{e}' for s, e in work) if work else 'day off'}"]
        text += [f"{g.name}: {_mins(m)}" for g, m in goals if m]
        text += [f"To do: {t.title} ({_mins(t.minutes)})" for t in tasks]

    if now.weekday() == 6 and not WorkWeek.objects.filter(user=user, week_start=monday(tomorrow)).exists():
        parts.append('<tr><td style="padding:16px 28px 0;font:600 14px/1.5 Arial,sans-serif;color:#b45309">'
                     "It's Sunday: set your working hours for the week ahead in My day.</td></tr>")
        text.append("It's Sunday: set your working hours for the week ahead in My day.")

    body = f"{'Hi ' + name + ',' if name else 'Hi,'}\n{intro}\n\n" + "\n".join(text) + f"\n\nOpen Glacier: {app}\n"
    return f"Glacier: {subject}", body, page(parts, app, name, intro)


def send_evening():
    sent = 0
    for prof in DayProfile.objects.filter(evening_email=True).select_related("user"):
        user = prof.user
        if not user.is_active or not user.email:
            continue
        built = evening_email(user)
        if not built:
            continue
        subject, text, html = built
        msg = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, [user.email])
        msg.attach_alternative(html, "text/html")
        try:
            msg.send()
            sent += 1
        except Exception:
            pass
    return sent
