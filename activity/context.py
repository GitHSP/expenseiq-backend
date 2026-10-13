# activity/context.py
#
# Who/what is making the current change, so automatically recorded activity
# can say "AI assistant" or "Statement import" rather than just "You".
#
#   with activity_context(source="assistant"):
#       ...  # anything saved here is attributed to the assistant
#
# `reason` explains a knock-on change (e.g. a debt balance that dropped
# because a checklist payment was ticked); `quiet` lists areas whose
# individual changes shouldn't be logged (an import logs one summary
# instead of a line per transaction).

from contextlib import contextmanager
from contextvars import ContextVar

_source = ContextVar("activity_source", default="app")
_reason = ContextVar("activity_reason", default="")
_quiet  = ContextVar("activity_quiet", default=frozenset())
_deleting_users = set()


@contextmanager
def activity_context(source=None, reason=None, quiet=None):
    tokens = []
    if source is not None:
        tokens.append((_source, _source.set(source)))
    if reason is not None:
        tokens.append((_reason, _reason.set(reason)))
    if quiet is not None:
        tokens.append((_quiet, _quiet.set(frozenset(quiet) | _quiet.get())))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def current_source():
    return _source.get()


def current_reason():
    return _reason.get()


def is_quiet(area):
    return area in _quiet.get()


def deleting_user(user_id, deleting):
    (_deleting_users.add if deleting else _deleting_users.discard)(user_id)


def log_activity(user, area, action, title, *, object_id=None, changes=None, details=None, source=None, reason=None):
    from .models import ActivityLog
    if user is None or user.pk in _deleting_users:
        return None
    return ActivityLog.objects.create(
        user=user, area=area, action=action, title=str(title)[:255], object_id=object_id,
        changes=changes or [], details=details or {},
        source=source or current_source(), reason=(reason if reason is not None else current_reason())[:255],
    )
