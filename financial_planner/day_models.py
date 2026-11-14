"""
My day: the user's week (working hours, asked each Sunday), goals that need
time (study for an exam, the gym), things to do, and each day's plan in time
slots. Times are the user's local times (DayProfile.timezone).
"""
from django.conf import settings
from django.db import models


class DayProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="day_profile")
    timezone = models.CharField(max_length=60, default="America/Toronto")
    wake_time = models.TimeField(default="07:00")
    bed_time = models.TimeField(default="23:00")
    evening_email = models.BooleanField(default=True)       # "plan tomorrow" email each evening


class WorkWeek(models.Model):
    """Working hours for one week (Monday to Sunday), asked for each Sunday.
    days: {"0": [["09:00", "17:00"]], ..., "6": []} with 0 = Monday."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="work_weeks")
    week_start = models.DateField()                         # a Monday
    days = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["user", "week_start"]
        ordering = ["-week_start"]


class TimeGoal(models.Model):
    """Something that needs time regularly, e.g. 'AZ-900: 60 minutes a day until Jan 15'."""
    WHEN = [("any", "Any time"), ("morning", "Morning"), ("evening", "Evening")]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="time_goals")
    name = models.CharField(max_length=80)
    minutes_per_day = models.PositiveSmallIntegerField(default=60)
    weekdays = models.JSONField(default=list)               # [0..6]; empty = every day
    deadline = models.DateField(null=True, blank=True)
    when = models.CharField(max_length=10, choices=WHEN, default="any")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["deadline", "created_at"]


class DayTask(models.Model):
    """A thing to do on a day (or someday), with how long it takes and,
    optionally, a fixed time ('dentist at 3:00')."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="day_tasks")
    title = models.CharField(max_length=120)
    minutes = models.PositiveSmallIntegerField(default=30)
    date = models.DateField(null=True, blank=True)
    at = models.TimeField(null=True, blank=True)
    done = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "at", "created_at"]


class DayPlan(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="day_plans")
    date = models.DateField()
    wake_time = models.TimeField()
    bed_time = models.TimeField()
    working = models.BooleanField(default=False)
    work = models.JSONField(default=list)                   # [["09:00", "17:00"], ...]
    unplaced = models.JSONField(default=list)               # what didn't fit
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["user", "date"]


class Slot(models.Model):
    KINDS = [("routine", "Routine"), ("work", "Work"), ("goal", "Goal"), ("task", "Task"), ("other", "Other")]
    plan = models.ForeignKey(DayPlan, on_delete=models.CASCADE, related_name="slots")
    start = models.TimeField()
    end = models.TimeField()
    title = models.CharField(max_length=120)
    kind = models.CharField(max_length=10, choices=KINDS, default="task")
    goal = models.ForeignKey(TimeGoal, on_delete=models.SET_NULL, null=True, blank=True, related_name="slots")
    task = models.ForeignKey(DayTask, on_delete=models.SET_NULL, null=True, blank=True, related_name="slots")
    done = models.BooleanField(default=False)
    # Minutes from the start of the plan's date, so slots after midnight sort last.
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "start", "id"]
