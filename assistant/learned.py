"""
Aurora learns the questions a user asks often and offers them as quick
questions. Only the user's own short question is kept (never the answer, and
nothing from their data); `instant` marks the ones quick answers handle with
no AI. A user can hide any of them.
"""
import re

from django.db.models import F
from django.utils import timezone

from .models import AskedQuestion

# Records and commands, not questions to ask again.
SKIP_INTENTS = {"log_expense", "log_payment", "undo", "help_text"}
MAX_KEPT = 50
MIN_TIMES = 2          # asked this often before it's offered


def _key(text):
    t = re.sub(r"[^\w$%.\s]", " ", text.lower())
    return " ".join(t.replace(".", " ").split())[:200]


def record(user, text, intent=None):
    """Count one question; `intent` is the quick answer that handled it, if any."""
    text = " ".join(str(text or "").split())
    if not (6 <= len(text) <= 140) or intent in SKIP_INTENTS:
        return
    key = _key(text)
    if not key:
        return
    obj, created = AskedQuestion.objects.get_or_create(user=user, key=key,
                                                       defaults={"text": text[:200], "instant": bool(intent)})
    if not created:
        AskedQuestion.objects.filter(pk=obj.pk).update(count=F("count") + 1, text=text[:200], instant=bool(intent),
                                                       last_asked=timezone.now())
    # Keep the most-asked 50.
    extra = list(AskedQuestion.objects.filter(user=user).order_by("-count", "-last_asked")
                 .values_list("pk", flat=True)[MAX_KEPT:])
    if extra:
        AskedQuestion.objects.filter(pk__in=extra).delete()


def yours(user, limit=8):
    return [{"text": q.text, "count": q.count, "instant": q.instant}
            for q in AskedQuestion.objects.filter(user=user, hidden=False, count__gte=MIN_TIMES)[:limit]]


def hide(user, text):
    return AskedQuestion.objects.filter(user=user, key=_key(text)).update(hidden=True)
