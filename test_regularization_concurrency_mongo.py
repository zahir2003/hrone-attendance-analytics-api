# Read the MongoDB connection string from the environment.
import os

# Run two regularization requests at the same time.
from concurrent.futures import ThreadPoolExecutor

# Create a barrier to synchronize both requests.
from threading import Barrier

# Mock the database read method during this concurrency test.
from unittest.mock import patch

from datetime import datetime, timezone

# Generate a unique database name for this test.
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# Import the API application.
from app import main


def test_simultaneous_regularizations_preserve_history():
    # Connect to MongoDB.
    uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    client = MongoClient(uri, serverSelectionTimeoutMS=3000)

    # Skip the test if MongoDB is not available.
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        client.close()
        pytest.skip(f"MongoDB is unavailable: {exc}")

    # Use a temporary database so test data stays isolated.
    database_name = f"attendance_regularize_test_{uuid4().hex}"
    db = client[database_name]
    original_db = main.db

    try:
        # Set the original attendance timestamps.
        punch_in = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
        punch_out = datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc)

        # Add the employee used for regularization.
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

        # Add the original attendance record with an empty history.
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

        # Make the API use this database and create its indexes.
        main.db = db
        main.create_indexes()

        # Both requests try to correct punch-out to different valid times.
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

        # Make both requests read the original record before either updates it.
        read_barrier = Barrier(2)
        original_find_one = db.attendance_logs.find_one

        def synchronized_find_one(*args, **kwargs):
            # Read the record before waiting for the other request.
            record = original_find_one(*args, **kwargs)

            # Synchronize only reads of the attendance record under test.
            if (
                record is not None
                and record.get("emp_code") == "REGCONC001"
                and record.get("date") == "2026-10-13"
            ):
                read_barrier.wait(timeout=5)

            return record

        def send_correction(payload):
            # Send one regularization request through the API.
            with TestClient(main.app) as api_client:
                with patch.object(
                    db.attendance_logs,
                    "find_one",
                    side_effect=synchronized_find_one,
                ):
                    response = api_client.patch(
                        "/attendance/REGCONC001/2026-10-13",
                        json=payload,
                    )
                    return response.status_code, response.text

        # Run both corrections concurrently to test update conflicts.
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(send_correction, requests))

        statuses = [status for status, _ in results]

        # One correction should succeed.
        assert statuses.count(200) == 1, results

        # The other correction should be rejected because the record changed.
        assert statuses.count(409) == 1, results

        # Read the final attendance record from MongoDB.
        record = original_find_one({"emp_code": "REGCONC001", "date": "2026-10-13"})

        # Confirm that the record exists and one correction was saved.
        assert record is not None
        assert record["punch_out"] in [
            datetime(2026, 10, 13, 12, 30),
            datetime(2026, 10, 13, 12, 45),
        ]
        assert len(record["history"]) == 1

        # The history should identify whichever reviewer succeeded.
        assert record["history"][0]["by"] in [
            "reviewer_one",
            "reviewer_two",
        ]

    finally:
        # Restore the original database after the test.
        main.db = original_db

        # Delete the temporary database and close the connection.
        client.drop_database(database_name)
        client.close()
