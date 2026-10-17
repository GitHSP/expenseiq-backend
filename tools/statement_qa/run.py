"""
Statement-import QA: generate the synthetic statements in corpus.py, run
them through the importer, and compare with what each should produce.

    cd backend
    python tools/statement_qa/run.py            # summary table
    python tools/statement_qa/run.py --verbose  # plus every missing/extra row

Files are written to tools/statement_qa/out/ so they can also be uploaded
through the app by hand.
"""

import os
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from corpus import build  # noqa: E402
from expenses.statement_import import StatementError, clean_title, parse_statement, suggest_flip  # noqa: E402

# Text that must never end up in a transaction's title: category labels,
# summary-box wording, page furniture, currency notes.
JUNK = ("restaurants", "retail and grocery", "transportation", "personal and household",
        "available credit", "minimum", "total", "balance", " of 2", "foreign currency", "statement")


def check(statement):
    try:
        rows, info = parse_statement(statement.name, statement.content)
    except StatementError as e:
        return {"error": str(e), "missing": statement.expected, "extra": [], "flip": None, "junk": []}
    flip = suggest_flip(rows, info)
    junk = [r["description"] for r in rows if any(j in clean_title(r["description"]).lower() for j in JUNK)]
    got = Counter((r["date"], -r["amount"] if flip else r["amount"]) for r in rows)
    want = Counter(statement.expected)
    return {
        "error": None,
        "missing": sorted((want - got).elements()),
        "extra": sorted((got - want).elements()),
        "flip": flip,
        "rows": rows,
        "junk": junk,
    }


def main(verbose=False, write=True):
    out = HERE / "out"
    out.mkdir(exist_ok=True)
    statements = build()
    failures = 0
    print(f"{'file':<24}{'bank':<20}{'expected':>9}{'found':>7}{'missing':>9}{'extra':>7}  result")
    print("-" * 86)
    for st in statements:
        if write:
            (out / st.name).write_bytes(st.content)
        r = check(st)
        found = 0 if r["error"] else len(r["rows"])
        ok = not r["error"] and not r["missing"] and not r["extra"] and not r.get("junk")
        failures += not ok
        print(f"{st.name:<24}{st.bank:<20}{len(st.expected):>9}{found:>7}{len(r['missing']):>9}{len(r['extra']):>7}  "
              f"{'PASS' if ok else 'FAIL'}{'  (flip)' if r['flip'] else ''}")
        if not ok and (verbose or r["error"]):
            if r["error"]:
                print(f"    error: {r['error']}")
            for j in r.get("junk", []):
                print(f"    messy title: {j[:70]}")
            for d, a in r["missing"]:
                print(f"    missing  {d}  {a:>10}")
            for d, a in r["extra"]:
                desc = next((x["description"] for x in r.get("rows", []) if x["date"] == d and abs(x["amount"]) == abs(a)), "")
                print(f"    extra    {d}  {a:>10}  {desc[:50]}")
    print("-" * 86)
    print(f"{len(statements) - failures}/{len(statements)} statements imported exactly right")
    return failures


if __name__ == "__main__":
    sys.exit(1 if main("--verbose" in sys.argv) else 0)
