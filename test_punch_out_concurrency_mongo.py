import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app import main


def test_simultaneous_punch_out_allows_only_one_request():
    uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    client = MongoClient(uri, serverSelectionTimeoutMS=3000)

    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        client.close()
        pytest.skip(f"MongoDB is unavailable: {exc}")

    database_name = f"attendance_punchout_test_{uuid4().hex}"
    db = client[database_name]
    original_db = main.db

    try:
        punch_in = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
        punch_out = datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc)

        db.employees.insert_one(
            {
                "emp_code": "CONCURRENCY002",
                "name": "Punch-Out Test Employee",
                "email": "punchout-test@example.com",
                "department": "Engineering",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            }
        )

        db.attendance_logs.insert_one(
            {
                "emp_code": "CONCURRENCY002",
                "date": "2026-10-13",
                "status": "PRESENT",
                "punch_in": punch_in,
                "punch_out": None,
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
                "history": [],
            }
        )

        main.db = db
        main.create_indexes()

        payload = {
            "emp_code": "CONCURRENCY002",
            "punched_at": int(punch_out.timestamp() * 1000),
        }

        def send_punch_out():
            with TestClient(main.app) as api_client:
                response = api_client.post(
                    "/attendance/punch-out",
                    json=payload,
                )
                return response.status_code, response.text

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(lambda _: send_punch_out(), range(10)))

        statuses = [status for status, _ in results]

        assert statuses.count(200) == 1, results
        assert statuses.count(409) == 9, results

        record = db.attendance_logs.find_one(
            {"emp_code": "CONCURRENCY002", "date": "2026-10-13"}
        )
        assert record is not None
        assert record["punch_out"] is not None
        assert record["work_hours"] == 9.0

    finally:
        main.db = original_db
        client.drop_database(database_name)
        client.close()
