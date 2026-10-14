import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app import main


def test_simultaneous_regularizations_preserve_history():
    uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    client = MongoClient(uri, serverSelectionTimeoutMS=3000)

    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        client.close()
        pytest.skip(f"MongoDB is unavailable: {exc}")

    database_name = f"attendance_regularize_test_{uuid4().hex}"
    db = client[database_name]
    original_db = main.db

    try:
        punch_in = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
        punch_out = datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc)

        db.employees.insert_one(
            {
                "emp_code": "REGCONC001",
                "name": "Regularization Test Employee",
                "email": "regularization-test@example.com",
                "department": "Engineering",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            }
        )

        db.attendance_logs.insert_one(
            {
                "emp_code": "REGCONC001",
                "date": "2026-10-13",
                "status": "PRESENT",
                "punch_in": punch_in,
                "punch_out": punch_out,
                "work_hours": 9.0,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
                "history": [],
            }
        )

        main.db = db
        main.create_indexes()

        # Both corrections change punch-out to a different valid time.
        requests = [
            {
                "punch_out": int(
                    datetime(2026, 10, 13, 12, 30, tzinfo=timezone.utc).timestamp()
                    * 1000
                ),
                "regularized_by": "reviewer_one",
                "reason": "Correction from reviewer one",
            },
            {
                "punch_out": int(
                    datetime(2026, 10, 13, 12, 45, tzinfo=timezone.utc).timestamp()
                    * 1000
                ),
                "regularized_by": "reviewer_two",
                "reason": "Correction from reviewer two",
            },
        ]

        def send_correction(payload):
            with TestClient(main.app) as api_client:
                response = api_client.patch(
                    "/attendance/REGCONC001/2026-10-13",
                    json=payload,
                )
                return response.status_code, response.text

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(send_correction, requests))

        statuses = [status for status, _ in results]

        assert statuses.count(200) == 1, results
        assert statuses.count(409) == 1, results

        record = db.attendance_logs.find_one(
            {"emp_code": "REGCONC001", "date": "2026-10-13"}
        )

        assert record is not None
        assert record["punch_out"] in [
            datetime(2026, 10, 13, 12, 30),
            datetime(2026, 10, 13, 12, 45),
        ]
        assert len(record["history"]) == 1
        assert record["history"][0]["by"] in [
            "reviewer_one",
            "reviewer_two",
        ]

    finally:
        main.db = original_db
        client.drop_database(database_name)
        client.close()
