"""
Fills a day with time slots: a morning routine after waking, work, then the
goals that need time today and the things to do, in the free time before bed.

  - fixed-time things (work, "dentist at 3:00") go in first;
  - goals: morning ones as early as possible, evening ones as late as
    possible, others in the first free time; split into 30+ minute pieces
    when no gap is long enough;
  - things to do: in order, in the first gap they fit;
  - 10 minutes are left between things; whatever doesn't fit is reported.

A day can run past midnight (a 4 PM to 12 AM shift, bed at 1 AM): minutes go
past 1440 then, and times wrap round when they're shown.
"""
from datetime import time

ROUTINE_MINUTES = 30
GAP = 10
MIN_PIECE = 30


def to_min(t):
    if isinstance(t, str):
        h, m = t.split(":")[:2]
        return int(h) * 60 + int(m)
    return t.hour * 60 + t.minute


def to_time(minutes):
    minutes = int(minutes) % (24 * 60)
    return time(minutes // 60, minutes % 60)


def length(start, end):
    """Minutes from start to end; an end at or before the start is the next day."""
    m = (to_min(end) - to_min(start)) % (24 * 60)
    return m or 24 * 60


def _subtract(free, start, end):
    out = []
    for a, b in free:
        if end <= a or start >= b:
            out.append((a, b))
            continue
        if a < start:
            out.append((a, start))
        if end < b:
            out.append((end, b))
    return out


def build(wake, bed, work=(), fixed=(), goals=(), tasks=()):
    """wake/bed: times. work: [(start, end)]. fixed: [(key, title, start, minutes)].
    goals: [(key, title, minutes, when)]. tasks: [(key, title, minutes)].
    Returns (slots, unplaced): slots are dicts with start/end minutes, title,
    kind and key; unplaced are (kind, key, title, minutes) that didn't fit."""
    w, b = to_min(wake), to_min(bed)
    if b <= w:
        b += 24 * 60                                    # bed after midnight
    slots = [{"start": w, "end": min(w + ROUTINE_MINUTES, b), "title": "Wake up & get ready", "kind": "routine", "key": None}]
    free = [(w + ROUTINE_MINUTES, b)]

    for s, e in work:
        s, e = to_min(s), to_min(e)
        if e <= s:
            e += 24 * 60                                # a shift that ends after midnight
        if s < w and e - s < 24 * 60 and s + 24 * 60 < b:
            s, e = s + 24 * 60, e + 24 * 60             # starts after midnight, before bed
        if e > s:
            slots.append({"start": s, "end": e, "title": "Work", "kind": "work", "key": None})
            free = _subtract(free, s, e)
    for key, title, at, minutes in fixed:
        s = to_min(at)
        if s < w:
            s += 24 * 60                                # after midnight
        slots.append({"start": s, "end": s + minutes, "title": title, "kind": "task", "key": key})
        free = _subtract(free, s, s + minutes + GAP)

    unplaced = []

    def place(kind, key, title, minutes, late=False, split=False):
        nonlocal free
        order = sorted(free, key=lambda g: -g[1] if late else g[0])
        for a, z in order:
            if z - a >= minutes:
                s = z - minutes if late else a
                slots.append({"start": s, "end": s + minutes, "title": title, "kind": kind, "key": key})
                free = _subtract(free, s - (GAP if late else 0), s + minutes + (0 if late else GAP))
                return 0
        if not split:
            return minutes
        left = minutes
        for a, z in sorted(free, key=lambda g: g[0]):
            piece = min(left, z - a)
            if piece >= MIN_PIECE:
                slots.append({"start": a, "end": a + piece, "title": title, "kind": kind, "key": key})
                free = _subtract(free, a, a + piece + GAP)
                left -= piece
            if left < MIN_PIECE:
                break
        return left if left >= MIN_PIECE else 0

    for key, title, minutes, when in goals:
        left = place("goal", key, title, minutes, late=(when == "evening"), split=True)
        if left:
            unplaced.append(("goal", key, title, left))
    for key, title, minutes in tasks:
        if place("task", key, title, minutes):
            unplaced.append(("task", key, title, minutes))

    slots.sort(key=lambda s: (s["start"], s["end"]))
    return slots, unplaced
