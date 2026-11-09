"""
The daily reminder email: a subject that says what's due, an HTML body in
Glacier's style (inline styles, so email apps show it as designed), and a
plain-text version for apps that don't show HTML.
"""
from datetime import date
from decimal import Decimal
from html import escape

GOLD, INK, TEXT, MUTED, LINE, BG, CARD = "#e3b341", "#1c1405", "#111827", "#6b7280", "#e5e7eb", "#f4f4f5", "#ffffff"


def money(v):
    return f"CA${Decimal(v):,.2f}"


def when(d):
    if d["days"] == 0:
        return "Today"
    if d["days"] == 1:
        return "Tomorrow"
    return f"{date.fromisoformat(d['date']):%a, %b} {date.fromisoformat(d['date']).day}"


def has_news(r):
    return bool(r["due"] or r["limits"] or r["budgets"])


def subject(r):
    due = r["due"]
    if due:
        total = sum((Decimal(d["amount"]) for d in due), Decimal(0))
        soon = "today" if all(d["days"] == 0 for d in due) else "this week" if max(d["days"] for d in due) <= 7 else "soon"
        return f"{len(due)} payment{'' if len(due) == 1 else 's'} due {soon} · {money(total)}"
    if r["limits"]:
        return f"{len(r['limits'])} card{'' if len(r['limits']) == 1 else 's'} near the limit"
    return "A spending limit is nearly used"


def text(r, app_url):
    out = ["Coming up in Glacier", ""]
    if r["due"]:
        out.append("DUE SOON")
        for d in r["due"]:
            out.append(f"  {when(d):<12} {d['label']}  {money(d['amount'])}" + ("  (automatic)" if d["auto"] else ""))
        out.append(f"  Total: {money(sum((Decimal(d['amount']) for d in r['due']), Decimal(0)))}")
        out.append("")
    if r["limits"]:
        out.append("CARDS NEAR THEIR LIMIT")
        out += [f"  {c['label']}: {money(c['room'])} of room left" for c in r["limits"]]
        out.append("")
    if r["budgets"]:
        out.append("SPENDING LIMITS")
        out += [f"  {b['label']}: {b['percent']:.0f}% used ({money(b['spent'])} of {money(b['limit'])})" for b in r["budgets"]]
        out.append("")
    if r["uploads"]:
        out.append(f"Upload your latest statements for {len(r['uploads'])} account{'' if len(r['uploads']) == 1 else 's'} to keep your numbers current.")
        out.append("")
    out += [f"Open Glacier: {app_url}", "", "You get this because reminders are on for your Glacier account."]
    return "\n".join(out)


def _section(title, rows_html):
    return (f'<tr><td style="padding:22px 28px 6px;font:600 11px/1 Arial,sans-serif;letter-spacing:.8px;'
            f'text-transform:uppercase;color:{MUTED}">{escape(title)}</td></tr>'
            f'<tr><td style="padding:0 28px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            f'{rows_html}</table></td></tr>')


def _row(left, sub, right, right_color=TEXT):
    return (f'<tr><td style="padding:11px 0;border-bottom:1px solid {LINE};font:600 15px/1.35 Arial,sans-serif;color:{TEXT}">'
            f'{escape(left)}<div style="font:400 12px/1.4 Arial,sans-serif;color:{MUTED};margin-top:2px">{escape(sub)}</div></td>'
            f'<td align="right" style="padding:11px 0;border-bottom:1px solid {LINE};font:700 15px/1 Arial,sans-serif;'
            f'color:{right_color};white-space:nowrap">{escape(right)}</td></tr>')


def html(r, app_url, name=""):
    parts = []
    if r["due"]:
        total = sum((Decimal(d["amount"]) for d in r["due"]), Decimal(0))
        rows = "".join(_row(d["label"], when(d) + (" · automatic" if d["auto"] else ""), money(d["amount"]),
                            "#b45309" if d["days"] <= 1 else TEXT) for d in r["due"])
        rows += (f'<tr><td style="padding:12px 0 0;font:700 14px Arial,sans-serif;color:{TEXT}">Total due</td>'
                 f'<td align="right" style="padding:12px 0 0;font:800 16px Arial,sans-serif;color:{TEXT}">{money(total)}</td></tr>')
        parts.append(_section("Due soon", rows))
    if r["limits"]:
        parts.append(_section("Cards near their limit", "".join(
            _row(c["label"], f"Limit {money(c['limit'])}", f"{money(c['room'])} left", "#be123c") for c in r["limits"])))
    if r["budgets"]:
        parts.append(_section("Spending limits", "".join(
            _row(b["label"], f"{money(b['spent'])} of {money(b['limit'])} this month", f"{b['percent']:.0f}%",
                 "#be123c" if b["percent"] >= 100 else "#b45309") for b in r["budgets"])))
    if r["uploads"]:
        n = len(r["uploads"])
        parts.append(f'<tr><td style="padding:18px 28px 0;font:400 13px/1.5 Arial,sans-serif;color:{MUTED}">'
                     f'Upload your latest statements for {n} account{"" if n == 1 else "s"} to keep your numbers current.</td></tr>')
    hello = f"Hi {escape(name)}," if name else "Hi,"
    return f"""<!doctype html><html><body style="margin:0;padding:0;background:{BG}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BG};padding:24px 12px">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:{CARD};border-radius:14px;overflow:hidden">
<tr><td style="background:#0a0a0a;padding:18px 28px">
  <img src="{escape(app_url.rstrip('/'))}/logo192.png" width="32" height="32" alt="Glacier"
       style="display:inline-block;width:32px;height:32px;border:0;border-radius:8px;vertical-align:middle">
  <span style="display:inline-block;vertical-align:middle;margin-left:10px;line-height:1.15">
    <span style="display:block;font:800 19px Arial,sans-serif;color:#ffffff">Glacier</span>
    <span style="display:block;font:600 11px Arial,sans-serif;color:#9ca3af;letter-spacing:.4px">Money manager</span>
  </span>
</td></tr>
<tr><td style="padding:26px 28px 0;font:400 15px/1.5 Arial,sans-serif;color:{TEXT}">{hello}<br>here's what's coming up.</td></tr>
{''.join(parts)}
<tr><td style="padding:26px 28px 30px">
  <a href="{escape(app_url)}" style="display:inline-block;background:{GOLD};color:{INK};font:700 15px Arial,sans-serif;
     text-decoration:none;padding:13px 24px;border-radius:9px">Open Glacier</a>
</td></tr>
</table>
<div style="max-width:560px;font:400 12px/1.5 Arial,sans-serif;color:{MUTED};padding:16px 8px 0;text-align:center">
  You get this because reminders are on for your Glacier account.<br>Estimates from your own data, not financial advice.
</div>
</td></tr></table></body></html>"""
