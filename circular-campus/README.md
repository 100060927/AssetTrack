# Circular Campus

Phase 6 draft implementation of the AI-powered Circular Campus Resource
Exchange and Asset Life Cycle Management System (COSC 336, Group 1-B2).

Stack (Phase 2 decision): HTML, CSS and Django templates · Python with Django ·
PostgreSQL (SQLite is used automatically when no PostgreSQL settings are given).

## Run it

```bash
pip install -r requirements.txt
```

```bash
python manage.py migrate
```

```bash
python manage.py seed_demo
```

```bash
python manage.py runserver
```

Open http://localhost:8000 and sign in. All demo data is fictional; every
account uses the password `campus123`.

| User | Role | Department |
| --- | --- | --- |
| sara, hamad, fatima, ali | Requester | CS, ENG, BUS, LIB |
| omar, noura, khalid, mariam | Asset custodian | ENG, CS, BUS, LIB |
| layla, yousef | Approver (asset officer) | FAC, BUS |
| hassan | Maintenance staff | FAC |
| admin | System administrator | none |

Run the checks:

```bash
python manage.py test
```

### PostgreSQL

Set `POSTGRES_DB` (and, as needed, `POSTGRES_USER`, `POSTGRES_PASSWORD`,
`POSTGRES_HOST`, `POSTGRES_PORT`) before `migrate`.

### AI provider

Without credentials the AI features run on the built-in rule-based fallback, so
everything works offline. To use the external model, set `ANTHROPIC_API_KEY`
in the environment (never in the code or the repository). Optional:
`AI_PROVIDER=classic` forces the fallback and `AI_MODEL` changes the model.

## What is implemented

| ID | Feature | Where |
| --- | --- | --- |
| C1 | Login and 5 roles, checked on the server | `accounts/permissions.py` |
| C2 | Departments and asset registration | `catalog/` |
| C3 | Publish, search and filter | `catalog/services.py` |
| C4 | Submit and track requests | `exchange/` |
| C5 | Approval, no self-approval, receipt changes owner and department, no double allocation | `exchange/services.py` |
| C6 | Allowed state moves only, history per asset | `catalog/models.py` |
| C7 | Sustainability dashboard with formula and source | `reports/services.py` |
| C8 | In-app notifications | `notifications/` |
| C9 | Requests and transfers report with CSV export | `reports/views.py` |
| UC-21, UC-09 | Report a defect (unsafe items are blocked); maintenance records inspection, repair, cost and result | `lifecycle/services.py` |
| UC-15 | Edit an item's details; old and new values go into its history | `catalog/services.py` |
| UC-18 | Holds expire and release the item; warnings, approval reminders, officer extensions (`python manage.py process_expiry`) | `exchange/services.py` |
| UC-19 | Receipt disputes: wrong, damaged or missing; an independent officer resolves (received, returned, lost) | `exchange/services.py` |
| UC-20 | Donate, recycle or dispose: propose, independent approval, completion with evidence | `lifecycle/services.py` |
| UC-26 | Audit log: every change in one searchable page, CSV export | `audit/` |
| UC-30 | Backup and restore of the whole database, with a safety copy and count check | `audit/backup.py` |
| AI 1 | Classification and tagging at registration; custodian accepts or edits | `ai/classification.py` |
| AI 2 | Matching and ranking: top 5, score, one-line reason, accept or reject | `ai/matching.py` |
| AI 3 | Sustainable-action advice with impact from our own formulas; custodian decides | `ai/advice.py` |
| AI 4 | Assistant: answers only from the app's data, never acts | `ai/assistant.py` |
| AI 5 | Generated report: database numbers, AI narrative, admin edits and publishes | `reports/services.py` |

Documents in `docs/`: `ARCHITECTURE.md` (class and architecture plan),
`Circular-Campus-Manual.pdf` (step-by-step procedures),
`Circular-Campus-Logic-Guide.pdf` (how the code works) and
`Circular-Campus-System-Guide.pdf` (overview and practice questions).

Backups are saved in the `backups/` folder next to `manage.py`. To release
expired holds automatically, schedule `python manage.py process_expiry`
(for example hourly in Windows Task Scheduler). For a demo,
`python manage.py process_expiry --pretend-days 8` acts as if eight days
have passed.
