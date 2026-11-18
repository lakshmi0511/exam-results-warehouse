"""Keep the SQLite warehouse in sync with the shared folder.

    python -m warehouse.sync plan       # what would change? (touches no data)
    python -m warehouse.sync apply      # load only new / changed / removed files
    python -m warehouse.sync status     # recent refresh_log entries and table counts
    python -m warehouse.sync rebuild    # delete the database and load everything again

Options: --raw DIR (default sample/raw), --db FILE (default db/warehouse.sqlite).
Environment variables WAREHOUSE_RAW / WAREHOUSE_DB work too.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import zipfile
import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path

from . import loader, rules
from .loader import SourceFile
from .views import build_views

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RAW = Path(os.environ.get("WAREHOUSE_RAW", ROOT / "sample" / "raw"))
DEFAULT_DB = Path(os.environ.get("WAREHOUSE_DB", ROOT / "db" / "warehouse.sqlite"))
MARKER = ".extracted_from"


@dataclass
class Plan:
    files: dict[str, SourceFile] = field(default_factory=dict)
    new: list[SourceFile] = field(default_factory=list)
    changed: list[tuple[SourceFile, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0
    unmatched: list[tuple[str, str]] = field(default_factory=list)   # present files with no rule
    archives: list[str] = field(default_factory=list)
    ignored: list[tuple[str, str]] = field(default_factory=list)
    extracted: list[str] = field(default_factory=list)

    @property
    def todo(self) -> int:
        return len(self.new) + len(self.changed) + len(self.removed)


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    loader.init_state(con)
    return con


# --------------------------------------------------------------------------- folder

def extract_zips(raw: Path) -> list[str]:
    """Extract each zip into '<name>.zip_extracted/' next to it (only when the zip changed),
    and remove extracted folders whose zip has gone. Returns messages."""
    messages = []
    for z in sorted(raw.rglob("*.zip")):
        if any(p.endswith(rules.ZIP_DIR_SUFFIX) for p in z.relative_to(raw).parts[:-1]):
            continue  # nested archives are not unpacked
        target = z.with_name(z.name + "_extracted")
        digest = loader.sha1_of(z)
        marker = target / MARKER
        if marker.exists() and marker.read_text().strip() == digest:
            continue
        if target.exists():
            shutil.rmtree(target)
        target.mkdir()
        root = target.resolve()
        with zipfile.ZipFile(z) as zf:
            for info in zf.infolist():
                dest = (target / info.filename).resolve()
                if root not in dest.parents:  # zip-slip guard
                    messages.append(f"refused unsafe path in {z.name}: {info.filename}")
                    continue
                if info.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                stamp = _dt.datetime(*info.date_time).timestamp()  # keep the export time
                os.utime(dest, (stamp, stamp))
        marker.write_text(digest)
        messages.append(f"extracted {z.relative_to(raw).as_posix()} -> {target.name}/")
    for d in sorted(raw.rglob("*" + rules.ZIP_DIR_SUFFIX)):
        zip_path = d.with_name(d.name[: -len("_extracted")])
        if d.is_dir() and (d / MARKER).exists() and not zip_path.exists():
            shutil.rmtree(d)
            messages.append(f"removed {d.relative_to(raw).as_posix()}/ (its zip is gone)")
    return messages


def scan(raw: Path, plan: Plan) -> None:
    for dirpath, dirnames, filenames in os.walk(raw):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(raw).as_posix()
            if name == MARKER:
                continue
            if name.startswith("."):
                plan.ignored.append((rel, "hidden file"))
            elif name.lower().endswith(".zip"):
                plan.archives.append(rel)
            else:
                plan.files[rel] = SourceFile(rel, path, loader.sha1_of(path), path.stat().st_mtime)


def make_plan(con: sqlite3.Connection, raw: Path) -> Plan:
    if not raw.is_dir():
        raise SystemExit(f"raw folder not found: {raw}")
    plan = Plan(extracted=extract_zips(raw))
    scan(raw, plan)
    state = {r["source_file"]: r for r in con.execute(
        "SELECT source_file, sha1, mtime FROM loaded_files GROUP BY source_file")}
    for rel, f in plan.files.items():
        if rules.match(rel) is None:
            plan.unmatched.append((rel, rules.classify(rel)[1]))
        old = state.get(rel)
        if old is None:
            plan.new.append(f)
        elif old["sha1"] != f.sha1:
            plan.changed.append((f, "content changed"))
        elif abs(old["mtime"] - f.mtime) > 1e-6:
            plan.changed.append((f, "modification time changed"))
        else:
            plan.unchanged += 1
    plan.removed = sorted(set(state) - set(plan.files))
    return plan


def apply(con: sqlite3.Connection, raw: Path, plan: Plan | None = None) -> Plan:
    plan = plan or make_plan(con, raw)
    pending = {f.rel for f in plan.new} | {f.rel for f, _ in plan.changed}
    for rel in plan.removed:
        loader.remove(con, rel, plan.files, pending, rules.match)
    # Oldest first, as if the files had arrived in that order: the log tells the story.
    todo = sorted(plan.new + [f for f, _ in plan.changed], key=lambda f: (f.mtime, f.rel))
    for f in todo:
        m = rules.match(f.rel)
        loader.ingest(con, f, m, None if m else rules.classify(f.rel)[1])
    build_views(con)
    return plan


# --------------------------------------------------------------------------- CLI output

def _rel(p: Path) -> str:
    try:
        return os.path.relpath(p)
    except ValueError:
        return str(p)


def print_plan(plan: Plan, raw: Path, db: Path) -> None:
    print(f"Folder {_rel(raw)}  ->  {_rel(db)}")
    for msg in plan.extracted:
        print(f"  zip     {msg}")
    def target(rel):
        t = rules.classify(rel)
        return f"{t[0]} [{t[1]}]" if t[0] else f"(skipped: {t[1]})"
    print(f"  new       {len(plan.new)}")
    for f in plan.new:
        print(f"    + {f.rel}  ->  {target(f.rel)}")
    print(f"  changed   {len(plan.changed)}")
    for f, why in plan.changed:
        print(f"    ~ {f.rel}  ({why})")
    print(f"  removed   {len(plan.removed)}")
    for rel in plan.removed:
        print(f"    - {rel}")
    print(f"  unchanged {plan.unchanged}")
    if plan.archives:
        print(f"  archives  {len(plan.archives)} (contents extracted and classified): {', '.join(plan.archives)}")
    for rel, reason in plan.unmatched:
        print(f"  no rule   {rel}  ({reason})")
    for rel, reason in plan.ignored:
        print(f"  ignored   {rel}  ({reason})")


def print_status(con: sqlite3.Connection, n: int) -> None:
    rows = con.execute("SELECT * FROM (SELECT * FROM refresh_log ORDER BY id DESC LIMIT ?) ORDER BY id",
                       (n,)).fetchall()
    print(f"Last {len(rows)} refresh_log entries")
    for r in rows:
        note = f"  -- {r['note']}" if r["note"] else ""
        print(f"  {r['at'][5:16].replace('T', ' ')}  {r['action']:<10} {r['rows']:>5}  "
              f"{(r['table'] or '-'):<30} {r['source_file']}{note}")
    print("\nTables")
    for r in con.execute('SELECT "table", COUNT(*) AS files, SUM(rows) AS rows FROM loaded_files '
                         'WHERE status = \'loaded\' GROUP BY "table" ORDER BY "table"'):
        print(f"  {r['table']:<34} {r['rows']:>6} rows  from {r['files']} file(s)")
    sup = con.execute("SELECT COUNT(DISTINCT source_file) FROM loaded_files WHERE status='superseded'").fetchone()[0]
    skp = con.execute("SELECT COUNT(DISTINCT source_file) FROM loaded_files WHERE status='skipped'").fetchone()[0]
    print(f"  ({sup} superseded file(s) kept on record, {skp} file(s) with no rule)")


def _print_new_log(con, since_id: int) -> None:
    rows = con.execute("SELECT * FROM refresh_log WHERE id > ? ORDER BY id", (since_id,)).fetchall()
    for r in rows:
        note = f"  -- {r['note']}" if r["note"] else ""
        print(f"  {r['action']:<10} {r['rows']:>5}  {(r['table'] or '-'):<30} {r['source_file']}{note}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m warehouse.sync", description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["plan", "apply", "status", "rebuild"])
    ap.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("-n", type=int, default=20, help="status: number of log rows (default 20)")
    args = ap.parse_args(argv)

    if args.command == "rebuild":
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(args.db) + suffix).unlink(missing_ok=True)
    con = open_db(args.db)
    try:
        if args.command == "status":
            print_status(con, args.n)
            return 0
        plan = make_plan(con, args.raw)
        print_plan(plan, args.raw, args.db)
        if args.command == "plan":
            print("\nNothing to do: the database matches the folder." if not plan.todo else
                  f"\n{plan.todo} file(s) to process. Run `python -m warehouse.sync apply`.")
            return 0
        if not plan.todo:
            build_views(con)
            print("\nNothing to do.")
            return 0
        last = con.execute("SELECT COALESCE(MAX(id), 0) FROM refresh_log").fetchone()[0]
        print("\nApplying")
        apply(con, args.raw, plan)
        _print_new_log(con, last)
        print("\nDone. Views exam_results and candidate_lookup rebuilt.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
