# MyUniProject — Academic Advising & Timetabling

Django web app for academic advisors at a Saudi university. Single Django app
(`core/`); SQLite locally, PostgreSQL on Render. Live at
[advisor-system-v9zs.onrender.com](https://advisor-system-v9zs.onrender.com).

## Run locally

```powershell
cd C:\Users\user\myUniproject
.\.venv\Scripts\Activate.ps1
python manage.py runserver 8001
```

Login: `superadmin` / `test123`. Set `DJANGO_DEBUG=true` for the looser
build-throttle and verbose error pages.

## Feature areas

### 1. Advisor portfolio
Per-advisor roster with focus filters (needs attention, risk, zero hours),
student plan matrix, eligible-course recommendations, and CSV export.
Entry: `/`

### 2. Course recommender
Per-student or batch recommendations, weighted by prerequisite chain depth
and credit-shortfall priority. CLI: `python manage.py recommend_student`,
`python manage.py recommend_batch`.

### 3. Timetable Workspace (V2 optimiser)
Section-planning, board-balance, room assignment, and a 5-stage optimiser
(generate → rank → local search → chain search → CP-SAT polish). Two UI
modes: **Optimise Current** (in-place improvements) and **Full Rebuild** (7
strategies from scratch). Entry: `/ops/tw/`

### 4. Exam timetable
Day×period scheduler with bucket-day rule, gender-separated room packer,
auto-split for oversized sections, invigilator calculator + balancer, and a
9-sheet styled XLSX export (Schedule, M/F variants, Courses, M/F students,
QA, Rooms, Invigilators). Optional thin-conflict relaxation for tiny
courses. Entry: `/exam-timetable/`

### 5. Eligibility & shortfall analysis
Pre-registration tools: credit-shortfall classifier, elective placeholder
resolver, and a multi-sheet eligibility XLSX export with charts and
blocked-course breakdowns.

## Key endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Advisor portfolio dashboard |
| GET | `/exam-timetable/` | Exam timetable builder |
| GET | `/ops/tw/` | Timetable workspace |
| GET | `/health/` | Liveness probe |
| GET | `/recommend/<student_id>/?year=1448&semester=0` | Course recs |
| POST | `/parse-and-classify/` | Parse plan + timetable HTML |

## Management commands

```powershell
python manage.py recommend_student 123456 --year 1448 --semester 0
python manage.py recommend_batch --year 1448 --semester 0 --program CS --section M
python manage.py build_exam_timetable --label "demo" --days W1-Sun ...
```

## Tests

```powershell
python -m pytest -q                                  # full suite (162 tests)
python -m pytest tests/test_exam_room_assignment.py  # exam room packer (39 tests)
python -m pytest tests/test_exam_timetable.py        # exam scheduler core
```

## Stack

- Django 5.2.x, Bootstrap 5, vanilla JS (no framework)
- OR-Tools (CP-SAT) for the V2 timetable polisher
- openpyxl for styled multi-sheet XLSX exports (with rich-text colouring)
- PostgreSQL on Render via `dj-database-url`
- Pre-commit: ruff (passes), bandit (passes), mypy (skip with `SKIP=mypy` —
  hook venv lacks `dj-database-url`)

## Deployment

Auto-deploys from `master` on push to GitHub. Render runs migrations as part
of the build. PostgreSQL on Render's Starter plan. Static files via
WhiteNoise; no S3/GCS yet.

## Design system

Glassmorphic + neumorphic cards on a Deep Navy dark theme. CSS variables in
[static/css/global.css](static/css/global.css). All pages standardised on
the `advisor-block` card pattern. RTL-aware (Arabic + English).

## Security

- Do not hardcode portal credentials — use `.env`
- Rotate any leaked credentials immediately
- `import_old/config/settings.py` is a historical reference only
- Build endpoint throttled (3/2min in production, 20/2min in dev)
