"""Path-based classification: which table does a file in the shared folder belong to?

Every file is classified purely from its path relative to the raw folder. The
answer is either a table plus a *group key*, or ``None`` plus a human-readable
reason. Nothing is silently ignored: a file that matches no rule is reported as
"no rule for this file" and recorded in the refresh log as ``skipped``.

Group keys drive snapshot versioning: within one group only the newest file
(by modification time) is kept in the database. A vendor re-exporting the
allocation list, or re-sending the scores for one shift, therefore replaces the
previous copy instead of duplicating rows.

Zip archives are extracted next to themselves into ``<name>.zip_extracted/``.
That directory segment is dropped before matching, so a score file inside
``day_3/shift_2/export.zip`` is classified exactly like a loose
``day_3/shift_2/score_*.csv``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ZIP_DIR_SUFFIX = ".zip_extracted"
NO_RULE = "no rule for this file"


@dataclass(frozen=True)
class Rule:
    pattern: str          # regex, matched (case-insensitively) against the logical path
    table: str            # target table; may use named groups, e.g. "sheet_{book}"
    group: str            # snapshot group key; may use named groups
    per_tab: bool = False  # load every worksheet into its own table "<table>_<tab>"
    description: str = ""


_DATA = r"\.(?:csv|xlsx)"
_SLOT = r"day_(?P<day>\d+)/shift_(?P<shift>\d+)"

RULES: list[Rule] = [
    Rule(rf"allocation/[^/]+{_DATA}", "candidates", "allocation",
         description="Seat allocation export: one row per registration (roll number)."),
    Rule(rf"{_SLOT}/score_[^/]*{_DATA}", "exam_scores", "scores/day_{day}/shift_{shift}",
         description="Vendor score export for one exam shift."),
    Rule(rf"{_SLOT}/absentees_[^/]*{_DATA}", "absentees", "absentees/day_{day}/shift_{shift}",
         description="Vendor absentee list for one exam shift."),
    Rule(rf"{_SLOT}/responses_[^/]*\.xlsx", "question_responses", "responses/day_{day}/shift_{shift}",
         description="Question-wise responses workbook for one exam shift."),
    Rule(rf"assignments/[^/]+{_DATA}", "assignment_scores", "assignments",
         description="Continuous-assessment (assignment) scores snapshot."),
    Rule(r"sheets/(?P<book>[^/]+)\.xlsx", "sheet_{book}", "sheets/{book}", per_tab=True,
         description="Planning workbook: one table per worksheet."),
]


@dataclass(frozen=True)
class Match:
    table: str
    group_key: str
    per_tab: bool = False
    rule: Rule | None = None


def snake(text: str) -> str:
    """'Shift Plan' -> 'shift_plan', 'RollNo' -> 'roll_no'."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(text).strip())
    return re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_").lower()


def book_name(stem: str) -> str:
    """Workbook name without a trailing date/version stamp: 'exam_planner_v3' -> 'exam_planner'."""
    stem = re.sub(r"[ _-]+(?:v\d+|\d{4}-\d{2}-\d{2}|\d{8}|copy|final)$", "", stem, flags=re.I)
    return snake(stem) or "book"


def logical_path(rel_path: str) -> str:
    """Drop '<name>.zip_extracted' segments so zip contents classify like loose files."""
    parts = rel_path.replace("\\", "/").split("/")
    return "/".join(p for p in parts if not p.lower().endswith(ZIP_DIR_SUFFIX))


def match(rel_path: str) -> Match | None:
    """Return the full Match for a path, or None when no rule applies."""
    path = rel_path.replace("\\", "/")
    name = path.rsplit("/", 1)[-1]
    if name.startswith("~$") or path.lower().endswith(".zip"):
        return None
    logical = logical_path(path)
    for rule in RULES:
        m = re.fullmatch(rule.pattern, logical, flags=re.IGNORECASE)
        if not m:
            continue
        groups = {k: v for k, v in m.groupdict().items() if v is not None}
        if "book" in groups:
            groups["book"] = book_name(groups["book"])
        for k in ("day", "shift"):
            if k in groups:
                groups[k] = str(int(groups[k]))
        return Match(rule.table.format(**groups), rule.group.format(**groups), rule.per_tab, rule)
    return None


def classify(rel_path: str) -> tuple[str, str] | tuple[None, str]:
    """``(table, group_key)`` for a known file, otherwise ``(None, reason)``."""
    m = match(rel_path)
    if m is not None:
        return m.table, m.group_key
    path = rel_path.replace("\\", "/")
    if path.rsplit("/", 1)[-1].startswith("~$"):
        return None, "spreadsheet lock file (~$...), not data"
    if path.lower().endswith(".zip"):
        return None, f"archive: extracted to {path.rsplit('/', 1)[-1]}_extracted/ and its contents classified"
    return None, NO_RULE
