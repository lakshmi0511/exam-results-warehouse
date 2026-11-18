# exam-results-warehouse

Turn a messy shared folder of exam-day exports into one searchable SQLite database,
keep it in sync incrementally, and give support staff a one-line candidate lookup and a
small browser UI.

Python 3.10+, one dependency (`openpyxl`). No server, no build step.

## The problem

A national online exam: tens of thousands of candidates, several exam days, two shifts
per day. On every exam day the results arrive in a shared folder as exports from
different vendors:

- the seat **allocation** list, re-exported every few days as registrations and centre moves come in;
- **score** and **absentee** files for each day and shift, in CSV with different column
  names and delimiters depending on the vendor, some sent as **zip** files;
- **question-wise responses** as XLSX workbooks;
- **assignment scores** from the course platform, keyed on email rather than roll number;
- planning **workbooks** with several tabs, and the odd file nobody expected.

Support staff kept getting the same question ("candidate X says their score is missing,
what do we have?") and answering it meant opening five spreadsheets. Re-exports made it
worse: which copy of the allocation list is the current one?

## The approach

```
shared folder (CSV / XLSX / ZIP)      warehouse/rules.py     warehouse/loader.py        SQLite
──────────────────────────────── ──►  classify by path  ──►  load as TEXT, + source_file ──►  raw tables
                                      (table, group_key)     snapshot per group_key          + views
                                                             loaded_files / refresh_log      │
                                                                                  lookup.py ◄┤
                                                                             browse/app.py  ◄┘
```

1. **Classify by path, not by content.** `rules.py` maps each file's relative path to a
   table and a *group key* (`day_2/shift_1/score_*.csv` → `exam_scores`, group
   `scores/day_2/shift_1`). Folder conventions are something people already follow;
   sniffing headers is not.
2. **Load raw, interpret later.** Every value is stored as TEXT exactly as it appeared in the
   file. Column names are cleaned to snake_case (duplicates become `remarks`, `remarks_2`),
   nothing else. Vendor differences (`roll_no` vs `roll_number`, `score` vs `marks_obtained`)
   are reconciled in views, so a new alias never needs a reload.
3. **Sync incrementally.** A state table remembers each file's SHA-1 and mtime; `plan`
   compares the folder against it and `apply` touches only what changed.

## Features

- **Provenance for every row.** Each row carries `source_file` and `source_row`. Lookup and UI
  show "from `day_1/shift_2/score_day1_shift2.csv` row 56" next to every value.
- **Incremental refresh.** New, changed (SHA-1 or mtime) and removed files are detected; only
  those are loaded or deleted. A second `plan` right after `apply` says *nothing to do*.
- **Snapshot versioning.** Within a group key only the newest file (by mtime) has rows in the
  database. A newer re-export *replaces* the old one; an older copy turning up later is recorded
  as *superseded*; if the active copy is deleted, the newest remaining copy is promoted back.
- **Nothing silently ignored.** Files without a rule are reported by `plan` on every run
  ("no rule for this file") and logged as `skipped`. Zips are extracted next to themselves into
  `<name>.zip_extracted/` and their contents classified like loose files.
- **Full history.** `refresh_log` records every `loaded` / `replaced` / `superseded` /
  `removed` / `skipped` event with row counts and a note.
- **Candidate lookup** by roll number, email or name fragment, resolving roll ↔ email through the
  allocation list (so you also see a person's other registrations and their assignment score).
- **Browser UI** on `127.0.0.1` only, read-only SQLite, parameterised SQL, whitelisted table names.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate     # or: uv venv .venv --python 3.11
pip install -r requirements.txt                          # openpyxl

python sample/generate.py            # writes a fictional sample folder to sample/raw/
python -m warehouse.sync rebuild     # loads it into db/warehouse.sqlite
python -m warehouse.sync plan        # -> Nothing to do: the database matches the folder.
python lookup.py dede.halford94@example.com
python browse/app.py                 # http://127.0.0.1:8001
```

The sample is deterministic (seeded) and entirely made up: about 600 registrations across 12
courses, 3 exam days × 2 shifts, two vendor formats, one zip, an allocation list exported twice,
a two-tab planning workbook and one file with no rule.

`rebuild` on the sample tells the snapshot story by itself, because files are processed oldest
first:

```
Applying
  loaded        12  sheet_exam_planner_courses     sheets/exam_planner_v3.xlsx  -- tab 'Courses'
  loaded         6  sheet_exam_planner_shift_plan  sheets/exam_planner_v3.xlsx  -- tab 'Shift Plan'
  loaded       577  candidates                     allocation/allocation_export_2026-02-10.csv
  replaced     605  candidates                     allocation/allocation_export_2026-02-20.csv  -- newer export replaces allocation/allocation_export_2026-02-10.csv (577 rows removed)
  loaded       573  assignment_scores              assignments/assignment_scores_2026-03-01.csv
  ...
  skipped        0  -                              misc/vendor_notes.txt  -- no rule for this file
```

A later day in the folder (a third allocation export, one score file corrected in place, the
notes file deleted):

```
$ python -m warehouse.sync plan
  new       1
    + allocation/allocation_export_2026-03-02.csv  ->  candidates [allocation]
  changed   1
    ~ day_1/shift_1/score_day1_shift1.csv  (content changed)
  removed   1
    - misc/vendor_notes.txt
  unchanged 16

$ python -m warehouse.sync apply
  removed        0  -              misc/vendor_notes.txt  -- file no longer in the folder
  replaced     605  candidates     allocation/allocation_export_2026-03-02.csv  -- newer export replaces allocation/allocation_export_2026-02-20.csv (605 rows removed)
  replaced     124  exam_scores    day_1/shift_1/score_day1_shift1.csv  -- file changed on disk; 124 old rows removed
```

### Lookup

```
$ python lookup.py dede.halford94@example.com
Dede Halford
  email : dede.halford94@example.com
  rolls : EX26A00127, EX26A00297

Summary
  EX26A00127  HS120 Technical Writing  day 3 shift 1  ->  49/100 (49%);  assignments 10.0/25, eligible: Yes
  EX26A00297  MA101 Calculus I  day 1 shift 2  ->  60/100 (60%);  no assignment record

Allocation  [candidates]  2 row(s)
    roll_no         EX26A00127
    candidate_name  Dede Halford
    email           dede.halford94@example.com
    course_code     HS120
    course_name     Technical Writing
    exam_day        3
    exam_date       2026-03-14
    shift           1
    centre_city     Quillford
    centre_code     QF04
    reporting_time  08:00
                    <- allocation/allocation_export_2026-02-20.csv row 128
    ...

Exam score  [exam_scores]  2 row(s)
    roll_no         EX26A00297
    candidate_name  Dede Halford
    course_code     MA101
    score           60
    max_score       100
    attempted       38
    submitted_at    2026-03-07 16:27
                    <- day_1/shift_2/score_day1_shift2.csv row 56
    roll_number     EX26A00127
    course          HS120
    marks_obtained  49
    total_marks     100
    status          Submitted
                    <- day_3/shift_1/score_day3_shift1.csv row 23

Absent  [absentees]  - none

Question-wise responses  [question_responses]  - none

Assignment score  [assignment_scores]  1 row(s)
    email                     dede.halford94@example.com
    course_id                 HS120
    assignment_avg_out_of_25  10.0
    assignments_submitted     5
    eligible                  Yes
                              <- assignments/assignment_scores_2026-03-01.csv row 118
```

Note the two score rows come from different vendors with different column names; the raw rows
are shown as the vendor sent them, while the summary uses the unified views. A name fragment
that matches several people lists them instead (`python lookup.py fen`).

### Browser UI

`python browse/app.py [--port 8001] [--db PATH]` serves one page with five tabs:

| Tab | What it shows |
| --- | --- |
| Search | Live search by roll / email / name; click a row for the full person view with provenance |
| Exam days | Scheduled / present / absent / no-result per day and shift, attendance bar, average score |
| Courses | Per-course counts and averages; score histogram in 10% buckets; top scores |
| Tables | Any table or view, filterable across all columns, paginated |
| Updates | `refresh_log` and the `loaded_files` state |

JSON routes: `/api/search?q=`, `/api/person?roll=`, `/api/overview`, `/api/courses`,
`/api/course?id=`, `/api/table?name=&q=&page=` (no `name` lists tables), `/api/log`.

Prefer Datasette? `pip install datasette && datasette db/warehouse.sqlite` works on the same file.

## Commands

| Command | Does |
| --- | --- |
| `python -m warehouse.sync plan` | Extract new/changed zips, compare folder with state: new / changed / removed / unchanged / no rule. No data changes. |
| `python -m warehouse.sync apply` | Process only new, changed and removed files; rebuild views. |
| `python -m warehouse.sync status [-n 20]` | Last N `refresh_log` rows and row counts per table. |
| `python -m warehouse.sync rebuild` | Delete the database and load everything from scratch. |
| `python lookup.py <roll \| email \| name>` | Everything known about one person. |

All accept `--raw DIR` / `--db FILE` (or `WAREHOUSE_RAW` / `WAREHOUSE_DB`).

## Tables

| Table / view | Source rule | Group key (snapshot) | Notes |
| --- | --- | --- | --- |
| `candidates` | `allocation/*.csv\|xlsx` | `allocation` | One row per registration (roll number) |
| `exam_scores` | `day_N/shift_M/score_*.csv\|xlsx` | `scores/day_N/shift_M` | Vendor columns kept as-is |
| `absentees` | `day_N/shift_M/absentees_*.csv\|xlsx` | `absentees/day_N/shift_M` | |
| `question_responses` | `day_N/shift_M/responses_*.xlsx` | `responses/day_N/shift_M` | First worksheet |
| `assignment_scores` | `assignments/*.csv\|xlsx` | `assignments` | Keyed on email + course |
| `sheet_<book>_<tab>` | `sheets/<book>.xlsx` | `sheets/<book>` | One table per worksheet; `_v3`/date suffix dropped from book name |
| `exam_results` (view) | | | Scores and absentees in one shape: `roll_no, course_code, exam_day, shift, attendance, score, max_score, pct, source_file` |
| `candidate_lookup` (view) | | | Allocation + exam result + assignment score, joined on roll number and email + course |
| `loaded_files` | | | State: `source_file, unit, table, group_key, rows, sha1, mtime, loaded_at, status` |
| `refresh_log` | | | History: `at, action, source_file, table, rows, note` |

Every raw table starts with `source_file` and `source_row`, followed by the file's own columns
as TEXT.

## Example queries

```sql
-- Attendance and average score per exam shift
SELECT exam_day, shift, COUNT(*) AS scheduled,
       SUM(attendance = 'absent') AS absent, ROUND(AVG(pct), 1) AS avg_pct
FROM candidate_lookup GROUP BY exam_day, shift;

-- Eligible on assignments but absent on exam day (candidates to contact)
SELECT roll_no, name, email, course_code, assignment_avg
FROM candidate_lookup WHERE eligible = 'Yes' AND attendance = 'absent';

-- Allocated but no score and no absentee record: a vendor file is missing
SELECT exam_day, shift, course_code, COUNT(*) FROM candidate_lookup
WHERE attendance IS NULL GROUP BY 1, 2, 3;

-- Which file did each shift's scores come from, and when was it loaded?
SELECT group_key, source_file, rows, loaded_at FROM loaded_files
WHERE "table" = 'exam_scores' AND status = 'loaded' ORDER BY group_key;

-- What has changed this week?
SELECT at, action, source_file, rows, note FROM refresh_log
WHERE at >= date('now', '-7 days') ORDER BY id DESC;
```

## Design notes

- **Why TEXT everywhere?** Roll numbers with leading zeros, scores like `AB` (absent) or `12.5*`,
  dates in three formats: casting at load time loses information silently. Views cast with
  `CAST(... AS REAL)` where a number is needed, and the raw value is always one join away.
- **Why path-based rules?** They are explicit, reviewable in one file and testable
  (`classify()` is a pure function). Content sniffing guesses; a guess that is wrong is worse
  than a file reported as "no rule".
- **Snapshot by mtime, not by filename.** Vendors name re-exports inconsistently
  (`final`, `final2`, a date, nothing). Modification time is the one thing every copy has. Ties
  are broken by path so the outcome is deterministic.
- **One transaction per file.** A file is either fully in or fully out; a crash mid-run leaves
  the state table consistent with the data, and the next `plan` picks up where it stopped.
- **Views are rebuilt after every refresh** from whichever alias columns actually exist, so a
  vendor adding or renaming a column never breaks the views.
- **The UI is deliberately boring:** `http.server`, one HTML file, vanilla JS, no build step.
  It binds to 127.0.0.1, opens the database with `mode=ro` and `PRAGMA query_only`, uses
  parameters for every value and only accepts table names that exist in `sqlite_master`.
- **Scale.** The original handled tens of thousands of candidates per exam on a laptop;
  SQLite is not the bottleneck at this size. Add indexes on the roll/email columns if
  you go much bigger.

## Tests

```bash
python -m unittest discover -s tests -v
```

`tests/test_sync.py` builds throwaway folders and checks replaced / superseded / removed (with
promotion of the previous copy), in-place changes, unknown files and path classification.

## Layout

```
exam-results-warehouse/
├── warehouse/
│   ├── rules.py      path -> (table, group_key) | (None, reason)
│   ├── loader.py     read CSV/XLSX, load as TEXT with provenance, snapshot rule, state + log
│   ├── views.py      exam_results and candidate_lookup over vendor-shaped tables
│   ├── query.py      person resolution and search (shared by lookup and UI)
│   └── sync.py       CLI: plan / apply / status / rebuild
├── lookup.py         candidate lookup CLI
├── browse/
│   ├── app.py        stdlib HTTP server + JSON API (127.0.0.1:8001)
│   └── app.html      single-page UI, vanilla JS
├── sample/
│   └── generate.py   deterministic fictional sample folder -> sample/raw/
├── tests/
│   └── test_sync.py
├── requirements.txt
├── LICENSE
└── README.md
```

## Data

All sample data is synthetic: names are generated from syllables, every email is
`@example.com`, towns and course titles are invented. No real candidate data is included.

## License

MIT, see [LICENSE](LICENSE).
