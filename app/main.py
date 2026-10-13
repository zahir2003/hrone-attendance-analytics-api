"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""

import os
from datetime import datetime, timedelta, timezone

from typing import Optional, Literal
from dotenv import load_dotenv
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from decimal import Decimal, ROUND_HALF_UP

# Load optional settings from a local .env file into environment variables.
# This keeps database configuration outside the source code and supports local development.
load_dotenv()

# Create the MongoDB client using an environment override or the local default.
# MongoClient manages database connections; the database handle below selects the app's database.
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
# All employee and attendance queries use this configured database handle.
db = client[os.getenv("MONGO_DB", "attendance_db")]

# The business rules use India Standard Time (UTC+05:30) for shift and attendance dates.
IST = timezone(timedelta(hours=5, minutes=30))


# Purpose: Controls the application's startup and shutdown period. Creating indexes at startup
# prepares MongoDB for the API's expected queries and helps enforce uniqueness before requests
# are handled.
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage FastAPI startup and shutdown. Before the application accepts requests, create the MongoDB indexes required for uniqueness and efficient queries. The yield marks the period during which the application is running."""
    create_indexes()
    yield


# Create the FastAPI application object. Its metadata appears in /docs and the OpenAPI schema.
# The lifespan handler runs create_indexes() during startup, before requests are served.
app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
# Purpose: Converts a Python datetime into the API's timestamp format: milliseconds since 1
# January 1970 UTC. A shared helper keeps timestamp responses consistent.
def to_epoch_ms(value: datetime) -> int:
    """Convert a datetime to epoch milliseconds."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return int(value.timestamp() * 1000)


# Purpose: Creates the indexes used by the application. Indexes help MongoDB find records
# faster, while unique indexes reject duplicate employee codes and duplicate employee/date
# attendance records.
def create_indexes():
    # Each employee code must be unique.
    """Create the MongoDB indexes used by this API. Unique indexes prevent duplicate employee codes and duplicate attendance records per employee/date; compound indexes support common filters and sort orders."""
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="unique_emp_code",
    )

    # Supports department filtering and employee sorting.
    db.employees.create_index(
        [("department", 1), ("emp_code", 1)],
        name="department_emp_code",
    )

    # Supports department summary filtering by employee join date.
    db.employees.create_index(
        [("joined_on", 1)],
        name="employee_joined_on",
    )

    # One attendance record per employee per attendance date.
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="unique_employee_attendance_date",
    )

    # Supports attendance date filtering and required sorting.
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="attendance_date_emp_code",
    )

    # Supports status-based attendance queries.
    db.attendance_logs.create_index(
        [("status", 1), ("date", -1), ("emp_code", 1)],
        name="attendance_status_date_emp_code",
    )


# Purpose: Compares the actual punch-in time with the employee's scheduled shift start. It
# applies the 10-minute grace period and returns whole late minutes for attendance reports.
def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_date: str,
) -> int:
    """Calculate how many whole minutes an employee arrived late compared with the scheduled shift start. The first 10 minutes are a grace period, so lateness is zero through exactly 600 seconds."""
    hours, minutes = map(int, shift_start.split(":"))

    shift_date = datetime.strptime(attendance_date, "%Y-%m-%d").date()

    shift_start_dt = datetime(
        shift_date.year,
        shift_date.month,
        shift_date.day,
        hours,
        minutes,
        tzinfo=IST,
    )

    seconds_late = int((punch_in - shift_start_dt).total_seconds())

    # The grace period is exactly 10 minutes.
    if seconds_late <= 600:
        return 0

    return seconds_late // 60


# Purpose: Calculates the time between punch-in and punch-out in hours. This helper uses
# Python's built-in round function; check which rounding rule is required before using it for
# business calculations.
def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """Calculate elapsed time between punch-in and punch-out in hours, rounded to two decimal places using Python's standard round behavior."""
    return round((punch_out - punch_in).total_seconds() / 3600, 2)


# Purpose: Calculates whole minutes after the scheduled shift end. It returns zero when the
# employee leaves at or before shift end.
def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    """Calculate whole minutes worked after the scheduled shift end. Return zero when punch-out is before or exactly at the shift end."""
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))


# Purpose: Calculates total hours worked and uses Decimal with ROUND_HALF_UP so halfway values
# round in a predictable way. Punch-out and regularization use this helper for the stored work-
# hours value.
def calculate_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """Calculate elapsed work hours and round to two decimal places using Decimal with ROUND_HALF_UP, giving predictable financial-style rounding at half-way values."""
    seconds = (punch_out - punch_in).total_seconds()

    hours = Decimal(str(seconds)) / Decimal("3600")

    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


# Purpose: Defines the request body accepted when a new employee is created. Pydantic checks
# field types and formats before the endpoint reaches the database, so invalid input can be
# rejected early.
class EmployeeIn(BaseModel):
    """Pydantic request model for creating an employee. Field constraints validate employee-code format, name, email, department, shift times, and joining date before the endpoint writes to MongoDB."""

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    joined_on: str

    # Purpose: Checks that joined_on is a real date written exactly as YYYY-MM-DD. This prevents
    # impossible dates and inconsistent date strings from entering the employee collection.
    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value):
        """Validate that joined_on is a real calendar date in YYYY-MM-DD format, not merely a string that resembles a date."""
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d")
            if parsed.strftime("%Y-%m-%d") != value:
                raise ValueError
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        return value

    # Purpose: Rejects names or department values made only of spaces. It does not silently rewrite
    # valid text; it simply checks that meaningful text was provided.
    @field_validator("name", "department")
    @classmethod
    def validate_nonblank(cls, value):
        """Reject names and department values that contain only whitespace, while returning the supplied nonblank value unchanged."""
        if not value.strip():
            raise ValueError("Field must not be blank")
        return value

    # Purpose: Checks the relationship between shift_start and shift_end. A shift with identical
    # start and end times is rejected because it would be ambiguous or zero-length.
    @model_validator(mode="after")
    def validate_shift(self):
        """Ensure shift_start and shift_end are not identical. This prevents an employee from being configured with a zero-length shift."""
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


# Purpose: Describes and validates the JSON body for a punch-in request. The timestamp is
# optional, but when supplied it must be an integer in the allowed epoch-millisecond range;
# status is limited to supported values.
class PunchInIn(BaseModel):
    """Pydantic request model for punch-in. It accepts an employee code, an optional epoch-millisecond timestamp, and an allowed attendance status; omitted timestamps use the current time."""

    emp_code: str
    punched_at: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


# Purpose: Describes the JSON body for punch-out. It accepts an employee code and optional
# bounded epoch-millisecond timestamp, allowing tests or clients to provide a specific punch-out
# time.
class PunchOutIn(BaseModel):
    """Pydantic request model for punch-out. It accepts an employee code and an optional epoch-millisecond timestamp, with bounds to reject implausible timestamp values."""

    emp_code: str
    punched_at: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )


# Purpose: Describes a correction request for an existing attendance record. It captures the
# reason and operator, plus any optional status or timestamp changes, so corrections can be
# validated and audited.
class RegularizeRequest(BaseModel):
    """Pydantic request model for correcting an existing attendance record. It validates the reason and operator name, and optionally accepts a status and corrected punch timestamps."""

    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None

    punch_in: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )

    punch_out: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )

    # Purpose: Ensures the correction reason and operator name contain actual text. This makes the
    # audit trail useful by preventing empty explanations or unidentified operators.
    @field_validator("reason", "regularized_by")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        """Reject a regularization reason or operator name that is empty or whitespace-only so every correction has an explanation and an identified actor."""
        if not value.strip():
            raise ValueError("This field cannot be blank")
        return value


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #


# Purpose: Implements GET /health. It pings MongoDB to check whether the database is reachable
# and returns a service-unavailable response when the database check fails.
@app.get("/health")
def health():
    """Health-check endpoint (GET /health). Ping MongoDB and return status=ok when it responds; return HTTP 503 if the database is unavailable."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="MongoDB is unavailable",
        )


# Purpose: Implements POST /employees. The validated request becomes a MongoDB document; a
# creation timestamp is added, and a duplicate employee code is reported as HTTP 409 Conflict.
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    """Create-employee endpoint (POST /employees). Validate the request through EmployeeIn, add a creation timestamp, insert the employee, and return HTTP 409 if the unique employee code already exists."""
    doc = body.model_dump()
    doc["created_at"] = datetime.now(IST)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )

    doc.pop("_id", None)
    doc["created_at"] = to_epoch_ms(doc["created_at"])
    return doc


# Purpose: Implements GET /employees. It supports an optional department filter and page-based
# results, counts matching records, sorts by employee code, and returns pagination metadata with
# the items.
@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    # Validate pagination parameters.
    """List-employees endpoint (GET /employees). Supports optional exact department filtering and bounded pagination, returns a total count, sorts by employee code, and converts timestamps to epoch milliseconds."""
    if page < 1:
        raise HTTPException(
            status_code=422,
            detail="page must be at least 1",
        )

    if page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=422,
            detail="page_size must be between 1 and 100",
        )

    # Apply the optional exact-match department filter.
    query = {}
    if department is not None:
        query["department"] = department

    # Count only employees matching the filter.
    total = db.employees.count_documents(query)

    # Fetch the requested page in emp_code ascending order.
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = list(cursor)

    # Convert created_at timestamps to epoch milliseconds.
    for employee in items:
        if isinstance(employee.get("created_at"), datetime):
            employee["created_at"] = to_epoch_ms(employee["created_at"])

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
    }


# Purpose: Implements GET /attendance. It builds a MongoDB filter from optional employee, date-
# range, and status parameters, validates pagination and dates, and converts stored timestamps
# to the API's millisecond format.
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    """List-attendance endpoint (GET /attendance). Filter attendance by employee, date range, and status; validate query parameters, paginate in a stable order, and convert stored timestamps—including audit history timestamps—to epoch milliseconds."""
    if page < 1:
        raise HTTPException(
            status_code=422,
            detail="page must be at least 1",
        )

    if page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=422,
            detail="page_size must be between 1 and 100",
        )

    if date_from:
        try:
            if (
                datetime.strptime(date_from, "%Y-%m-%d").strftime("%Y-%m-%d")
                != date_from
            ):
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="date_from must be a valid YYYY-MM-DD date",
            )

    if date_to:
        try:
            if datetime.strptime(date_to, "%Y-%m-%d").strftime("%Y-%m-%d") != date_to:
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="date_to must be a valid YYYY-MM-DD date",
            )

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from cannot be later than date_to",
        )

    allowed_statuses = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
    if status and status not in allowed_statuses:
        raise HTTPException(
            status_code=422,
            detail="Invalid attendance status",
        )

    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to

    if status:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)

    skip = (page - 1) * page_size

    cursor = (
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = []

    for doc in cursor:

        for entry in doc.get("history", []):
            if isinstance(entry.get("at"), datetime):
                entry["at"] = to_epoch_ms(entry["at"])

            for change in entry.get("changes", {}).values():
                for side in ("from", "to"):
                    value = change.get(side)
                    if isinstance(value, datetime):
                        change[side] = to_epoch_ms(value)

        if doc.get("punch_in") is not None:
            doc["punch_in"] = to_epoch_ms(doc["punch_in"])

        if doc.get("punch_out") is not None:
            doc["punch_out"] = to_epoch_ms(doc["punch_out"])

        items.append(doc)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# Purpose: Implements POST /attendance/punch-in. It verifies the employee, normalizes the punch
# time, handles overnight shifts when deciding the attendance date, calculates lateness, and
# inserts the attendance record.
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    # Check that the employee exists.
    """Punch-in endpoint (POST /attendance/punch-in). Confirm the employee exists, normalize the punch time to IST, determine the attendance date for day or overnight shifts, calculate lateness, and insert one record. The unique employee/date index prevents duplicate punch-ins."""
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    # Convert epoch milliseconds to IST and remove fractional seconds.
    if body.punched_at is None:
        ts = datetime.now(timezone.utc).astimezone(IST)
    else:
        ts = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc,
        ).astimezone(IST)

    ts = ts.replace(microsecond=0)

    # Determine the attendance date, including overnight shifts.
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    attendance_date = ts.date()

    if shift_end <= shift_start:
        end_hour, end_minute = map(int, shift_end.split(":"))
        current_time = (ts.hour, ts.minute)

        if current_time < (end_hour, end_minute):
            attendance_date -= timedelta(days=1)

    date_str = attendance_date.isoformat()

    # Calculate late minutes against the correct shift start.
    late_minutes = compute_late_minutes(
        ts,
        shift_start,
        date_str,
    )

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    # The unique database index prevents duplicate records,
    # even when simultaneous requests arrive.
    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this attendance date",
        )

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc["punch_in"]),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": doc["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }


# Purpose: Implements POST /attendance/punch-out. It finds an open punch-in, checks that punch-
# out is after punch-in and within 24 hours, calculates work hours/overtime/half-day status, and
# uses a conditional database update to guard against duplicate concurrent punch-outs.
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    # Check that the employee exists.
    """Punch-out endpoint (POST /attendance/punch-out). Find an eligible open attendance record, validate the duration, calculate work hours/overtime/half-day status, and atomically set punch-out so concurrent requests cannot close the same record twice."""
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    # Convert the supplied time to UTC and truncate to whole seconds.
    if body.punched_at is None:
        punched_at = datetime.now(timezone.utc)
    else:
        punched_at = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc,
        )

    punched_at = punched_at.replace(microsecond=0)

    # Find the most recent punch-in at or before the punch-out time.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punched_at},
        },
        sort=[("punch_in", -1)],
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No punch-in found for this employee",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="This attendance record is already punched out",
        )

    # PyMongo commonly returns naive UTC datetimes.
    punch_in_time = record["punch_in"]
    if punch_in_time.tzinfo is None:
        punch_in_time = punch_in_time.replace(tzinfo=timezone.utc)

    # Punch-out must be after punch-in and no more than 24 hours later.
    duration_seconds = (punched_at - punch_in_time).total_seconds()

    if duration_seconds <= 0 or duration_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in and within 24 hours",
        )

    # Calculate work hours using half-up rounding.
    work_hours = calculate_work_hours(punch_in_time, punched_at)

    # Calculate the employee's scheduled shift end in IST.
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]
    shift_date = datetime.strptime(record["date"], "%Y-%m-%d").date()

    end_hour, end_minute = map(int, shift_end.split(":"))

    # Overnight shifts finish on the following calendar day.
    if shift_end <= shift_start:
        shift_date += timedelta(days=1)

    shift_end_ist = datetime(
        shift_date.year,
        shift_date.month,
        shift_date.day,
        end_hour,
        end_minute,
        tzinfo=IST,
    )

    shift_end_utc = shift_end_ist.astimezone(timezone.utc)

    overtime_seconds = (punched_at - shift_end_utc).total_seconds()
    overtime_whole_minutes = int(overtime_seconds // 60)

    # Overtime is counted only when it reaches 30 whole minutes.
    overtime_minutes = overtime_whole_minutes if overtime_whole_minutes >= 30 else 0

    half_day = work_hours < 4.50

    # Atomic update: only one simultaneous request can close this record.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="This attendance record is already punched out",
        )

    # Return API timestamps as epoch milliseconds.
    history = updated.get("history", [])

    for entry in history:
        if isinstance(entry.get("at"), datetime):
            entry["at"] = to_epoch_ms(entry["at"])

        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                value = change.get(side)
                if isinstance(value, datetime):
                    change[side] = to_epoch_ms(value)

    return {
        "emp_code": updated["emp_code"],
        "date": updated["date"],
        "status": updated["status"],
        "punch_in": to_epoch_ms(punch_in_time),
        "punch_out": to_epoch_ms(punched_at),
        "work_hours": updated["work_hours"],
        "late_minutes": updated["late_minutes"],
        "overtime_minutes": updated["overtime_minutes"],
        "half_day": updated["half_day"],
        "history": history,
    }


# Purpose: Implements PATCH /attendance/{emp_code}/{date}. It validates a correction,
# recalculates derived attendance values, records who changed what and why, and only updates the
# record if the stored version has not changed in the meantime.
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    # Validate the attendance date.
    """Attendance-regularization endpoint (PATCH /attendance/{emp_code}/{date}). Validate the date and employee/record, apply allowed corrections, recalculate derived fields, record who changed what and why, and use a conditional update to detect concurrent edits."""
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
        if parsed_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "date must use YYYY-MM-DD format")

    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "Attendance record not found")

    old_status = record["status"]
    new_status = body.status if body.status is not None else old_status

    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")

    # PyMongo may return naive datetimes. Treat them as UTC.
    if old_punch_in is not None and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)

    if old_punch_out is not None and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    # Use supplied timestamps when present; otherwise retain existing ones.
    new_punch_in = (
        datetime.fromtimestamp(body.punch_in / 1000, tz=timezone.utc)
        if body.punch_in is not None
        else old_punch_in
    )

    new_punch_out = (
        datetime.fromtimestamp(body.punch_out / 1000, tz=timezone.utc)
        if body.punch_out is not None
        else old_punch_out
    )

    # Truncate timestamps to whole seconds.
    if new_punch_in is not None:
        new_punch_in = new_punch_in.replace(microsecond=0)

    if new_punch_out is not None:
        new_punch_out = new_punch_out.replace(microsecond=0)

    # ABSENT and LEAVE records cannot contain punch times.
    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422,
                "ABSENT and LEAVE records cannot have punch times",
            )

        new_punch_in = None
        new_punch_out = None

    else:
        if new_punch_in is None:
            raise HTTPException(
                422,
                "A presence record must have punch_in",
            )

        # Calculate the attendance date using IST and overnight rules.
        punch_in_ist = new_punch_in.astimezone(IST)
        expected_date = punch_in_ist.date()

        if employee["shift_end"] <= employee["shift_start"]:
            end_hour, end_minute = map(int, employee["shift_end"].split(":"))

            if (punch_in_ist.hour, punch_in_ist.minute) < (end_hour, end_minute):
                expected_date -= timedelta(days=1)

        if expected_date.isoformat() != date:
            raise HTTPException(
                422,
                "punch_in does not belong to this attendance date",
            )

        if new_punch_out is not None:
            duration = (new_punch_out - new_punch_in).total_seconds()

            if duration <= 0 or duration > 24 * 60 * 60:
                raise HTTPException(
                    422,
                    "punch_out must be after punch_in and within 24 hours",
                )

    # Recalculate all derived fields.
    if new_status in {"ABSENT", "LEAVE"}:
        late_minutes = 0
        work_hours = None
        overtime_minutes = 0
        half_day = False

    else:
        # Convert UTC to IST before calculating lateness.
        punch_in_ist = new_punch_in.astimezone(IST)

        late_minutes = compute_late_minutes(
            punch_in_ist,
            employee["shift_start"],
            date,
        )

        work_hours = None
        overtime_minutes = 0
        half_day = False

        if new_punch_out is not None:
            work_hours = calculate_work_hours(
                new_punch_in,
                new_punch_out,
            )
            half_day = work_hours < 4.50

            end_hour, end_minute = map(int, employee["shift_end"].split(":"))

            shift_end_date = parsed_date
            if employee["shift_end"] <= employee["shift_start"]:
                shift_end_date += timedelta(days=1)

            shift_end_ist = datetime(
                shift_end_date.year,
                shift_end_date.month,
                shift_end_date.day,
                end_hour,
                end_minute,
                tzinfo=IST,
            )

            shift_end_utc = shift_end_ist.astimezone(timezone.utc)
            overtime_seconds = (new_punch_out - shift_end_utc).total_seconds()

            if overtime_seconds >= 30 * 60:
                overtime_minutes = int(overtime_seconds // 60)

    # Build history using normalized timestamps.
    changes = {}

    # Purpose: A small helper used by regularization to record only fields whose values actually
    # changed. The before-and-after values are normalized for the API audit history.
    def add_change(field, old_value, new_value):
        """Add a field to the regularization audit log only when its old and new values differ. Convert datetime values to epoch milliseconds so the history is JSON/API friendly."""
        if old_value != new_value:
            if isinstance(old_value, datetime):
                old_value = to_epoch_ms(old_value)
            if isinstance(new_value, datetime):
                new_value = to_epoch_ms(new_value)

            changes[field] = {
                "from": old_value,
                "to": new_value,
            }

    add_change("status", old_status, new_status)
    add_change("punch_in", old_punch_in, new_punch_in)
    add_change("punch_out", old_punch_out, new_punch_out)
    add_change(
        "late_minutes",
        record.get("late_minutes", 0),
        late_minutes,
    )
    add_change("work_hours", record.get("work_hours"), work_hours)
    add_change(
        "overtime_minutes",
        record.get("overtime_minutes", 0),
        overtime_minutes,
    )
    add_change(
        "half_day",
        record.get("half_day", False),
        half_day,
    )

    if not changes:
        raise HTTPException(
            422,
            "The correction does not change anything",
        )

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by.strip(),
        "reason": body.reason.strip(),
        "changes": changes,
    }

    update_fields = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "late_minutes": late_minutes,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    # Only update if the original record has not changed meanwhile.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "status": old_status,
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
            "history": record.get("history", []),
        },
        {
            "$set": update_fields,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            409,
            "Attendance changed during correction; please retry",
        )

    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]},
        {"_id": 0},
    )

    # Convert stored timestamps to epoch milliseconds for the API.
    for field in ("punch_in", "punch_out"):
        if updated.get(field) is not None:
            updated[field] = to_epoch_ms(updated[field])

    for entry in updated.get("history", []):
        if entry.get("at") is not None:
            entry["at"] = to_epoch_ms(entry["at"])

        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                value = change.get(side)
                if isinstance(value, datetime):
                    change[side] = to_epoch_ms(value)

    return updated


# Purpose: Implements GET /analytics/employees/{emp_code}/monthly. It calculates eligible
# weekdays from the month and joining date, then uses a MongoDB aggregation to summarize
# presence, leave, lateness, overtime, and attendance percentage.
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    # Check the requested month format.
    """Monthly employee analytics endpoint (GET /analytics/employees/{emp_code}/monthly). Calculate eligible weekdays from the later of the month start or joining date, then use a MongoDB aggregation to summarize attendance, leave, lateness, and overtime."""
    try:
        month_start = datetime.strptime(month, "%Y-%m")
        if month_start.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    # Check whether the employee exists.
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    # Find the first day of this month and the first day of next month.
    start_date = month_start.strftime("%Y-%m-01")

    if month_start.month == 12:
        next_month = month_start.replace(
            year=month_start.year + 1,
            month=1,
        )
    else:
        next_month = month_start.replace(
            month=month_start.month + 1,
        )

    end_date = next_month.strftime("%Y-%m-01")

    # Calculate the number of Monday-Friday working days.
    # Count working days from the employee's joining date
    # or the first day of the month, whichever is later.
    month_end = next_month - timedelta(days=1)

    joined_on = datetime.strptime(employee["joined_on"], "%Y-%m-%d").date()

    effective_start = max(month_start.date(), joined_on)
    effective_end = month_end.date()

    working_days = 0
    current_day = effective_start

    while current_day <= effective_end:
        if current_day.weekday() < 5:
            working_days += 1
        current_day += timedelta(days=1)
    # The aggregation calculates totals in MongoDB.
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": effective_start.isoformat(),
                    "$lte": effective_end.isoformat(),
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$lt": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            7,
                                        ]
                                    },
                                    {
                                        "$gt": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            1,
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$ifNull": ["$half_day", False]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    result = list(db.attendance_logs.aggregate(pipeline))

    if result:
        totals = result[0]
    else:
        totals = {
            "present_days": 0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
        }

    present_days = totals["present_days"]

    attendance_pct = (
        float(
            (
                Decimal(str(present_days)) / Decimal(working_days) * Decimal("100")
            ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        )
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": totals["leave_days"],
        "late_count": totals["late_count"],
        "total_late_minutes": totals["total_late_minutes"],
        "total_overtime_minutes": totals["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }


# Purpose: Implements GET /analytics/departments/summary. It starts from eligible employees,
# joins their attendance logs for the selected month, and summarizes headcount and
# attendance/work metrics by department.
@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    # Validate the requested month.
    """Department-summary endpoint (GET /analytics/departments/summary). Start from eligible employees so staff without attendance logs still count toward headcount, join monthly attendance records, and aggregate department-level attendance and work metrics."""
    try:
        month_start = datetime.strptime(month, "%Y-%m")
        if month_start.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    start_date = month_start.strftime("%Y-%m-01")

    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)

    end_date = next_month.strftime("%Y-%m-01")

    # Begin with employees so employees without attendance logs
    # are included in the headcount.
    employee_match = {"joined_on": {"$lt": end_date}}

    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lt": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employee_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$lt": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$logs.date"
                                                    }
                                                }
                                            },
                                            7,
                                        ]
                                    },
                                    {
                                        "$gt": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$logs.date"
                                                    }
                                                }
                                            },
                                            1,
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {
                                        "$eq": [
                                            {"$ifNull": ["$logs.half_day", False]},
                                            True,
                                        ]
                                    },
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hours_total": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$isNumber": "$logs.work_hours"},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$isNumber": "$logs.work_hours"},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$logs.late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$logs.late_minutes", 0]},
                            "$logs.late_minutes",
                            0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employee_codes"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {
                                                        "$divide": [
                                                            "$work_hours_total",
                                                            "$work_hours_count",
                                                        ]
                                                    },
                                                    100,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                100,
                            ]
                        },
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))

    return {"month": month, "items": items}


# Purpose: Implements GET /analytics/leaderboard/late. It totals late minutes for each employee
# in a month, optionally filters by department, and ranks employees. Filtering before ranking
# ensures ranks are based on the requested department.
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = 10,
    department: Optional[str] = None,
):
    # Validate month and limit.
    """Late-arrival leaderboard endpoint (GET /analytics/leaderboard/late). Aggregate late minutes by employee for a month, optionally filter by department before ranking, and preserve ties at the requested rank cutoff."""
    try:
        month_start = datetime.strptime(month, "%Y-%m")
        if month_start.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    if limit < 1 or limit > 50:
        raise HTTPException(
            status_code=422,
            detail="limit must be between 1 and 50",
        )

    start_date = month_start.strftime("%Y-%m-01")

    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)

    end_date = next_month.strftime("%Y-%m-01")

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lt": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    # Apply the department filter before calculating ranks.
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend(
        [
            {
                "$set": {
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            # Limit after ranking, so ties at the cutoff remain.
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "emp_code": 1,
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": 1,
                    "name": 1,
                    "department": 1,
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )

    items = list(db.attendance_logs.aggregate(pipeline))

    return {"month": month, "items": items}


# Purpose: Implements GET /analytics/departments/{department}/trend. It validates a date window
# of at most 92 days and calculates daily headcount, attendance, lateness, attendance rate, and
# a seven-day moving average.
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    # Validate the date parameters.
    """Department-trend endpoint (GET /analytics/departments/{department}/trend). Validate a date range of at most 92 days and aggregate daily headcount, presence, lateness, attendance rate, and a seven-day moving average."""
    try:
        start_day = datetime.strptime(from_date, "%Y-%m-%d").date()
        end_day = datetime.strptime(to_date, "%Y-%m-%d").date()

        if start_day.isoformat() != from_date or end_day.isoformat() != to_date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="from and to must use YYYY-MM-DD format",
        )

    if end_day < start_day:
        raise HTTPException(
            status_code=422,
            detail="to must be on or after from",
        )

    day_count = (end_day - start_day).days + 1
    if day_count > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    if not db.employees.find_one(
        {"department": department},
        {"_id": 1},
    ):
        raise HTTPException(
            status_code=404,
            detail="Unknown department",
        )

    start_datetime = datetime.combine(start_day, datetime.min.time())

    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "_id": 0,
                "dates": {
                    "$map": {
                        "input": {"$range": [0, day_count]},
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": start_datetime,
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$dates"},
        {
            "$set": {
                "date_text": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$dates",
                    }
                },
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$isoDayOfWeek": "$dates"}, 1]},
                        {"$lte": [{"$isoDayOfWeek": "$dates"}, 5]},
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day_text": "$date_text"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$day_text"]}}},
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee",
                        }
                    },
                    {"$unwind": "$employee"},
                    {"$match": {"employee.department": department}},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {
                                                    "$eq": [
                                                        {
                                                            "$ifNull": [
                                                                "$half_day",
                                                                False,
                                                            ]
                                                        },
                                                        True,
                                                    ]
                                                },
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": ["$late_minutes", 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "daily",
            }
        },
        {
            "$set": {
                "present_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$daily.present_count", 0]},
                        0,
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$daily.late_count", 0]},
                        0,
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_text": "$date_text"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {"$lte": ["$joined_on", "$$day_text"]},
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "headcount_result",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$headcount_result.count", 0]},
                        0,
                    ]
                }
            }
        },
        {
            "$set": {
                "attendance_rate_raw": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"dates": 1},
                "output": {
                    "moving_avg_raw": {
                        "$avg": "$attendance_rate_raw",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$ne": ["$attendance_rate_raw", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    "$attendance_rate_raw",
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_raw", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    "$moving_avg_raw",
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date_text",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))

    return {"department": department, "items": items}


# Purpose: Implements GET /admin/explain/{endpoint}. It runs MongoDB explain with execution
# statistics for a limited set of supported queries, helping reviewers inspect index use and
# query execution rather than changing business data.
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20,
):
    """Query-plan inspection endpoint (GET /admin/explain/{endpoint}). For a limited set of supported analytics/list queries, ask MongoDB for execution statistics so reviewers can inspect query plans and index use. This is a diagnostic endpoint, not a normal business operation."""
    allowed_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed_endpoints:
        raise HTTPException(
            status_code=422,
            detail="Unsupported endpoint name",
        )

    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=422,
            detail="Invalid pagination parameters",
        )

    if not 1 <= limit <= 50:
        raise HTTPException(
            status_code=422,
            detail="limit must be between 1 and 50",
        )

    if endpoint == "attendance_list":
        query = {}

        if emp_code:
            query["emp_code"] = emp_code

        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to

        if status:
            query["status"] = status

        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required",
            )

        try:
            month_start = datetime.strptime(month, "%Y-%m")
            if month_start.strftime("%Y-%m") != month:
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="month must use YYYY-MM format",
            )

        employee = db.employees.find_one(
            {"emp_code": emp_code},
            {"_id": 0, "emp_code": 1, "joined_on": 1},
        )

        if employee is None:
            raise HTTPException(
                status_code=404,
                detail="Employee not found",
            )

        if month_start.month == 12:
            next_month = month_start.replace(
                year=month_start.year + 1,
                month=1,
            )
        else:
            next_month = month_start.replace(
                month=month_start.month + 1,
            )

        month_end = next_month - timedelta(days=1)
        joined_on = datetime.strptime(employee["joined_on"], "%Y-%m-%d").date()

        effective_start = max(month_start.date(), joined_on)
        effective_end = month_end.date()

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": effective_start.isoformat(),
                        "$lte": effective_end.isoformat(),
                    },
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$lt": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$date"
                                                        }
                                                    }
                                                },
                                                7,
                                            ]
                                        },
                                        {
                                            "$gt": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$date"
                                                        }
                                                    }
                                                },
                                                1,
                                            ]
                                        },
                                    ]
                                },
                                {
                                    "$cond": [
                                        "$half_day",
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", "LEAVE"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$late_minutes", 0]},
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                }
            },
        ]

        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required",
            )

        try:
            month_start = datetime.strptime(month, "%Y-%m")
            if month_start.strftime("%Y-%m") != month:
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="month must use YYYY-MM format",
            )

        start_date = month_start.strftime("%Y-%m-01")

        if month_start.month == 12:
            next_month = month_start.replace(
                year=month_start.year + 1,
                month=1,
            )
        else:
            next_month = month_start.replace(
                month=month_start.month + 1,
            )

        end_date = next_month.strftime("%Y-%m-01")

        employee_match = {"joined_on": {"$lt": end_date}}

        if department is not None:
            employee_match["department"] = department

        pipeline = [
            {"$match": employee_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"employee_code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {
                                            "$eq": [
                                                "$emp_code",
                                                "$$employee_code",
                                            ]
                                        },
                                        {"$gte": ["$date", start_date]},
                                        {"$lt": ["$date", end_date]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$unwind": {
                    "path": "$logs",
                    "preserveNullAndEmptyArrays": True,
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "employee_codes": {"$addToSet": "$emp_code"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$in": [
                                        "$logs.status",
                                        ["PRESENT", "WFH", "ON_DUTY"],
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$logs.half_day", True]},
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "work_hours_total": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$logs.status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {"$isNumber": "$logs.work_hours"},
                                    ]
                                },
                                "$logs.work_hours",
                                0,
                            ]
                        }
                    },
                    "work_hours_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$logs.status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {"$isNumber": "$logs.work_hours"},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$logs.late_minutes", 0]},
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$logs.late_minutes", 0]},
                                "$logs.late_minutes",
                                0,
                            ]
                        }
                    },
                    "leave_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$logs.status", "LEAVE"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$logs.status", "ON_DUTY"]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "employee_codes": 1,
                    "present_days": 1,
                    "avg_work_hours": {
                        "$cond": [
                            {"$gt": ["$work_hours_count", 0]},
                            {
                                "$round": [
                                    {
                                        "$divide": [
                                            "$work_hours_total",
                                            "$work_hours_count",
                                        ]
                                    },
                                    2,
                                ]
                            },
                            None,
                        ]
                    },
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                }
            },
            {"$sort": {"department": 1}},
        ]

        explain = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain,
        }

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required",
            )

        try:
            month_start = datetime.strptime(month, "%Y-%m")
            if month_start.strftime("%Y-%m") != month:
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="month must use YYYY-MM format",
            )

        if limit < 1 or limit > 50:
            raise HTTPException(
                status_code=422,
                detail="limit must be between 1 and 50",
            )

        start_date = month_start.strftime("%Y-%m-01")

        if month_start.month == 12:
            next_month = month_start.replace(
                year=month_start.year + 1,
                month=1,
            )
        else:
            next_month = month_start.replace(
                month=month_start.month + 1,
            )

        end_date = next_month.strftime("%Y-%m-01")

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_date, "$lt": end_date},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]

        if department is not None:
            pipeline.append({"$match": {"employee.department": department}})

        pipeline.extend(
            [
                {
                    "$set": {
                        "emp_code": "$_id",
                        "name": "$employee.name",
                        "department": "$employee.department",
                    }
                },
                {
                    "$setWindowFields": {
                        "sortBy": {"total_late_minutes": -1},
                        "output": {"rank": {"$rank": {}}},
                    }
                },
                {"$match": {"rank": {"$lte": limit}}},
                {
                    "$sort": {
                        "total_late_minutes": -1,
                        "emp_code": 1,
                    }
                },
                {
                    "$project": {
                        "_id": 0,
                        "rank": 1,
                        "emp_code": 1,
                        "name": 1,
                        "department": 1,
                        "total_late_minutes": 1,
                        "late_count": 1,
                    }
                },
            ]
        )

        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required",
            )

        try:
            start_day = datetime.strptime(from_date, "%Y-%m-%d").date()
            end_day = datetime.strptime(to_date, "%Y-%m-%d").date()

            if start_day.isoformat() != from_date or end_day.isoformat() != to_date:
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="from and to must use YYYY-MM-DD format",
            )

        if start_day > end_day:
            raise HTTPException(
                status_code=422,
                detail="from must be on or before to",
            )

        day_count = (end_day - start_day).days + 1

        if day_count > 92:
            raise HTTPException(
                status_code=422,
                detail="Date range cannot exceed 92 days",
            )

        department_exists = db.employees.find_one(
            {"department": department},
            {"_id": 1},
        )

        if department_exists is None:
            raise HTTPException(
                status_code=404,
                detail="Department not found",
            )

        start_datetime = datetime.combine(
            start_day,
            datetime.min.time(),
        )

        pipeline = [
            {"$match": {"department": department}},
            {"$limit": 1},
            {
                "$project": {
                    "_id": 0,
                    "dates": {
                        "$map": {
                            "input": {"$range": [0, day_count]},
                            "as": "offset",
                            "in": {
                                "$dateAdd": {
                                    "startDate": start_datetime,
                                    "unit": "day",
                                    "amount": "$$offset",
                                }
                            },
                        }
                    },
                }
            },
            {"$unwind": "$dates"},
            {
                "$set": {
                    "date_text": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$dates",
                        }
                    },
                    "is_working_day": {
                        "$and": [
                            {
                                "$gte": [
                                    {"$isoDayOfWeek": "$dates"},
                                    1,
                                ]
                            },
                            {
                                "$lte": [
                                    {"$isoDayOfWeek": "$dates"},
                                    5,
                                ]
                            },
                        ]
                    },
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"day_text": "$date_text"},
                    "pipeline": [
                        {"$match": {"$expr": {"$eq": ["$date", "$$day_text"]}}},
                        {
                            "$lookup": {
                                "from": "employees",
                                "let": {"employee_code": "$emp_code"},
                                "pipeline": [
                                    {
                                        "$match": {
                                            "department": department,
                                            "$expr": {
                                                "$eq": [
                                                    "$emp_code",
                                                    "$$employee_code",
                                                ]
                                            },
                                        }
                                    },
                                    {"$limit": 1},
                                ],
                                "as": "matching_employee",
                            }
                        },
                        {"$match": {"matching_employee.0": {"$exists": True}}},
                        {
                            "$group": {
                                "_id": None,
                                "present_count": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$in": [
                                                    "$status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                            {
                                                "$cond": [
                                                    {"$eq": ["$half_day", True]},
                                                    0.5,
                                                    1,
                                                ]
                                            },
                                            0,
                                        ]
                                    }
                                },
                                "late_count": {
                                    "$sum": {
                                        "$cond": [
                                            {"$gt": ["$late_minutes", 0]},
                                            1,
                                            0,
                                        ]
                                    }
                                },
                            }
                        },
                    ],
                    "as": "daily",
                }
            },
            {
                "$set": {
                    "present_count": {
                        "$ifNull": [
                            {
                                "$arrayElemAt": [
                                    "$daily.present_count",
                                    0,
                                ]
                            },
                            0,
                        ]
                    },
                    "late_count": {
                        "$ifNull": [
                            {
                                "$arrayElemAt": [
                                    "$daily.late_count",
                                    0,
                                ]
                            },
                            0,
                        ]
                    },
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"day_text": "$date_text"},
                    "pipeline": [
                        {
                            "$match": {
                                "department": department,
                                "$expr": {
                                    "$lte": [
                                        "$joined_on",
                                        "$$day_text",
                                    ]
                                },
                            }
                        },
                        {"$count": "count"},
                    ],
                    "as": "headcount_result",
                }
            },
            {
                "$set": {
                    "headcount": {
                        "$ifNull": [
                            {
                                "$arrayElemAt": [
                                    "$headcount_result.count",
                                    0,
                                ]
                            },
                            0,
                        ]
                    }
                }
            },
            {
                "$set": {
                    "attendance_rate_raw": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_working_day",
                                    {"$gt": ["$headcount", 0]},
                                ]
                            },
                            {
                                "$divide": [
                                    "$present_count",
                                    "$headcount",
                                ]
                            },
                            None,
                        ]
                    }
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"dates": 1},
                    "output": {
                        "moving_avg_raw": {
                            "$avg": "$attendance_rate_raw",
                            "window": {"documents": [-6, 0]},
                        }
                    },
                }
            },
            {
                "$set": {
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$ne": [
                                    "$attendance_rate_raw",
                                    None,
                                ]
                            },
                            {
                                "$divide": [
                                    {
                                        "$floor": {
                                            "$add": [
                                                {
                                                    "$multiply": [
                                                        "$attendance_rate_raw",
                                                        10000,
                                                    ]
                                                },
                                                0.5,
                                            ]
                                        }
                                    },
                                    10000,
                                ]
                            },
                            None,
                        ]
                    },
                    "moving_avg_7d": {
                        "$cond": [
                            {
                                "$ne": [
                                    "$moving_avg_raw",
                                    None,
                                ]
                            },
                            {
                                "$divide": [
                                    {
                                        "$floor": {
                                            "$add": [
                                                {
                                                    "$multiply": [
                                                        "$moving_avg_raw",
                                                        10000,
                                                    ]
                                                },
                                                0.5,
                                            ]
                                        }
                                    },
                                    10000,
                                ]
                            },
                            None,
                        ]
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": "$date_text",
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": 1,
                }
            },
            {"$sort": {"date": 1}},
        ]

        explain = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain,
        }

    raise HTTPException(
        status_code=422,
        detail="Explain support for this endpoint is not implemented yet",
    )
