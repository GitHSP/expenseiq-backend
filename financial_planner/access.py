"""
Which pages each user can open, set by an admin (a staff user) on the Users
page. No row (or pages = null) means every page. Admins always see everything.
It's about features, not security: everyone only ever sees their own data. The
one thing also blocked on the server is Aurora, since it uses the AI credits.
"""
from django.conf import settings
from django.db import models

PAGES = ["day", "dashboard", "expenses", "analytics", "payments", "accounts", "investments", "activity", "aurora"]


class PageAccess(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="page_access")
    pages = models.JSONField(null=True, blank=True)          # null = all pages
    updated_at = models.DateTimeField(auto_now=True)


def allowed_pages(user):
    """The pages this user can open, or None for all of them."""
    if not user or not user.is_authenticated or user.is_staff:
        return None
    row = PageAccess.objects.filter(user=user).first()
    return None if row is None or row.pages is None else [p for p in PAGES if p in row.pages]


def can_use(user, page):
    pages = allowed_pages(user)
    return pages is None or page in pages
