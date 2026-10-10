# Employee Attendance & Analytics API

FastAPI + PyMongo implementation of the supplied v2.0 contract. All application code is in `app/main.py`.

## Run

Requirements: Python 3.11+ and a running MongoDB 6.0+ server. MongoDB 7.0 was used for verification.

From this folder on Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:MONGO_URI="mongodb://localhost:27017"
$env:MONGO_DB="attendance_db"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

With an activated environment, the standard command is:

```text
uvicorn app.main:app --port 8000
```

Open http://127.0.0.1:8000/docs for interactive API documentation and http://127.0.0.1:8000/health for readiness. A 404 at `/` is expected: there is no homepage.

Real environment variables override optional local `.env` values. Indexes are created automatically at startup. No database is seeded, cleared or reset by the application. Existing duplicate employee codes or attendance keys must be resolved before unique indexes can be built.

## Implemented

Employee creation/listing, punch-in/out, attendance listing, audited corrections, employee monthly analytics, department summaries, late rankings, daily trends and executionStats explain are implemented. Analytics execute in MongoDB. Trends generate calendar rows inside MongoDB using `$range`/`$map`, including periods with no logs. Unique indexes and optimistic conditional updates protect concurrent writes.

There are no deliberately omitted endpoints. No authentication or frontend is included because neither is part of this assignment.

## Verification and limits

During development, 87 functional assertions passed against a temporary MongoDB 7.0.16 instance with Python 3.12. Checks covered validation, grace/rounding boundaries, overnight shifts, corrections, legacy fields, ties, empty trends, pagination and concurrency. Additional checks used 100,000 generated attendance records: all five explain routes contained IXSCAN and no COLLSCAN. Startup took approximately 0.54 seconds in that environment. Concurrent corrections retained an entry for every successful edit.

These are development checks, not the employer's hidden tests or a guarantee of identical performance on other machines. The temporary test harness and generated database are not included in this package. Windows/Python 3.14 execution must be verified on the candidate's machine.

The implementation and documentation were prepared with AI assistance and should be reviewed and understood before submission.
# HROne-Assignment 
