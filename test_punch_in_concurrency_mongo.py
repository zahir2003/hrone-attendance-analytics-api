# Import environment-variable support for MongoDB configuration.
import os

# Run multiple requests at the same time for concurrency testing.
from concurrent.futures import ThreadPoolExecutor

# Generate a unique database name for each test run.
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# Import the application whose punch-in endpoint is being tested.
from app import main


def test_simultaneous_punch_in_allows_only_one_request():
    # Connect to MongoDB using the configured connection string.
    uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    client = MongoClient(uri, serverSelectionTimeoutMS=3000)

    # Skip the test if MongoDB is not available.
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        client.close()
        pytest.skip(f"MongoDB is unavailable: {exc}")

    # Use a separate database so the test cannot affect other data.
    database_name = f"attendance_concurrency_test_{uuid4().hex}"
    db = client[database_name]
    original_db = main.db

    try:
        # Add an employee for the concurrent punch-in requests.
        db.employees.insert_one(
            {
                "emp_code": "CONCURRENCY001",
                "name": "Concurrency Test Employee",
                "email": "concurrency-test@example.com",
                "department": "Engineering",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            }
        )

        # Point the application at this test database.
        main.db = db
        main.create_indexes()

        # Use a timestamp for 09:30 AM IST on 13 October 2026.
        punched_at = 1791864000000
        payload = {
            "emp_code": "CONCURRENCY001",
            "punched_at": punched_at,
            "status": "PRESENT",
        }

        def send_punch_in():
            # Give each concurrent request its own test client.
            with TestClient(main.app) as api_client:
                response = api_client.post(
                    "/attendance/punch-in",
                    json=payload,
                )
                return response.status_code, response.text

        # Send ten requests at roughly the same time.
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(lambda _: send_punch_in(), range(10)))

        statuses = [status for status, _ in results]

        # Only one request should create the attendance record.
        assert statuses.count(201) == 1, results

        # The other nine requests should be rejected as duplicates.
        assert statuses.count(409) == 9, results

        # Confirm that the database contains exactly one record.
        assert (
            db.attendance_logs.count_documents(
                {"emp_code": "CONCURRENCY001", "date": "2026-10-13"}
            )
            == 1
        )

    finally:
        # Restore the original database after the test.
        main.db = original_db

        # Delete the temporary database and close the connection.
        client.drop_database(database_name)
        client.close()
