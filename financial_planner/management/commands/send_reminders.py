"""
Email each user a short digest of what's due in the next few days.
Run it once a day (PythonAnywhere → Tasks):  venv/bin/python manage.py send_reminders
Nothing is sent to a user with nothing coming up. --dry-run prints instead.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.core.management.base import BaseCommand

from financial_planner.reminders import build_reminders


def digest(r):
    lines = []
    for d in r["due"]:
        when = "today" if d["days"] == 0 else "tomorrow" if d["days"] == 1 else f"in {d['days']} days"
        lines.append(f"- {d['label']}: ${d['amount']:,.2f} due {when} ({d['date']})"
                     + (" (automatic)" if d["auto"] else ""))
    for c in r["limits"]:
        lines.append(f"- {c['label']} is close to its limit: ${c['room']:,.2f} of room left")
    for b in r["budgets"]:
        lines.append(f"- {b['label']}: {b['percent']:.0f}% of your monthly limit used")
    return lines


class Command(BaseCommand):
    help = "Email each user the payments due in the next few days."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=3)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, days=3, dry_run=False, **kwargs):
        sent = 0
        for user in get_user_model().objects.filter(is_active=True).exclude(email=""):
            lines = digest(build_reminders(user, days=days))
            if not lines:
                continue
            body = ("Coming up in Glacier:\n\n" + "\n".join(lines)
                    + "\n\nOpen Glacier to see the details or tick them off in your Planner.")
            if dry_run:
                self.stdout.write(f"--- {user.email}\n{body}\n")
                continue
            try:
                send_mail("Glacier: payments coming up", body, settings.DEFAULT_FROM_EMAIL, [user.email])
                sent += 1
            except Exception as e:                       # one bad address mustn't stop the rest
                self.stderr.write(f"Couldn't email {user.email}: {e}")
        self.stdout.write(f"Reminders sent: {sent}")
