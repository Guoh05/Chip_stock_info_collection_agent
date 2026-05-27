# Chip Stock Webapp

Phase 2 webapp — wraps the Phase 1 CLI pipeline as a self-service browser tool
for Versuni business users.

See `docs/planning.md` for the full design (45 decisions across 6 design blocks).

## M0 — local visual demo

Install deps + run locally:

```bash
# from project root (02_work_chip_availability/)
.venv/Scripts/python.exe -m pip install -r webapp/requirements_webapp.txt

# start the dev server
.venv/Scripts/python.exe -m uvicorn webapp.app.main:app --reload --port 8000
```

Then open <http://localhost:8000/>.

### What M0 demonstrates

- `/query` page with Mode A (paste) tab active; Mode B (upload) shown but disabled
- Submit → fake run created in memory (no DB, no real pipeline)
- `/r/<run_id>` waiting page with **3 phase progress bars** that animate over
  ~18 seconds (decision #23)
- After ~18s the page auto-reloads into the **result table** showing 5 fake rows
- Result table visuals **sync with xlsx** (decision #14):
  - T1 columns get dark-blue header; T2 light-orange; 8 procurement-key cols dark-red
  - `in_stock=True` rows tinted light green
- `/history` lists past runs (decision #24, owner_email filtered)
- Empty state + failed state demo URLs:
  - `http://localhost:8000/r/<run_id>?status=empty`
  - `http://localhost:8000/r/<run_id>?status=failed`

### What's stubbed in M0 (real impl comes in M1+)

- Real pipeline subprocess
- Mode B Excel upload + template download
- MPN cleaning + review page
- xlsx parsing + JSON cache
- xlsx download (`/r/<run_id>/download` returns 501)
- SQLite storage (uses in-memory dict)
- Magic Link auth
- Email notification
- 24h cache
- Retention cleanup
- Cloud deployment

## File layout

```
webapp/
├── app/
│   ├── main.py          ← FastAPI entry
│   ├── config.py        ← Paths + M0 flags
│   ├── schemas.py       ← WEBAPP_SCHEMA_v1 + fake data + render_cell()
│   ├── storage.py       ← In-memory RUNS dict (M0); SQLite in M1
│   └── routers/
│       ├── query.py     ← /query (Mode A paste)
│       ├── runs.py      ← /r/<id> + /r/<id>/status + /r/<id>/download
│       └── history.py   ← /history
├── templates/           ← base / query / run / history
├── static/css/style.css
├── docs/planning.md     ← Full design doc — read first
├── requirements_webapp.txt
└── README.md
```

## Path on production (not yet deployed)

Cloud install path will be confirmed with the user before M3 deployment.
Working assumption from planning: `/opt/chip-project/` on Alibaba Cloud
(IP 101.133.151.21, OS = Alibaba Cloud Linux 3).
