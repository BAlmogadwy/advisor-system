# MyUniProject (Django + Legacy Advisor Migration)

## Run locally
```powershell
cd C:\Users\user\myUniproject
.\.venv\Scripts\Activate.ps1
python manage.py runserver
```

## Key endpoints
- `GET /health/`
- `GET /recommend/<student_id>/?year=1448&semester=0`
- `POST /classify/` (JSON: `study_plan`, `timetable`)
- `POST /parse-and-classify/` (JSON: `study_plan_html`, `timetable_html`)

## Management commands
```powershell
python manage.py recommend_student 123456 --year 1448 --semester 0
python manage.py recommend_batch --year 1448 --semester 0 --program CS --section M
```

## Advisor workflow (Admin + Portfolio)
1. Open dashboard `/` and go to **Advisor Admin**.
2. Create advisor (supports multiple departments, e.g. `CS,AI`).
3. Click **Enable students.advisor_id** once.
4. Assign students:
   - Bulk CSV (`student_id,advisor_id`) or
   - Quick single assignment.
5. Go to **Advisor Portfolio**:
   - Select advisor and load students.
   - Use focus filters/chips (needs attention, risk, missing, zero hours).
   - Click student ID to open Student Plan Matrix.
   - Click **View (N)** in high-priority column for detailed missing courses.
   - Export filtered roster CSV.

## Security
- Do not hardcode portal credentials.
- Use `.env` values and rotate old leaked credentials.
- Keep `import_old/config/settings.py` as historical reference only.
