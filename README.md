# HOD Attendance Automation System

A Flask + Selenium web app that lets a Head of Department upload an absentee
list (PDF / Excel / CSV / TXT), review the extracted register numbers, and
run a background browser-automation workflow that logs into your college
attendance portal and marks attendance automatically — with live progress,
error handling/retry, and PDF/Excel/CSV reports.

## ⚠️ One thing you must configure before automation will work

I built the entire app end-to-end, but I don't have access to your specific
college portal's HTML, so I can't hard-code its exact field names/buttons.
**One file** handles this:

```
backend/automation/steps_config.py
```

Open your attendance portal in Chrome, right-click each field (login
username/password, department/year/section/subject dropdowns, the student
table, the save button) → **Inspect** → copy its `id`/CSS selector → paste
into the matching entry in `SELECTORS`. Takes about 5–10 minutes. Everything
else (login flow, filter selection, marking absent/present, save, verify,
progress reporting, retries) is already wired up and will work once the
selectors match your portal.

## Features implemented

- Secure HOD login (hashed passwords, Flask-Login sessions)
- Drag-and-drop upload for PDF / Excel / CSV / TXT
- Automatic extraction of register numbers (+ name/department/section/date
  when present in the file), with an editable preview before automation runs
- Background Selenium worker (doesn't block the UI) that: opens Chrome →
  logs in → navigates to attendance → selects department/year/section/
  subject/date → marks absentees → marks present → saves → verifies → closes
- Live progress console with step-by-step status, percentage bar, and logs
- Error handling for login failure / unreachable site / save failure, each
  with a **Retry** button
- Auto-generated PDF, Excel, and CSV reports per session (present/absent
  counts, register numbers, names, duration, status)
- Dashboard with today's automation count, totals, recent sessions, and logs
- Dark mode / light mode, glassmorphism UI, fully responsive

## Project structure

```
hod_attendance_system/
├── backend/
│   ├── app.py              # Flask app factory
│   ├── config.py           # env-based configuration
│   ├── extensions.py       # db, login_manager
│   ├── models.py           # SQLAlchemy models
│   ├── dashboard.py        # dashboard route
│   ├── auth/routes.py      # login/logout
│   ├── upload/
│   │   ├── extractor.py    # PDF/Excel/CSV/TXT -> register numbers
│   │   └── routes.py
│   ├── automation/
│   │   ├── steps_config.py # <-- EDIT THIS for your portal's selectors
│   │   ├── selenium_bot.py # the automation workflow
│   │   └── routes.py       # start / status / retry endpoints
│   └── reports/
│       ├── generator.py    # PDF (reportlab) / Excel (openpyxl) / CSV
│       └── routes.py
├── frontend/
│   ├── templates/          # Jinja2 + Tailwind (CDN) + vanilla JS
│   └── static/css/main.css
├── uploads/                 # uploaded files land here
├── generated_reports/       # generated reports land here
├── database/                 # SQLite file lives here
├── requirements.txt
├── .env.example
└── run.py
```

## Setup

```bash
cd hod_attendance_system
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
# then edit .env:
#   - SECRET_KEY: any long random string
#   - PORTAL_URL / PORTAL_USERNAME / PORTAL_PASSWORD: your college portal login
#   - APP_ADMIN_USERNAME / APP_ADMIN_PASSWORD: the login for THIS dashboard

python run.py
```

Visit `http://localhost:5000`, sign in with the `APP_ADMIN_USERNAME` /
`APP_ADMIN_PASSWORD` you set in `.env` (a matching user is auto-created on
first run). To add more HOD logins later:

```bash
flask --app backend.app:create_app create-admin <username> <password>
```

Chrome must be installed on the machine running the app; `webdriver-manager`
downloads the matching ChromeDriver automatically the first time automation
runs (requires internet access on that machine).

## Notes on scope / assumptions

- **Register number format**: the extractor looks for 10–15 digit numbers by
  default (`Config.REGISTER_NUMBER_REGEX`). Adjust the regex in
  `backend/config.py` if your register numbers use a different format
  (e.g. include letters).
- **Report roster**: reports list the absentees you uploaded (marked
  "Absent") plus present/absent totals from the automation run. If you want
  named "Present" rows in the report too, upload a file containing the full
  class roster with a `Status` column (`Present`/`Absent`) — the extractor
  already supports that format.
- **10,000-student files**: pandas/pdfplumber handle this fine; for very
  large PDF files, extraction may take a few extra seconds — this runs
  synchronously on upload (before automation starts), so the UI shows a
  spinner during that step.
- **Security**: portal credentials are read only from `.env` (never sent to
  the browser); dashboard passwords are hashed with Werkzeug's
  `generate_password_hash`. For real deployment, also put the app behind
  HTTPS and set `FLASK_ENV=production` with a real WSGI server (gunicorn/
  waitress), not Flask's dev server.
