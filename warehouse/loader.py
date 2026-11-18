"""Load one source file at a time into SQLite, with provenance and snapshot versioning.

* Values are stored as TEXT exactly as they appear in the source. Interpretation
  (casting scores, unifying vendor column names) happens in views, never at load
  time, so the raw tables can always be traced back to a file and a row.
* Every row carries ``source_file`` (path relative to the raw folder) and
  ``source_row`` (record number within that file or worksheet).
* ``loaded_files`` is the state table: one row per file (per worksheet for
  workbooks loaded tab by tab). ``refresh_log`` is append-only history.
* Snapshot rule: within a group key only the newest file (by mtime) has rows in
  the database. A newer re-export *replaces* the active copy; an older copy that
  turns up later is recorded as *superseded* and not loaded.
"""
from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .rules import Match, snake

STATE_SQL = """
CREATE TABLE IF NOT EXISTS loaded_files (
    source_file TEXT NOT NULL,
    unit        TEXT NOT NULL DEFAULT '',
    "table"     TEXT,
    group_key   TEXT,
    rows        INTEGER NOT NULL DEFAULT 0,
    sha1        TEXT NOT NULL,
    mtime       REAL NOT NULL,
    loaded_at   TEXT NOT NULL,
    status      TEXT NOT NULL,   -- loaded | superseded | skipped
    PRIMARY KEY (source_file, unit)
);
CREATE TABLE IF NOT EXISTS refresh_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    action      TEXT NOT NULL
                CHECK (action IN ('loaded', 'replaced', 'superseded', 'removed', 'skipped')),
    source_file TEXT NOT NULL,
    "table"     TEXT,
    rows        INTEGER NOT NULL DEFAULT 0,
    note        TEXT
);
"""

RESERVED_COLUMNS = {"source_file", "source_row", "rowid", "oid", "_rowid_"}


@dataclass(frozen=True)
class SourceFile:
    rel: str      # path relative to the raw folder, forward slashes
    path: Path    # absolute path on disk
    sha1: str
    mtime: float


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def qi(name: str) -> str:
    """Quote an SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def init_state(con: sqlite3.Connection) -> None:
    con.executescript(STATE_SQL)


# --------------------------------------------------------------------------- reading

def clean_headers(headers: Iterable[str]) -> list[str]:
    """snake_case column names; blanks become col_N; duplicates become name_2, name_3 ..."""
    out: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(headers, 1):
        base = snake(raw) or f"col_{i}"
        if base[0].isdigit():
            base = "c_" + base
        if base in RESERVED_COLUMNS:
            base += "_src"
        name, n = base, 1
        while name in seen:
            n += 1
            name = f"{base}_{n}"
        seen.add(name)
        out.append(name)
    return out


def cell_text(value) -> str:
    """Render a spreadsheet cell the way a person would read it, as text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, _dt.datetime):
        return value.date().isoformat() if value.time() == _dt.time() else value.isoformat(sep=" ")
    if isinstance(value, (_dt.date, _dt.time)):
        return value.isoformat()
    return str(value)


def _split_header(rows: list[list[str]]) -> tuple[list[str], list[tuple[int, list[str]]]]:
    """First non-blank row is the header; blank rows are dropped; ragged rows are padded."""
    def blank(r):
        return not any(str(c).strip() for c in r)

    def width(r):
        for i in range(len(r) - 1, -1, -1):
            if str(r[i]).strip():
                return i + 1
        return 0

    start = next((i for i, r in enumerate(rows) if not blank(r)), None)
    if start is None:
        return [], []
    header = rows[start]
    data = [(i + 1, r) for i, r in enumerate(rows) if i > start and not blank(r)]
    w = max([width(header)] + [width(r) for _, r in data])
    pad = lambda r: [str(c) for c in r[:w]] + [""] * (w - len(r))  # noqa: E731
    return pad(header), [(n, pad(r)) for n, r in data]


def read_csv(path: Path):
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    first_line = text.split("\n", 1)[0]
    delim = max(",;\t|", key=first_line.count)  # vendors disagree on delimiters
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delim))
    header, data = _split_header(rows)
    return [("", header, data)] if header else []


def read_xlsx(path: Path, all_tabs: bool):
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = []
        for ws in wb.worksheets:
            rows = [[cell_text(v) for v in r] for r in ws.iter_rows(values_only=True)]
            header, data = _split_header(rows)
            if not header:
                continue
            sheets.append((ws.title, header, data))
            if not all_tabs:
                break
        return sheets
    finally:
        wb.close()


# --------------------------------------------------------------------------- writing

def table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({qi(table)})")]


def ensure_table(con: sqlite3.Connection, table: str, columns: list[str]) -> None:
    existing = table_columns(con, table)
    if not existing:
        cols = ", ".join(f"{qi(c)} TEXT" for c in columns)
        con.execute(f"CREATE TABLE {qi(table)} (source_file TEXT NOT NULL, source_row INTEGER"
                    f"{', ' + cols if cols else ''})")
        con.execute(f"CREATE INDEX IF NOT EXISTS {qi('ix_' + table + '_source_file')} "
                    f"ON {qi(table)}(source_file)")
        return
    for c in columns:  # a re-export may add columns; old rows simply have NULL there
        if c not in existing:
            con.execute(f"ALTER TABLE {qi(table)} ADD COLUMN {qi(c)} TEXT")


def load_file(con: sqlite3.Connection, f: SourceFile, m: Match) -> list[tuple[str, str, int]]:
    """Insert one file's rows. Returns [(unit, table, rows)]; unit is the worksheet name or ''."""
    suffix = f.path.suffix.lower()
    if suffix == ".csv":
        sheets = read_csv(f.path)
    elif suffix in (".xlsx", ".xlsm"):
        sheets = read_xlsx(f.path, all_tabs=m.per_tab)
    else:
        raise ValueError(f"don't know how to read {suffix} files")
    units = []
    for unit, header, data in sheets:
        table = f"{m.table}_{snake(unit) or 'sheet'}" if m.per_tab else m.table
        cols = clean_headers(header)
        ensure_table(con, table, cols)
        placeholders = ", ".join("?" * (len(cols) + 2))
        con.executemany(
            f"INSERT INTO {qi(table)} (source_file, source_row, {', '.join(map(qi, cols))}) "
            f"VALUES ({placeholders})",
            ([f.rel, n, *vals] for n, vals in data),
        )
        units.append((unit, table, len(data)))
    return units or [("", m.table, 0)]


# --------------------------------------------------------------------------- state

def log(con, action: str, source_file: str, table: str | None, rows: int, note: str | None = None):
    con.execute('INSERT INTO refresh_log (at, action, source_file, "table", rows, note) '
                'VALUES (?, ?, ?, ?, ?, ?)', (now(), action, source_file, table, rows, note))


def state_rows(con, source_file: str) -> list[sqlite3.Row]:
    con.row_factory = sqlite3.Row
    return con.execute("SELECT * FROM loaded_files WHERE source_file = ? ORDER BY unit",
                       (source_file,)).fetchall()


def _record(con, f: SourceFile, unit: str, table, group, rows: int, status: str):
    con.execute('INSERT OR REPLACE INTO loaded_files '
                '(source_file, unit, "table", group_key, rows, sha1, mtime, loaded_at, status) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (f.rel, unit, table, group, rows, f.sha1, f.mtime, now(), status))


def _delete_rows(con, source_file: str) -> int:
    """Delete a file's data rows (not its state). Returns the number of rows removed."""
    n = 0
    for r in state_rows(con, source_file):
        if r["status"] == "loaded" and r["table"] and table_columns(con, r["table"]):
            n += con.execute(f"DELETE FROM {qi(r['table'])} WHERE source_file = ?",
                             (source_file,)).rowcount
    return n


def _active(con, group_key: str, exclude: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM loaded_files WHERE group_key = ? AND status = 'loaded' "
                       "AND source_file <> ? LIMIT 1", (group_key, exclude)).fetchone()


def _load_and_record(con, f: SourceFile, m: Match):
    units = load_file(con, f, m)
    for unit, table, rows in units:
        _record(con, f, unit, table, m.group_key, rows, "loaded")
    return units


def ingest(con: sqlite3.Connection, f: SourceFile, m: Match | None, reason: str | None = None) -> None:
    """Bring one new or changed file into the database, applying the snapshot rule."""
    with con:
        previous = state_rows(con, f.rel)
        was_loaded = any(r["status"] == "loaded" for r in previous)
        old_rows = _delete_rows(con, f.rel)
        con.execute("DELETE FROM loaded_files WHERE source_file = ?", (f.rel,))

        if m is None:
            _record(con, f, "", None, None, 0, "skipped")
            log(con, "skipped", f.rel, None, 0, reason)
            return

        active = _active(con, m.group_key, f.rel)
        if active is not None and (active["mtime"], active["source_file"]) > (f.mtime, f.rel):
            _record(con, f, "", m.table, m.group_key, 0, "superseded")
            log(con, "superseded", f.rel, m.table, 0,
                f"older than {active['source_file']} (group {m.group_key}); kept on record, not loaded")
            return

        if active is not None:
            gone = _delete_rows(con, active["source_file"])
            con.execute("UPDATE loaded_files SET status = 'superseded', rows = 0 "
                        "WHERE source_file = ?", (active["source_file"],))
            note = f"newer export replaces {active['source_file']} ({gone} rows removed)"
            action = "replaced"
        elif was_loaded:
            note, action = f"file changed on disk; {old_rows} old rows removed", "replaced"
        else:
            note, action = None, "loaded"

        for unit, table, rows in _load_and_record(con, f, m):
            log(con, action, f.rel, table, rows, note if not unit else f"tab '{unit}'" + (f"; {note}" if note else ""))


def remove(con: sqlite3.Connection, source_file: str, present: Mapping[str, SourceFile],
           pending: set[str], matcher: Callable[[str], Match | None]) -> None:
    """A file disappeared from the folder: delete its rows and, if it was the active copy of a
    snapshot group, promote the newest remaining copy (unless that copy is about to be ingested
    anyway because it is new or changed in this same run)."""
    with con:
        previous = state_rows(con, source_file)
        if not previous:
            return
        group = previous[0]["group_key"]
        was_active = any(r["status"] == "loaded" for r in previous)
        n = _delete_rows(con, source_file)
        con.execute("DELETE FROM loaded_files WHERE source_file = ?", (source_file,))
        tables = ", ".join(sorted({r["table"] for r in previous if r["table"]})) or None
        log(con, "removed", source_file, tables, n, "file no longer in the folder")

        if not (was_active and group):
            return
        candidates = [r for r in con.execute(
            "SELECT DISTINCT source_file, mtime FROM loaded_files "
            "WHERE group_key = ? AND status = 'superseded'", (group,))
            if r["source_file"] in present and r["source_file"] not in pending]
        if not candidates:
            return
        best = max(candidates, key=lambda r: (r["mtime"], r["source_file"]))
        f = present[best["source_file"]]
        m = matcher(f.rel)
        if m is None:
            return
        con.execute("DELETE FROM loaded_files WHERE source_file = ?", (f.rel,))
        for unit, table, rows in _load_and_record(con, f, m):
            log(con, "loaded", f.rel, table, rows,
                f"promoted: newest remaining copy in {group} after {source_file} was removed")
