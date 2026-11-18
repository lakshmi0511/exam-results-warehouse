#!/usr/bin/env python3
"""A small, read-only browser UI over the warehouse. Standard library only.

    python browse/app.py            # http://127.0.0.1:8001
    python browse/app.py --port 8010 --db path/to/warehouse.sqlite

Safety: binds to 127.0.0.1 only, opens SQLite read-only (mode=ro + query_only),
uses parameterised SQL everywhere, and only serves tables/views that exist in
sqlite_master (identifiers are never taken from the request as-is).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from warehouse.loader import qi, table_columns  # noqa: E402
from warehouse.query import connect_ro, person, search  # noqa: E402
from warehouse.views import table_exists  # noqa: E402

HOST = "127.0.0.1"
PAGE_SIZE = 50
HTML = Path(__file__).with_name("app.html")
DB_PATH = Path(os.environ.get("WAREHOUSE_DB", ROOT / "db" / "warehouse.sqlite"))
_local = threading.local()


def db():
    con = getattr(_local, "con", None)
    if con is None:
        con = _local.con = connect_ro(DB_PATH)
    return con


def rows(sql, params=()):
    return [dict(r) for r in db().execute(sql, params)]


def known_tables() -> list[str]:
    return [r["name"] for r in db().execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
        "AND name NOT LIKE 'sqlite_%' ORDER BY type DESC, name")]


# --------------------------------------------------------------------------- routes

def api_search(q):
    return {"results": search(db(), q.get("q", ""))}


def api_person(q):
    roll = q.get("roll", "").strip()
    if not roll:
        raise ValueError("roll is required")
    return person(db(), roll)


def api_overview(q):
    if not table_exists(db(), "candidate_lookup"):
        return {"slots": [], "totals": {}}
    slots = rows("""
        SELECT exam_day, shift, COUNT(*) AS scheduled,
               SUM(attendance = 'present') AS present, SUM(attendance = 'absent') AS absent,
               SUM(attendance IS NULL) AS no_result, ROUND(AVG(pct), 1) AS avg_pct,
               GROUP_CONCAT(DISTINCT course_code) AS courses
        FROM candidate_lookup GROUP BY exam_day, shift
        ORDER BY CAST(exam_day AS INTEGER), CAST(shift AS INTEGER)""")
    totals = rows("""
        SELECT COUNT(*) AS registrations, COUNT(DISTINCT email) AS people,
               COUNT(DISTINCT course_code) AS courses,
               SUM(attendance = 'present') AS present, SUM(attendance = 'absent') AS absent,
               ROUND(AVG(pct), 1) AS avg_pct FROM candidate_lookup""")[0]
    return {"slots": slots, "totals": totals}


def api_courses(q):
    if not table_exists(db(), "candidate_lookup"):
        return {"courses": []}
    return {"courses": rows("""
        SELECT course_code, MAX(course_name) AS course_name, MIN(exam_day) AS exam_day, MIN(shift) AS shift,
               COUNT(*) AS registered, SUM(attendance = 'present') AS present,
               SUM(attendance = 'absent') AS absent, ROUND(AVG(pct), 1) AS avg_pct,
               ROUND(AVG(CAST(assignment_avg AS REAL)), 1) AS avg_assignment
        FROM candidate_lookup GROUP BY course_code ORDER BY course_code""")}


def api_course(q):
    cid = q.get("id", "").strip().upper()
    if not cid or not table_exists(db(), "candidate_lookup"):
        raise ValueError("id is required")
    summary = rows("""
        SELECT course_code, MAX(course_name) AS course_name, COUNT(*) AS registered,
               SUM(attendance = 'present') AS present, SUM(attendance = 'absent') AS absent,
               ROUND(AVG(pct), 1) AS avg_pct, MIN(pct) AS min_pct, MAX(pct) AS max_pct
        FROM candidate_lookup WHERE course_code = ?""", (cid,))[0]
    buckets = {r["b"]: r["n"] for r in rows("""
        SELECT MIN(CAST(pct / 10 AS INTEGER), 9) AS b, COUNT(*) AS n
        FROM candidate_lookup WHERE course_code = ? AND pct IS NOT NULL GROUP BY b""", (cid,))}
    histogram = [{"bucket": f"{i * 10}-{i * 10 + 10}%", "count": buckets.get(i, 0)} for i in range(10)]
    top = rows("""SELECT roll_no, name, pct FROM candidate_lookup WHERE course_code = ? AND pct IS NOT NULL
                  ORDER BY pct DESC, roll_no LIMIT 5""", (cid,))
    return {"summary": summary, "histogram": histogram, "top": top}


def api_table(q):
    tables = known_tables()
    name = q.get("name", "")
    if not name:
        return {"tables": [{"name": t, "rows": db().execute(f"SELECT COUNT(*) FROM {qi(t)}").fetchone()[0]}
                           for t in tables]}
    if name not in tables:                       # whitelist: only names that exist in the schema
        raise ValueError("unknown table")
    cols = table_columns(db(), name)
    term = q.get("q", "").strip()
    where, params = "", []
    if term:
        where = "WHERE " + " OR ".join(f"CAST({qi(c)} AS TEXT) LIKE ?" for c in cols)
        params = [f"%{term}%"] * len(cols)
    total = db().execute(f"SELECT COUNT(*) FROM {qi(name)} {where}", params).fetchone()[0]
    pages = max(1, math.ceil(total / PAGE_SIZE))
    try:
        page = min(max(1, int(q.get("page", "1"))), pages)
    except ValueError:
        page = 1
    data = db().execute(f"SELECT * FROM {qi(name)} {where} LIMIT ? OFFSET ?",
                        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
    return {"name": name, "columns": cols, "rows": [list(r) for r in data],
            "total": total, "page": page, "pages": pages}


def api_log(q):
    return {
        "log": rows("SELECT * FROM refresh_log ORDER BY id DESC LIMIT 300"),
        "files": rows('SELECT source_file, unit, "table", group_key, rows, status, loaded_at '
                      'FROM loaded_files ORDER BY source_file, unit'),
    }


ROUTES = {
    "/api/search": api_search, "/api/person": api_person, "/api/overview": api_overview,
    "/api/courses": api_courses, "/api/course": api_course, "/api/table": api_table, "/api/log": api_log,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "warehouse-browse/1.0"

    def _send(self, status, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, default=str).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
        route = ROUTES.get(url.path)
        if route is None:
            return self._json(404, {"error": "not found"})
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            self._json(200, route(params))
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:  # keep the server up; report the error to the page
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt, *args):
        sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")


def main():
    global DB_PATH
    ap = argparse.ArgumentParser(description="Browse the exam results warehouse.")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8001)))
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()
    DB_PATH = args.db
    connect_ro(DB_PATH).close()  # fail fast if the database is missing
    httpd = ThreadingHTTPServer((HOST, args.port), Handler)
    print(f"Serving {os.path.relpath(DB_PATH)} read-only at http://{HOST}:{args.port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
