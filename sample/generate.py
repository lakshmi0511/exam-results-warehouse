#!/usr/bin/env python3
"""Generate a deterministic, entirely fictional sample folder (sample/raw/).

It imitates the shared folder the warehouse was built for: exports from two
vendors with different column names and delimiters, a re-exported allocation
list (older and newer copy), a zip, a planning workbook with two tabs, and one
file that no rule knows about. Names are made up from syllables and every email
is @example.com.

    python sample/generate.py            # writes sample/raw/ (replacing it)
    python sample/generate.py --out DIR
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import os
import random
import shutil
import zipfile
from pathlib import Path

from openpyxl import Workbook

SEED = 2026
HERE = Path(__file__).resolve().parent

COURSES = [
    ("CS101", "Programming Fundamentals"), ("CS205", "Data Structures"),
    ("MA101", "Calculus I"), ("MA210", "Linear Algebra"),
    ("PH101", "Mechanics"), ("CH110", "Chemistry Basics"),
    ("EE201", "Circuit Analysis"), ("ME150", "Engineering Drawing"),
    ("HS120", "Technical Writing"), ("BT130", "Cell Biology"),
    ("DS240", "Applied Statistics"), ("EC110", "Microeconomics"),
]
SLOTS = [(1, 1), (1, 2), (2, 1), (2, 2), (3, 1), (3, 2)]          # (day, shift); 2 courses each
EXAM_DATES = {1: dt.date(2026, 3, 7), 2: dt.date(2026, 3, 8), 3: dt.date(2026, 3, 14)}
SHIFT_TIMES = {1: ("09:00", "12:00"), 2: ("14:00", "17:00")}
REPORTING = {1: "08:00", 2: "13:00"}
CENTRES = [("Quillford", "QF"), ("Avonmere", "AV"), ("Tamsin Bay", "TB"),
           ("Corvel", "CV"), ("Brightwater", "BW"), ("Oakhollow", "OH")]   # invented towns

SYL = ["ka", "ri", "an", "vi", "sha", "lo", "mi", "ra", "tu", "ne", "de", "ya", "so", "pa", "li", "ve", "jo", "ma"]
LAST_A = ["Mor", "Tel", "Van", "Dri", "Kes", "Oru", "Lin", "Bar", "Quen", "Zel", "Fen", "Hal"]
LAST_B = ["wick", "ford", "ani", "ell", "ora", "ton", "mere", "vash", "ley", "dune"]


def stamp(d: dt.date | dt.datetime, hour: int = 18) -> float:
    if not isinstance(d, dt.datetime):
        d = dt.datetime(d.year, d.month, d.day, hour)
    return d.timestamp()


class Writer:
    def __init__(self, root: Path):
        self.root = root
        self.files: list[str] = []

    def path(self, rel: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        self.files.append(rel)
        return p

    def csv(self, rel: str, header, rows, when: float, delimiter=","):
        p = self.path(rel)
        with open(p, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, delimiter=delimiter)
            w.writerow(header)
            w.writerows(rows)
        os.utime(p, (when, when))

    def xlsx(self, rel: str, tabs: dict, when: float, title_rows: dict | None = None):
        wb = Workbook()
        wb.remove(wb.active)
        for name, (header, rows) in tabs.items():
            ws = wb.create_sheet(name)
            for extra in (title_rows or {}).get(name, []):
                ws.append(extra)
            ws.append(header)
            for r in rows:
                ws.append(r)
        p = self.path(rel)
        wb.save(p)
        os.utime(p, (when, when))


def csv_bytes(header, rows, delimiter=",") -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator="\r\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def generate(out: Path) -> list[str]:
    rnd = random.Random(SEED)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    W = Writer(out)

    slot_of = {code: SLOTS[i // 2] for i, (code, _) in enumerate(COURSES)}
    course_name = dict(COURSES)
    difficulty = {code: rnd.randint(48, 70) for code, _ in COURSES}

    # ---- people and registrations ---------------------------------------------------------
    used_names = set()
    people = []
    while len(people) < 575:
        first = "".join(rnd.choice(SYL) for _ in range(rnd.choice([2, 2, 3]))).capitalize()
        last = rnd.choice(LAST_A) + rnd.choice(LAST_B)
        if (first, last) in used_names:
            continue
        used_names.add((first, last))
        email = f"{first.lower()}.{last.lower()}{rnd.randint(10, 99)}@example.com"
        people.append({"name": f"{first} {last}", "email": email})

    regs = []
    weights = [rnd.uniform(0.6, 1.6) for _ in COURSES]
    for p in people:
        regs.append((p, rnd.choices([c for c, _ in COURSES], weights)[0]))
    for p in rnd.sample(people, 30):                       # some take a second course
        first_slot = slot_of[next(c for q, c in regs if q is p)]
        options = [c for c, _ in COURSES if slot_of[c] != first_slot]
        regs.append((p, rnd.choice(options)))
    rnd.shuffle(regs)

    cands = []
    for i, (p, code) in enumerate(regs, 1):
        day, shift = slot_of[code]
        city, ccode = rnd.choice(CENTRES)
        cands.append({
            "roll": f"EX26A{i:05d}", "name": p["name"], "email": p["email"], "course": code,
            "day": day, "shift": shift, "city": city, "centre": f"{ccode}{rnd.randint(1, 4):02d}",
        })

    # ---- allocation: older export (before late registrations + centre moves), newer export --
    late = {c["roll"] for c in rnd.sample(cands, 28)}
    moved = {c["roll"] for c in rnd.sample(cands, 15)}
    old_rows = []
    for c in cands:
        if c["roll"] in late:
            continue
        city, centre = c["city"], c["centre"]
        if c["roll"] in moved:
            city, ccode = rnd.choice([x for x in CENTRES if x[0] != city])
            centre = f"{ccode}{rnd.randint(1, 4):02d}"
        old_rows.append([c["roll"], c["name"], c["email"], c["course"], course_name[c["course"]],
                         c["day"], EXAM_DATES[c["day"]].isoformat(), c["shift"], city, centre])
    W.csv("allocation/allocation_export_2026-02-10.csv",
          ["Roll No", "Candidate Name", "Email", "Course Code", "Course Name", "Exam Day",
           "Exam Date", "Shift", "Centre City", "Centre Code"],
          old_rows, stamp(dt.date(2026, 2, 10)))
    W.csv("allocation/allocation_export_2026-02-20.csv",
          ["Roll No", "Candidate Name", "Email", "Course Code", "Course Name", "Exam Day",
           "Exam Date", "Shift", "Reporting Time", "Centre City", "Centre Code"],
          [[c["roll"], c["name"], c["email"], c["course"], course_name[c["course"]], c["day"],
            EXAM_DATES[c["day"]].isoformat(), c["shift"], REPORTING[c["shift"]], c["city"], c["centre"]]
           for c in cands],
          stamp(dt.date(2026, 2, 20)))

    # ---- exam day results ---------------------------------------------------------------------
    results = {}
    for c in cands:
        if rnd.random() < 0.08:
            results[c["roll"]] = None
        else:
            results[c["roll"]] = max(0, min(100, round(rnd.gauss(difficulty[c["course"]], 15))))

    for day, shift in SLOTS:
        in_slot = [c for c in cands if (c["day"], c["shift"]) == (day, shift)]
        present = [c for c in in_slot if results[c["roll"]] is not None]
        absent = [c for c in in_slot if results[c["roll"]] is None]
        exam_end = dt.datetime.combine(EXAM_DATES[day], dt.time(int(SHIFT_TIMES[shift][1][:2])))
        when = stamp(exam_end + dt.timedelta(hours=3))
        folder = f"day_{day}/shift_{shift}"
        if day < 3:
            # Vendor A: comma separated, title-case headers, a duplicated "Remarks" column.
            rows = []
            for c in present:
                sub = exam_end - dt.timedelta(minutes=rnd.randint(0, 40))
                rows.append([c["roll"], c["name"], c["course"], results[c["roll"]], 100,
                             rnd.randint(30, 50), sub.strftime("%Y-%m-%d %H:%M"),
                             rnd.choice(["", "", "", "", "Late login", "Re-login x1"]),
                             rnd.choice(["", "Proctor: ok", "Proctor: ok", "Proctor: warning"])])
            W.csv(f"{folder}/score_day{day}_shift{shift}.csv",
                  ["Roll No", "Candidate Name", "Course Code", "Score", "Max Score", "Attempted",
                   "Submitted At", "Remarks", "Remarks"], rows, when)
            W.csv(f"{folder}/absentees_day{day}_shift{shift}.csv",
                  ["Roll No", "Candidate Name", "Course Code", "Reason"],
                  [[c["roll"], c["name"], c["course"], rnd.choice(["Absent", "Did not report"])]
                   for c in absent], when)
        else:
            # Vendor B: semicolon separated, different column names, some lower-case roll numbers.
            def roll_b(r):
                return r.lower() if rnd.random() < 0.2 else r
            score_rows = [[roll_b(c["roll"]), c["course"], results[c["roll"]], 100,
                           rnd.choice(["Submitted", "Submitted", "Auto-submitted"])] for c in present]
            absent_rows = [[roll_b(c["roll"]), c["course"], "No show"] for c in absent]
            score_h = ["Roll Number", "Course", "Marks Obtained", "Total Marks", "Status"]
            absent_h = ["Roll Number", "Course", "Remarks"]
            if shift == 1:
                W.csv(f"{folder}/score_day3_shift1.csv", score_h, score_rows, when, delimiter=";")
                W.csv(f"{folder}/absentees_day3_shift1.csv", absent_h, absent_rows, when, delimiter=";")
            else:
                zrel = f"{folder}/vendor_b_export_day3_shift2.zip"
                zpath = W.path(zrel)
                t = (exam_end + dt.timedelta(hours=2)).timetuple()[:6]
                with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
                    for name, h, rows in [("score_day3_shift2.csv", score_h, score_rows),
                                          ("absentees_day3_shift2.csv", absent_h, absent_rows)]:
                        zf.writestr(zipfile.ZipInfo(name, date_time=t), csv_bytes(h, rows, ";"),
                                    compress_type=zipfile.ZIP_DEFLATED)
                os.utime(zpath, (when, when))

    # ---- question-wise responses for day 1 shift 1 -------------------------------------------
    nq = 10
    resp_rows = []
    for c in cands:
        if (c["day"], c["shift"]) != (1, 1) or results[c["roll"]] is None:
            continue
        p_correct = results[c["roll"]] / 100
        key = [rnd.choice("ABCD") for _ in range(nq)]
        answers = [k if rnd.random() < p_correct else rnd.choice([x for x in "ABCD-" if x != k]) for k in key]
        resp_rows.append([c["roll"], c["course"], *answers, sum(a == k for a, k in zip(answers, key))])
    W.xlsx("day_1/shift_1/responses_day1_shift1.xlsx",
           {"Question-wise": (["Roll No", "Course Code", *[f"Q{i:02d}" for i in range(1, nq + 1)], "Correct"],
                              resp_rows)},
           stamp(dt.datetime(2026, 3, 7, 22)))

    # ---- assignment scores (keyed on email + course, like the course platform exports them) --
    assign_rows = []
    for c in cands:
        if rnd.random() < 0.04:
            continue
        avg = round(min(25, max(0, rnd.gauss(16, 5))), 1)
        assign_rows.append([c["email"], c["course"], avg, rnd.randint(4, 8) if avg else 0,
                            "Yes" if avg >= 10 else "No"])
    W.csv("assignments/assignment_scores_2026-03-01.csv",
          ["Email", "Course ID", "Assignment Avg (out of 25)", "Assignments Submitted", "Eligible"],
          assign_rows, stamp(dt.date(2026, 3, 1)))

    # ---- planning workbook with two tabs -------------------------------------------------------
    counts = {code: sum(c["course"] == code for c in cands) for code, _ in COURSES}
    coordinators = {}
    for code, _ in COURSES:
        coordinators[code] = ("Dr. " + "".join(rnd.choice(SYL) for _ in range(2)).capitalize()
                              + " " + rnd.choice(LAST_A) + rnd.choice(LAST_B))
    W.xlsx("sheets/exam_planner_v3.xlsx", {
        "Courses": (["Course Code", "Course Name", "Coordinator", "Expected Candidates"],
                    [[code, name, coordinators[code], counts[code]] for code, name in COURSES]),
        "Shift Plan": (["Day", "Date", "Shift", "Start", "End", "Courses"],
                       [[d, EXAM_DATES[d], s, *SHIFT_TIMES[s],
                         ", ".join(code for code, _ in COURSES if slot_of[code] == (d, s))] for d, s in SLOTS]),
    }, stamp(dt.date(2026, 2, 5)))

    # ---- a file nobody wrote a rule for --------------------------------------------------------
    p = W.path("misc/vendor_notes.txt")
    p.write_text("Vendor B: day 3 shift 2 files are sent zipped. Roll numbers may be lower-case.\n")
    os.utime(p, (stamp(dt.date(2026, 3, 14), 21),) * 2)

    return W.files


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=HERE / "raw")
    args = ap.parse_args()
    files = generate(args.out)
    print(f"Wrote {len(files)} files to {os.path.relpath(args.out)}:")
    for f in sorted(files):
        print(f"  {f}")


if __name__ == "__main__":
    main()
