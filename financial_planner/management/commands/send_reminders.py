"""
Email each user a short digest of what's due in the next few days.
Run it once a day (PythonAnywhere → Tasks):  venv/bin/python manage.py send_reminders
Nothing is sent to a user with nothing coming up. --dry-run prints instead;
--preview FILE writes the first user's email as HTML to look at.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand

from financial_planner.reminder_email import has_news, html, subject, text
from financial_planner.reminders import build_reminders


class Command(BaseCommand):
    help = "Email each user the payments due in the next few days."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=3)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--preview", help="Write the first email that would go out to this HTML file.")

    def handle(self, *args, days=3, dry_run=False, preview=None, **kwargs):
        app_url = getattr(settings, "FRONTEND_URL", "")
        sent = 0
        for user in get_user_model().objects.filter(is_active=True).exclude(email=""):
            r = build_reminders(user, days=days)
            if not has_news(r):
                continue
            name = (user.first_name or "").strip()
            if preview:
                with open(preview, "w", encoding="utf-8") as f:
                    f.write(html(r, app_url, name))
                self.stdout.write(f"Wrote a preview to {preview} (subject: {subject(r)})")
                return
            if dry_run:
                self.stdout.write(f"--- {user.email}\nSubject: {subject(r)}\n{text(r, app_url)}\n")
                continue
            msg = EmailMultiAlternatives(f"Glacier: {subject(r)}", text(r, app_url), settings.DEFAULT_FROM_EMAIL, [user.email])
            msg.attach_alternative(html(r, app_url, name), "text/html")
            try:
                msg.send()
                sent += 1
            except Exception as e:                       # one bad address mustn't stop the rest
                self.stderr.write(f"Couldn't email {user.email}: {e}")
        self.stdout.write(f"Reminders sent: {sent}")
