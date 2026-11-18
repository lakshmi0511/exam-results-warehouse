"""Read-side helpers shared by lookup.py and the browser UI."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from .loader import qi, table_columns
from .views import COURSE, EMAIL, NAME, ROLL, pick, table_exists

# (section title, table, key) -- key says whether the table is matched on roll number or email.
SECTIONS = [
    ("Allocation", "candidates", "roll"),
    ("Exam score", "exam_scores", "roll"),
    ("Absent", "absentees", "roll"),
    ("Question-wise responses", "question_responses", "roll"),
    ("Assignment score", "assignment_scores", "email"),
]


def connect_ro(path: Path) -> sqlite3.Connection:
    """Open the warehouse read-only (the UI and lookup never write)."""
    if not Path(path).exists():
        raise SystemExit(f"database not found: {path}  (run `python -m warehouse.sync rebuild` first)")
    con = sqlite3.connect(f"file:{quote(str(Path(path).resolve()))}?mode=ro", uri=True,
                          check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    return con


def _in(n: int) -> str:
    return "(" + ", ".join("?" * n) + ")"


def resolve(con: sqlite3.Connection, q: str) -> dict:
    """Turn a roll number, email or name fragment into the set of rolls and emails of one person.
    Roll <-> email is resolved through the candidates table, so a roll number also finds the
    person's other registrations and their assignment scores (which are keyed on email)."""
    q = q.strip()
    rolls: set[str] = set()
    emails: set[str] = set()
    cc = table_columns(con, "candidates") if table_exists(con, "candidates") else []
    roll_e, email_e, name_e = pick(cc, "c", ROLL, "UPPER"), pick(cc, "c", EMAIL, "LOWER"), pick(cc, "c", NAME)

    if "@" in q:
        emails.add(q.lower())
    elif cc and con.execute(f"SELECT 1 FROM candidates c WHERE {roll_e} = ? LIMIT 1", (q.upper(),)).fetchone():
        rolls.add(q.upper())
    elif " " not in q and any(ch.isdigit() for ch in q):
        rolls.add(q.upper())  # looks like a roll number that only the vendor files know about
    elif cc and q:
        people = {}
        for r in con.execute(f"SELECT {roll_e} AS roll, {name_e} AS name, {email_e} AS email "
                             f"FROM candidates c WHERE {name_e} LIKE ? ORDER BY name", (f"%{q}%",)):
            people.setdefault(r["email"] or r["roll"], []).append(dict(r))
        if len(people) > 1:
            return {"rolls": [], "emails": [], "matches": [v[0] | {"rolls": [x["roll"] for x in v]}
                                                         for v in people.values()]}
        for regs in people.values():
            for r in regs:
                rolls.add(r["roll"])
                if r["email"]:
                    emails.add(r["email"])

    if cc and rolls:
        emails |= {r[0] for r in con.execute(
            f"SELECT DISTINCT {email_e} FROM candidates c WHERE {roll_e} IN {_in(len(rolls))}",
            sorted(rolls)) if r[0]}
    if cc and emails:
        rolls |= {r[0] for r in con.execute(
            f"SELECT DISTINCT {roll_e} FROM candidates c WHERE {email_e} IN {_in(len(emails))}",
            sorted(emails)) if r[0]}
    return {"rolls": sorted(rolls), "emails": sorted(emails), "matches": []}


def person(con: sqlite3.Connection, q: str) -> dict:
    who = resolve(con, q)
    out = {"query": q, **who, "name": None, "summary": [], "sections": []}
    if who["matches"]:
        return out
    rolls, emails = who["rolls"], who["emails"]
    if rolls and table_exists(con, "candidate_lookup"):
        out["summary"] = [dict(r) for r in con.execute(
            f"SELECT * FROM candidate_lookup WHERE roll_no IN {_in(len(rolls))} ORDER BY roll_no", rolls)]
        names = [r["name"] for r in out["summary"] if r["name"]]
        out["name"] = names[0] if names else None
    for title, table, key in SECTIONS:
        if not table_exists(con, table):
            continue
        cols = table_columns(con, table)
        values = rolls if key == "roll" else emails
        expr = pick(cols, "t", ROLL, "UPPER") if key == "roll" else pick(cols, "t", EMAIL, "LOWER")
        rows = []
        if values and expr != "NULL":
            rows = [dict(r) for r in con.execute(
                f"SELECT * FROM {qi(table)} t WHERE {expr} IN {_in(len(values))} "
                f"ORDER BY source_file, source_row", values)]
        out["sections"].append({"title": title, "table": table, "rows": rows})
        if out["name"] is None:
            names = [r.get(c) for r in rows for c in NAME if r.get(c)]
            out["name"] = names[0] if names else None
    return out


def search(con: sqlite3.Connection, q: str, limit: int = 50) -> list[dict]:
    q = q.strip()
    if not q or not table_exists(con, "candidate_lookup"):
        return []
    like = f"%{q}%"
    return [dict(r) for r in con.execute(
        "SELECT roll_no, name, email, course_code, course_name, exam_day, shift, attendance, score, pct "
        "FROM candidate_lookup WHERE roll_no LIKE ? OR email LIKE ? OR name LIKE ? "
        "ORDER BY name, roll_no LIMIT ?", (like, like, like, limit))]


