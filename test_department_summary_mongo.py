import os
import pytest
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from fastapi.testclient import TestClient
from app import main

def test_department_summary_with_real_mongodb():
    uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    client = MongoClient(uri, serverSelectionTimeoutMS=3000)

    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        client.close()
        pytest.skip(f"MongoDB is unavailable: {exc}")

    # Use a separate database so application data stays untouched.
    db = client["attendance_summary_test_temp"]

    try:
        db.drop_collection("employees")
        db.drop_collection("attendance_logs")

        db.employees.insert_many(
            [
                {
                    "emp_code": "TEST001",
                    "name": "Employee With Logs",
                    "email": "test1@example.com",
                    "department": "Engineering",
                    "shift_start": "09:30",
                    "shift_end": "18:30",
                    "joined_on": "2026-01-01",
                },
                {
                    "emp_code": "TEST002",
                    "name": "Employee Without Logs",
                    "email": "test2@example.com",
                    "department": "Engineering",
                    "shift_start": "09:30",
                    "shift_end": "18:30",
                    "joined_on": "2026-01-01",
                },
            ]
        )

        db.attendance_logs.insert_many(
            [
                {
                    "emp_code": "TEST001",
                    "date": "2026-10-12",
                    "status": "PRESENT",
                    "half_day": False,
                    "work_hours": 8.0,
                    "late_minutes": 10,
                },
                {
                    "emp_code": "TEST001",
                    "date": "2026-10-13",
                    "status": "PRESENT",
                    "half_day": True,
                    "work_hours": 4.0,
                    "late_minutes": 0,
                },
                {
                    "emp_code": "TEST001",
                    "date": "2026-10-17",
                    "status": "PRESENT",
                    "half_day": False,
                    "work_hours": 8.0,
                    "late_minutes": 15,
                },
            ]
        )

        original_db = main.db
        main.db = db

        try:
            with TestClient(main.app) as api_client:
                response = api_client.get(
                    "/analytics/departments/summary",
                    params={
                        "month": "2026-10",
                        "department": "Engineering",
                    },
                )
        finally:
            main.db = original_db

        assert response.status_code == 200, response.text

        items = response.json()["items"]
        assert len(items) == 1

        summary = items[0]
        assert summary["headcount"] == 2
        assert summary["present_days"] == 1.5
        assert summary["avg_work_hours"] == 6.67
        assert summary["late_count"] == 2
        assert summary["total_late_minutes"] == 25

    finally:
        # Remove only this dedicated temporary test database.
        client.drop_database("attendance_summary_test_temp")
        client.close()
