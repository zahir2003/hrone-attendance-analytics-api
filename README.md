# Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for employee management, attendance tracking, attendance regularization, and analytics.

## Project Structure

- `app/main.py` — API routes, business logic, MongoDB queries, and index creation.
- `sample_data/` — Sample MongoDB Extended JSON documents.
- `sample_seed.py` — Script for loading sample data.
- `openapi.yaml` — API contract and business rules.
- `DATA_MODEL.md` — Database schema and sample documents.
- `test_attendance_calculations.py` — Automated attendance calculation tests.
- `DECISIONS.md` — Technical decisions and trade-offs.
- `REVIEW.md` — Implementation review.

## Requirements

- Python 3.11 or compatible version
- MongoDB 6.0 or newer
- Dependencies listed in `requirements.txt`


## Local Setup

### 1. Create and activate a virtual environment

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Linux/macOS**

```bash
python -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```powershell
python -m pip install -r requirements.txt
```

### 3. Start MongoDB

Ensure MongoDB is running at `mongodb://localhost:27017`.

The application supports these optional environment variables:

| Variable      | Default value                 | Purpose                   |
| ------------- | ----------------------------- | ------------------------- |
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGO_DB`  | `attendance_db`             | Database name             |

You can set them in PowerShell before starting the API:

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

### 4. Load sample data

```powershell
python sample_seed.py
```

**Warning:** The seed script deletes existing employee and attendance records before loading sample data. Review it before running it against a database containing data you want to preserve.

### 5. Start the API

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

API base URL: `http://127.0.0.1:8000`

Interactive documentation: `http://127.0.0.1:8000/docs`

### 6. Run automated tests

```powershell
python -m pytest -v
```

```bash
python -m pytest -v
```

## Main API Endpoints

- `GET /health` — Health check.
- `POST /employees` and `GET /employees` — Create and list employees.
- `GET /attendance` — List attendance records.
- `POST /attendance/punch-in` and `POST /attendance/punch-out` — Record attendance punches.
- `PATCH /attendance/{emp_code}/{date}` — Regularize attendance.
- `GET /analytics/employees/{emp_code}/monthly` — Monthly employee analytics.
- `GET /analytics/departments/summary` — Department summary.
- `GET /analytics/leaderboard/late` — Late-arrival leaderboard.
- `GET /analytics/departments/{department}/trend` — Department attendance trend.
- `GET /admin/explain/{endpoint}` — Inspect query execution plans.

## Submission Notes

Refer to `PROBLEM_STATEMENT.docx` and `openapi.yaml` for the complete requirements and business rules.

Do not commit environment files, credentials, or secrets. Include the required source code, dependencies, documentation, and tests in the submission.
