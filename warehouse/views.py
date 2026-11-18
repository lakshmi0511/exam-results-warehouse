"""Views that give the raw, vendor-shaped tables one consistent shape.

Vendors name the same thing differently (``roll_no`` vs ``roll_number``,
``score`` vs ``marks_obtained``). The raw tables keep whatever the file said;
these views pick the first column that exists from a list of known aliases.
Views are rebuilt after every refresh, so a new vendor column only needs a new
alias here, never a reload.
"""
from __future__ import annotations

import sqlite3

from .loader import qi, table_columns

ROLL = ("roll_no", "roll_number", "roll", "registration_no", "hall_ticket_no")
EMAIL = ("email", "email_id", "email_address", "candidate_email")
NAME = ("candidate_name", "name", "full_name", "student_name")
COURSE = ("course_code", "course", "course_id", "subject_code")
COURSE_NAME = ("course_name", "course_title", "subject")
DAY = ("exam_day", "day")
SHIFT = ("shift", "session")
CENTRE = ("centre_city", "center_city", "centre", "city")
SCORE = ("score", "marks_obtained", "marks", "total_score")
MAX_SCORE = ("max_score", "total_marks", "out_of", "maximum_marks")
ASSIGN_AVG = ("assignment_avg_out_of_25", "assignment_avg", "assignment_score")
ASSIGN_N = ("assignments_submitted", "submitted")
ELIGIBLE = ("eligible", "eligibility")


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                       (name,)).fetchone() is not None


def pick(cols, alias: str, candidates, wrap: str | None = None) -> str:
    """SQL expression for the first non-blank of the alias columns that exist, else NULL."""
    found = [c for c in candidates if c in cols]
    if not found:
        return "NULL"
    parts = [f"NULLIF(TRIM({alias}.{qi(c)}), '')" for c in found]
    expr = parts[0] if len(parts) == 1 else f"COALESCE({', '.join(parts)})"
    return f"{wrap}({expr})" if wrap else expr


def _cols(con, table):
    return table_columns(con, table) if table_exists(con, table) else []


def build_views(con: sqlite3.Connection) -> None:
    with con:
        for v in ("candidate_lookup", "exam_results"):
            con.execute(f"DROP VIEW IF EXISTS {v}")

        parts = []
        sc = _cols(con, "exam_scores")
        if sc:
            parts.append(
                f"SELECT {pick(sc, 's', ROLL, 'UPPER')} AS roll_no, {pick(sc, 's', COURSE, 'UPPER')} AS course_code, "
                f"'present' AS attendance, CAST({pick(sc, 's', SCORE)} AS REAL) AS score, "
                f"CAST({pick(sc, 's', MAX_SCORE)} AS REAL) AS max_score, s.source_file FROM exam_scores s")
        ab = _cols(con, "absentees")
        if ab:
            parts.append(
                f"SELECT {pick(ab, 'a', ROLL, 'UPPER')}, {pick(ab, 'a', COURSE, 'UPPER')}, 'absent', NULL, NULL, "
                f"a.source_file FROM absentees a")
        if not parts:
            parts.append("SELECT NULL AS roll_no, NULL AS course_code, NULL AS attendance, NULL AS score, "
                         "NULL AS max_score, NULL AS source_file WHERE 0")
        cc = _cols(con, "candidates")
        slot = (f"SELECT {pick(cc, 'c', ROLL, 'UPPER')} AS roll_no, {pick(cc, 'c', DAY)} AS exam_day, "
                f"{pick(cc, 'c', SHIFT)} AS shift FROM candidates c") if cc else \
            "SELECT NULL AS roll_no, NULL AS exam_day, NULL AS shift WHERE 0"
        con.execute(f"""
            CREATE VIEW exam_results AS
            SELECT r.roll_no, r.course_code, c.exam_day, c.shift, r.attendance, r.score, r.max_score,
                   CASE WHEN r.max_score > 0 THEN ROUND(100.0 * r.score / r.max_score, 1) END AS pct,
                   r.source_file
            FROM ({' UNION ALL '.join(parts)}) r
            LEFT JOIN ({slot}) c ON c.roll_no = r.roll_no""")

        if not cc:
            return
        asg = _cols(con, "assignment_scores")
        assign = (f"SELECT LOWER({pick(asg, 'a', EMAIL)}) AS email, {pick(asg, 'a', COURSE, 'UPPER')} AS course_code, "
                  f"{pick(asg, 'a', ASSIGN_AVG)} AS assignment_avg, {pick(asg, 'a', ASSIGN_N)} AS assignments_submitted, "
                  f"{pick(asg, 'a', ELIGIBLE)} AS eligible FROM assignment_scores a") if asg else \
            ("SELECT NULL AS email, NULL AS course_code, NULL AS assignment_avg, "
             "NULL AS assignments_submitted, NULL AS eligible WHERE 0")
        con.execute(f"""
            CREATE VIEW candidate_lookup AS
            SELECT c.roll_no, c.name, c.email, c.course_code, c.course_name, c.exam_day, c.shift, c.centre,
                   e.attendance, e.score, e.max_score, e.pct,
                   a.assignment_avg, a.assignments_submitted, a.eligible,
                   c.source_file AS allocation_file
            FROM (SELECT {pick(cc, 'c', ROLL, 'UPPER')} AS roll_no, {pick(cc, 'c', NAME)} AS name,
                         LOWER({pick(cc, 'c', EMAIL)}) AS email, {pick(cc, 'c', COURSE, 'UPPER')} AS course_code,
                         {pick(cc, 'c', COURSE_NAME)} AS course_name, {pick(cc, 'c', DAY)} AS exam_day,
                         {pick(cc, 'c', SHIFT)} AS shift, {pick(cc, 'c', CENTRE)} AS centre, c.source_file
                  FROM candidates c) c
            LEFT JOIN exam_results e ON e.roll_no = c.roll_no
            LEFT JOIN ({assign}) a ON a.email = c.email AND a.course_code = c.course_code""")
