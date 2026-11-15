"""
A full backup of one user's data, as a gzipped JSON file: every table in
expenses, financial_planner, assistant and activity, filtered to the user.
Left out on purpose: the login (password hash, two-factor secret) and the
assistant's encrypted chat sync (useless without the passphrase).

Downloaded from Profile (GET /api/fp/backup/) and emailed every Sunday
morning by the clock (day_views.CronView, morning job).
"""
import gzip
import json
from zoneinfo import ZoneInfo

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import serializers
from django.core.mail import EmailMessage
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

APPS = ("expenses", "financial_planner", "assistant", "activity")
SKIP = {("assistant", "EncryptionKey"), ("assistant", "SyncedMessage")}
# Tables without a user column: reached through their parent.
THROUGH = {"ChecklistItem": "monthly_plan__user", "InvestmentPayment": "investment__user", "Slot": "plan__user"}


def tables(user):
    out, missed = {}, []
    for label in APPS:
        for model in apps.get_app_config(label).get_models():
            if (label, model.__name__) in SKIP:
                continue
            names = {f.name for f in model._meta.get_fields()}
            if "user" in names:
                qs = model.objects.filter(user=user)
            elif model.__name__ in THROUGH:
                qs = model.objects.filter(**{THROUGH[model.__name__]: user})
            else:
                missed.append(f"{label}.{model.__name__}")
                continue
            out[f"{label}.{model.__name__}"] = json.loads(serializers.serialize("json", qs.order_by("pk")))
    return out, missed


def build_backup(user):
    data, missed = tables(user)
    payload = {"app": "Glacier", "version": 1, "created": timezone.now().isoformat(), "user": user.email,
               "counts": {k: len(v) for k, v in data.items()}, "not_included": missed, "tables": data}
    return gzip.compress(json.dumps(payload, cls=DjangoJSONEncoder).encode("utf-8"))


def filename(user):
    return f"glacier-backup-{timezone.localdate():%Y-%m-%d}.json.gz"


def send_backups(only_sunday=True):
    """Email each user their backup; by default only where it's Sunday for them."""
    from .day_models import DayProfile
    sent = 0
    for user in get_user_model().objects.filter(is_active=True).exclude(email=""):
        tz = getattr(DayProfile.objects.filter(user=user).first(), "timezone", None) or "America/Toronto"
        try:
            local = timezone.now().astimezone(ZoneInfo(tz))
        except Exception:
            local = timezone.now().astimezone(ZoneInfo("America/Toronto"))
        if only_sunday and local.weekday() != 6:
            continue
        body = ("Your weekly Glacier backup is attached: everything you've entered, as one file.\n\n"
                "Keep it somewhere safe (it's your financial data). You can also download one any time "
                "from Profile in Glacier.\n")
        msg = EmailMessage(f"Glacier: your weekly backup ({local:%b %d})", body, settings.DEFAULT_FROM_EMAIL, [user.email])
        msg.attach(filename(user), build_backup(user), "application/gzip")
        try:
            msg.send()
            sent += 1
        except Exception:
            pass
    return sent
