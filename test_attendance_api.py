# Import datetime utilities to create timezone-aware test timestamps.
from datetime import datetime, timezone

# Import mongomock to simulate MongoDB without using the real database.
import mongomock

# Import pytest for fixtures and parameterized testing.
import pytest

# Import FastAPI's TestClient to send requests to the API during tests.
from fastapi.testclient import TestClient

# Import the main application module and its database-related functions.
from app import main


@pytest.fixture
def api_client(monkeypatch):
    """Use an isolated in-memory database for each test."""

    # Create an in-memory MongoDB client for test isolation.
    mock_client = mongomock.MongoClient()
    mock_db = mock_client["attendance_test_db"]

    # Replace the application's database with the mock database.
    monkeypatch.setattr(main, "db", mock_db)

    # Create the indexes required by the application.
    main.create_indexes()

    # Seed one employee for punch-out tests.
    mock_db.employees.insert_one(
        {
            "emp_code": "EMP0001",
            "name": "Test Employee",
            "email": "test@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }
    )

    # Create a test client for sending requests to the FastAPI application.
    # Disable automatic lifespan startup for this isolated test client.
    with TestClient(main.app, raise_server_exceptions=True) as client:
        yield client

    # Close the mock MongoDB client after the test finishes.
    mock_client.close()


def epoch_ms(value):
    """Convert an aware datetime to epoch milliseconds."""

    # Convert the timestamp from seconds to integer milliseconds.
    return int(value.timestamp() * 1000)


def seed_attendance(db, punch_in, punch_out=None):
    """Insert a consistent test attendance record."""

    # Calculate work hours only when a punch-out time is available.
    work_hours = (
        main.calculate_work_hours(punch_in, punch_out)
        if punch_out is not None
        else None
    )

    # Insert a sample attendance record into the test database.
    db.attendance_logs.insert_one(
        {
            "emp_code": "EMP0001",
            "date": "2026-10-13",
            "status": "PRESENT",
            "punch_in": punch_in,
            "punch_out": punch_out,
            "work_hours": work_hours,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": work_hours is not None and work_hours < 4.50,
            "history": [],
        }
    )


def test_punch_out_success(api_client):
    # Set the punch-in and punch-out times in UTC.
    punch_in = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc)

    # Insert an attendance record that has not been punched out yet.
    seed_attendance(main.db, punch_in)

    # Send a punch-out request with the timestamp in milliseconds.
    response = api_client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": epoch_ms(punch_out),
        },
    )

    # Verify that the API successfully processes the punch-out.
    assert response.status_code == 200

    # Read the returned attendance details.
    body = response.json()

    # Verify the punch-out time and calculated attendance values.
    assert body["punch_out"] == epoch_ms(punch_out)
    assert body["work_hours"] == 9.0
    assert body["half_day"] is False

    # Retrieve the saved record and verify the stored punch-out time.
    saved = main.db.attendance_logs.find_one(
        {"emp_code": "EMP0001", "date": "2026-10-13"}
    )
    assert saved["punch_out"].replace(tzinfo=timezone.utc) == punch_out


def test_punch_out_without_matching_record_returns_404(api_client):
    # Send a punch-out request when no matching attendance record exists.
    response = api_client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": 1791900000000,
        },
    )

    # Verify that the API returns HTTP 404.
    assert response.status_code == 404


def test_punch_out_already_closed_record_returns_409(api_client):
    # Create an attendance record that already has a punch-out time.
    punch_in = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc)
    seed_attendance(main.db, punch_in, punch_out)

    # Attempt to punch out again after the record is already closed.
    response = api_client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": epoch_ms(datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)),
        },
    )

    # Verify that the API rejects the repeated punch-out with HTTP 409.
    assert response.status_code == 409


# Test several invalid timestamp values using the same test function.
@pytest.mark.parametrize(
    "timestamp",
    [
        99999999999,
        4102444800001,
        1791900000000.0,
        "1791900000000",
    ],
)
def test_invalid_punch_out_timestamp_returns_422(api_client, timestamp):
    # Send a punch-out request with the supplied invalid timestamp.
    response = api_client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": timestamp,
        },
    )

    # Verify that invalid timestamps are rejected with HTTP 422.
    assert response.status_code == 422


def test_regularization_recalculates_work_hours_and_records_changes(api_client):
    # Create an attendance record with an existing punch-out time.
    punch_in = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc)
    seed_attendance(main.db, punch_in, punch_out)

    # Prepare a corrected punch-out time for regularization.
    corrected_punch_out = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)

    # Submit the correction through the attendance regularization endpoint.
    response = api_client.patch(
        "/attendance/EMP0001/2026-10-13",
        json={
            "reason": "Corrected punch-out time",
            "regularized_by": "HR Admin",
            "punch_out": epoch_ms(corrected_punch_out),
        },
    )

    # Verify that the correction succeeds.
    assert response.status_code == 200

    # Read the updated attendance record from the response.
    body = response.json()

    # Verify that work hours and half-day status are recalculated.
    assert body["work_hours"] == 8.0
    assert body["half_day"] is False

    # Verify that the audit history contains one entry for this correction.
    assert len(body["history"]) == 1
    changes = body["history"][0]["changes"]

    # Confirm that the changed punch-out and work-hour values are recorded.
    assert "punch_out" in changes
    assert "work_hours" in changes
    assert changes["work_hours"]["from"] == 9.0
    assert changes["work_hours"]["to"] == 8.0

    # Fields that did not change should not appear in the audit entry.
    assert "status" not in changes
    assert "punch_in" not in changes


def test_regularization_rejects_a_correction_that_changes_nothing(api_client):
    # Create an attendance record with an existing punch-out time.
    punch_in = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc)
    seed_attendance(main.db, punch_in, punch_out)

    # Submit a regularization request without changing any attendance fields.
    response = api_client.patch(
        "/attendance/EMP0001/2026-10-13",
        json={
            "reason": "No actual change",
            "regularized_by": "HR Admin",
        },
    )

    # Verify that the API rejects a correction that changes nothing.
    assert response.status_code == 422
    assert "does not change anything" in response.json()["detail"]

    # Confirm that a rejected correction does not create an audit entry.
    saved = main.db.attendance_logs.find_one(
        {"emp_code": "EMP0001", "date": "2026-10-13"}
    )
    assert saved["history"] == []


def test_regularization_succeeds_when_history_field_is_missing(api_client):
    # Create an attendance record without a history field.
    punch_in = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc)

    main.db.attendance_logs.insert_one(
        {
            "emp_code": "EMP0001",
            "date": "2026-10-13",
            "status": "PRESENT",
            "punch_in": punch_in,
            "punch_out": punch_out,
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    )

    # Correct the punch-out time to reduce work hours from 9 to 8.
    response = api_client.patch(
        "/attendance/EMP0001/2026-10-13",
        json={
            "punch_out": int(
                datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc).timestamp() * 1000
            ),
            "reason": "Corrected punch-out time",
            "regularized_by": "HR Admin",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()

    # The correction should succeed and create an audit-history entry.
    assert body["work_hours"] == 8.0
    assert len(body["history"]) == 1
    assert body["history"][0]["by"] == "HR Admin"
    assert body["history"][0]["reason"] == "Corrected punch-out time"
