import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app import main


def test_simultaneous_punch_in_allows_only_one_request():
    uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    client = MongoClient(uri, serverSelectionTimeoutMS=3000)

    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        client.close()
        pytest.skip(f"MongoDB is unavailable: {exc}")

    # Use a unique database so this test cannot overwrite another test's data.
    database_name = f"attendance_concurrency_test_{uuid4().hex}"
    db = client[database_name]
    original_db = main.db

    try:
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

        main.db = db
        main.create_indexes()

        # 09:30 AM IST on 13 October 2026.
        punched_at = 1791864000000
        payload = {
            "emp_code": "CONCURRENCY001",
            "punched_at": punched_at,
            "status": "PRESENT",
        }

        def send_punch_in():
            # Give each worker its own TestClient.
            with TestClient(main.app) as api_client:
                response = api_client.post(
                    "/attendance/punch-in",
                    json=payload,
                )
                return response.status_code, response.text

        # Release ten requests at roughly the same time.
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(lambda _: send_punch_in(), range(10)))

        statuses = [status for status, _ in results]

        assert statuses.count(201) == 1, results
        assert statuses.count(409) == 9, results
        assert (
            db.attendance_logs.count_documents(
                {"emp_code": "CONCURRENCY001", "date": "2026-10-13"}
            )
            == 1
        )

    finally:
        main.db = original_db
        client.drop_database(database_name)
        client.close()
