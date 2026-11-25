# AssetTrack: Circular Campus

COSC 336 group project (Group 1-B2): the **Intelligent, AI-Powered Circular Campus Resource Exchange and Asset Life Cycle Management System**.

University departments list surplus equipment, other departments request it, an independent approver decides, and the item is handed over and tracked for its whole life: repairs, reuse and finally donation, recycling or disposal. AI helps at five points, and a person decides every time.

## What is in this repository

| Folder or file | Contents |
| --- | --- |
| [`circular-campus/`](circular-campus/) | Phase 6: the working Django web application, tests and guides |
| [`circular-campus/docs/`](circular-campus/docs/) | Submission report, manual, logic guide and website test guide |
| `Phase 2*.docx`, `Phase2-*.pdf` | Phase 2 documents |

## Run the website

You need Python 3.12 or newer. In a terminal, inside the `circular-campus` folder:

```bash
python -m pip install -r requirements.txt
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

Open http://localhost:8000. Demo accounts (fictional data), all with the password `campus123`: `sara` (requester), `omar` (custodian), `layla` and `yousef` (approvers), `hassan` (maintenance), `admin` (administrator).

The AI features use the Claude API when the `ANTHROPIC_API_KEY` environment variable is set, and built-in keyword rules otherwise. Never put the key in a file.

See [`circular-campus/README.md`](circular-campus/README.md) for the full feature list.

## Team

Mohammed Alyammahi (100060927) · Mohammed Ali (100060689)
