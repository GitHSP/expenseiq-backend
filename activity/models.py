# activity/models.py
#
# A permanent, per-user history of changes to money-related records, so the
# user can see how a debt balance (or anything else) got to where it is.

from django.conf import settings
from django.db import models


class ActivityLog(models.Model):
    AREAS = [
        ("debt", "Debt"),
        ("checklist", "Checklist"),
        ("recurring", "Recurring payment"),
        ("fund", "Emergency fund"),
        ("expense", "Expense"),
        ("income", "Income"),
        ("import", "Statement import"),
        ("plan", "Monthly plan"),
    ]
    SOURCES = [
        ("app", "You"),
        ("assistant", "AI assistant"),
        ("import", "Statement import"),
        ("system", "Automatic"),
    ]

    user       = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="activity")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    area       = models.CharField(max_length=20, choices=AREAS)
    action     = models.CharField(max_length=20)   # created / updated / deleted / ticked / unticked / interest / imported / started
    title      = models.CharField(max_length=255)  # what it's about, e.g. the debt's name
    object_id  = models.PositiveIntegerField(null=True, blank=True)
    # [{"field", "label", "before", "after", "money": bool}, ...]
    changes    = models.JSONField(default=list, blank=True)
    details    = models.JSONField(default=dict, blank=True)
    source     = models.CharField(max_length=20, choices=SOURCES, default="app")
    reason     = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["user", "area", "-created_at"])]

    def __str__(self):
        return f"{self.user} {self.area} {self.action}: {self.title}"
