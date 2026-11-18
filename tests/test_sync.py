"""Snapshot versioning and incremental refresh, using throwaway folders.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from warehouse import rules, sync  # noqa: E402

HEADER = "Roll No,Candidate Name,Email,Course Code,Exam Day,Shift\n"
T0 = 1_772_000_000  # a fixed epoch second; only the ordering matters


def allocation(rolls: list[str]) -> str:
    return HEADER + "".join(f"{r},Test Person {r},{r.lower()}@example.com,CS101,1,1\n" for r in rolls)


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw = Path(self.tmp.name) / "raw"
        self.raw.mkdir()
        self.con = sync.open_db(Path(self.tmp.name) / "w.sqlite")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def write(self, rel: str, text: str, mtime: float) -> None:
        p = self.raw / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        os.utime(p, (mtime, mtime))

    def rolls(self) -> set[str]:
        return {r[0] for r in self.con.execute("SELECT roll_no FROM candidates")}

    def sources(self) -> set[str]:
        return {r[0] for r in self.con.execute("SELECT DISTINCT source_file FROM candidates")}

    def actions(self) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in self.con.execute("SELECT action, source_file FROM refresh_log ORDER BY id")]

    # ------------------------------------------------------------------------------------------
    def test_newer_export_replaces_older(self):
        self.write("allocation/old.csv", allocation(["A1", "A2"]), T0)
        sync.apply(self.con, self.raw)
        self.write("allocation/new.csv", allocation(["A1", "A2", "A3"]), T0 + 3600)
        plan = sync.make_plan(self.con, self.raw)
        self.assertEqual([f.rel for f in plan.new], ["allocation/new.csv"])
        sync.apply(self.con, self.raw)
        self.assertEqual(self.rolls(), {"A1", "A2", "A3"})
        self.assertEqual(self.sources(), {"allocation/new.csv"})
        self.assertIn(("replaced", "allocation/new.csv"), self.actions())
        self.assertEqual(sync.make_plan(self.con, self.raw).todo, 0)

    def test_older_copy_arriving_later_is_superseded(self):
        self.write("allocation/new.csv", allocation(["A1", "A2", "A3"]), T0 + 3600)
        sync.apply(self.con, self.raw)
        self.write("allocation/old.csv", allocation(["A1"]), T0)
        sync.apply(self.con, self.raw)
        self.assertEqual(self.sources(), {"allocation/new.csv"})
        self.assertEqual(self.actions()[-1], ("superseded", "allocation/old.csv"))
        status = self.con.execute("SELECT status FROM loaded_files WHERE source_file='allocation/old.csv'").fetchone()[0]
        self.assertEqual(status, "superseded")

    def test_removed_file_deletes_rows_and_promotes_previous_copy(self):
        self.write("allocation/old.csv", allocation(["A1"]), T0)
        self.write("allocation/new.csv", allocation(["A1", "A2"]), T0 + 3600)
        sync.apply(self.con, self.raw)
        (self.raw / "allocation/new.csv").unlink()
        plan = sync.make_plan(self.con, self.raw)
        self.assertEqual(plan.removed, ["allocation/new.csv"])
        sync.apply(self.con, self.raw)
        self.assertEqual(self.sources(), {"allocation/old.csv"})   # older copy promoted back
        self.assertIn(("removed", "allocation/new.csv"), self.actions())
        (self.raw / "allocation/old.csv").unlink()
        sync.apply(self.con, self.raw)
        self.assertEqual(self.rolls(), set())
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM loaded_files").fetchone()[0], 0)

    def test_changed_file_is_reloaded(self):
        self.write("day_1/shift_1/score_a.csv", "Roll No,Course Code,Score,Max Score\nA1,CS101,40,100\n", T0)
        sync.apply(self.con, self.raw)
        self.write("day_1/shift_1/score_a.csv", "Roll No,Course Code,Score,Max Score\nA1,CS101,45,100\n", T0 + 60)
        plan = sync.make_plan(self.con, self.raw)
        self.assertEqual([f.rel for f, _ in plan.changed], ["day_1/shift_1/score_a.csv"])
        sync.apply(self.con, self.raw)
        self.assertEqual([tuple(r) for r in self.con.execute("SELECT score FROM exam_results")], [(45.0,)])

    def test_unknown_file_is_reported_not_ignored(self):
        self.write("misc/readme.txt", "hello", T0)
        plan = sync.make_plan(self.con, self.raw)
        self.assertEqual(plan.unmatched, [("misc/readme.txt", rules.NO_RULE)])
        sync.apply(self.con, self.raw)
        self.assertEqual(self.actions(), [("skipped", "misc/readme.txt")])
        self.assertEqual(sync.make_plan(self.con, self.raw).todo, 0)

    def test_classify(self):
        self.assertEqual(rules.classify("day_2/shift_1/score_x.csv"), ("exam_scores", "scores/day_2/shift_1"))
        self.assertEqual(rules.classify("day_3/shift_2/b.zip_extracted/absentees_y.csv"),
                         ("absentees", "absentees/day_3/shift_2"))
        self.assertEqual(rules.classify("sheets/Exam Planner_v2.xlsx"), ("sheet_exam_planner", "sheets/exam_planner"))
        self.assertEqual(rules.classify("notes.docx"), (None, rules.NO_RULE))


if __name__ == "__main__":
    unittest.main()
