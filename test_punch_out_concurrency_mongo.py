# Read the MongoDB connection string from the environment.
import os

# Run multiple requests concurrently.
from concurrent.futures import ThreadPoolExecutor

from datetime import datetime, timezone

# Generate a separate database name for each test run.
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# Import the API application.
from app import main


def test_simultaneous_punch_out_allows_only_one_request():
    # Connect to MongoDB using the configured connection string.
    uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    client = MongoClient(uri, serverSelectionTimeoutMS=3000)

    # Skip the test when MongoDB is unavailable.
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        client.close()
        pytest.skip(f"MongoDB is unavailable: {exc}")

    # Keep this test's data separate from other tests.
    database_name = f"attendance_punchout_test_{uuid4().hex}"
    db = client[database_name]
    original_db = main.db

    try:
        # Set the original punch-in and expected punch-out times.
        punch_in = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
        punch_out = datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc)

        # Create the employee who will be used in the test.
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

        # Create an attendance record that has not been punched out yet.
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

        # Make the API use the temporary test database.
        main.db = db
        main.create_indexes()

        # Prepare the punch-out request.
        payload = {
            "emp_code": "CONCURRENCY002",
            "punched_at": int(punch_out.timestamp() * 1000),
        }

        def send_punch_out():
            # Use a separate client for each concurrent request.
            with TestClient(main.app) as api_client:
                response = api_client.post(
                    "/attendance/punch-out",
                    json=payload,
                )
                return response.status_code, response.text

        # Send ten punch-out requests at nearly the same time.
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(lambda _: send_punch_out(), range(10)))

        statuses = [status for status, _ in results]

        # Only one request should successfully close the attendance record.
        assert statuses.count(200) == 1, results

        # The other requests should receive a conflict response.
        assert statuses.count(409) == 9, results

        # Confirm that the record was updated correctly.
        record = db.attendance_logs.find_one(
            {"emp_code": "CONCURRENCY002", "date": "2026-10-13"}
        )
        assert record is not None
        assert record["punch_out"] is not None
        assert record["work_hours"] == 9.0

    finally:
        # Restore the original database to avoid affecting other tests.
        main.db = original_db

        # Remove the temporary database and close the connection.
        client.drop_database(database_name)
        client.close()
