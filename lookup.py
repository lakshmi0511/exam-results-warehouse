#!/usr/bin/env python3
"""Quick candidate lookup for support staff.

    python lookup.py EX26A00042              # roll number
    python lookup.py someone@example.com     # email
    python lookup.py "tumelo"                # name fragment

Prints every section the warehouse knows about the person: allocation, exam
score, absence, question-wise responses and assignment score, each with the
file and row it came from.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from warehouse.query import connect_ro, person  # noqa: E402

DEFAULT_DB = Path(os.environ.get("WAREHOUSE_DB", ROOT / "db" / "warehouse.sqlite"))
HIDE = {"source_file", "source_row"}


def fmt_score(r: dict) -> str:
    if r["attendance"] == "absent":
        return "absent"
    if r["score"] is None:
        return "no result yet"
    return f"{r['score']:g}/{r['max_score']:g} ({r['pct']:g}%)"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Look up a candidate by roll number, email or name.")
    ap.add_argument("query", nargs="+")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = ap.parse_args(argv)
    q = " ".join(args.query)

    con = connect_ro(args.db)
    p = person(con, q)

    if p["matches"]:
        print(f"'{q}' matches {len(p['matches'])} people; be more specific:")
        for m in p["matches"][:25]:
            print(f"  {m['name']:<28} {m['email'] or '-':<40} {', '.join(m['rolls'])}")
        return 2
    if not p["rolls"] and not p["emails"]:
        print(f"Nothing found for '{q}'.")
        return 1

    print(f"{p['name'] or '(name unknown)'}")
    print(f"  email : {', '.join(p['emails']) or '-'}")
    print(f"  rolls : {', '.join(p['rolls']) or '-'}")

    if p["summary"]:
        print("\nSummary")
        for r in p["summary"]:
            assign = f"assignments {r['assignment_avg']}/25, eligible: {r['eligible']}" \
                if r["assignment_avg"] is not None else "no assignment record"
            print(f"  {r['roll_no']}  {r['course_code']} {r['course_name'] or ''}  "
                  f"day {r['exam_day']} shift {r['shift']}  ->  {fmt_score(r)};  {assign}")

    for s in p["sections"]:
        rows = s["rows"]
        print(f"\n{s['title']}  [{s['table']}]  " + (f"{len(rows)} row(s)" if rows else "- none"))
        for r in rows:
            fields = [(k, v) for k, v in r.items() if k not in HIDE and v not in (None, "")]
            width = max(len(k) for k, _ in fields) if fields else 0
            for k, v in fields:
                print(f"    {k:<{width}}  {v}")
            print(f"    {'':<{width}}  <- {r['source_file']} row {r['source_row']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
