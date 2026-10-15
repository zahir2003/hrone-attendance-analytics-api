# Import datetime utilities to create timezone-aware test timestamps.
from datetime import datetime, timezone

# Use an in-memory MongoDB database so tests do not affect real data.
import mongomock

# Use pytest for fixtures and parameterized tests.
import pytest

# Use TestClient to send requests to the FastAPI application.
from fastapi.testclient import TestClient

# Import the application and its database-related functions.
from app import main


@pytest.fixture
def api_client(monkeypatch):
    """Create an isolated database for each API test."""

    # Create an in-memory MongoDB client for testing.
    mock_client = mongomock.MongoClient()
    mock_db = mock_client["attendance_test_db"]

    # Replace the application's database with the test database.
    # This keeps test data separate from real attendance records.
    monkeypatch.setattr(main, "db", mock_db)

    # Create the indexes required by the application.
    main.create_indexes()

    # Add a sample employee for the attendance tests.
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

    # Create a test client to call the API endpoints.
    # The context manager also handles client cleanup.
    with TestClient(main.app, raise_server_exceptions=True) as client:
        yield client

    # Close the in-memory database client after the test.
    mock_client.close()


def epoch_ms(value):
    """Convert an aware datetime to epoch milliseconds."""

    # The API accepts timestamps in milliseconds.
    return int(value.timestamp() * 1000)


def seed_attendance(db, punch_in, punch_out=None):
    """Insert a sample attendance record for testing."""

    # Calculate work hours only if punch-out is available.
    work_hours = (
        main.calculate_work_hours(punch_in, punch_out)
        if punch_out is not None
        else None
    )

    # Save a consistent attendance record in the test database.
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
    # Set punch-in and punch-out times in UTC.
    punch_in = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc)

    # Create an attendance record that is still open.
    seed_attendance(main.db, punch_in)

    # Send the punch-out request using a millisecond timestamp.
    response = api_client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": epoch_ms(punch_out),
        },
    )

    # A successful punch-out should return HTTP 200.
    assert response.status_code == 200

    # Read the attendance details returned by the API.
    body = response.json()

    # Check the punch-out time and calculated work details.
    assert body["punch_out"] == epoch_ms(punch_out)
    assert body["work_hours"] == 9.0
    assert body["half_day"] is False

    # Confirm that the punch-out time was saved in MongoDB.
    saved = main.db.attendance_logs.find_one(
        {"emp_code": "EMP0001", "date": "2026-10-13"}
    )
    assert saved["punch_out"].replace(tzinfo=timezone.utc) == punch_out


def test_punch_out_without_matching_record_returns_404(api_client):
    # Try to punch out when no attendance record exists.
    response = api_client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": 1791900000000,
        },
    )

    # The API should return 404 when the record is not found.
    assert response.status_code == 404


def test_punch_out_already_closed_record_returns_409(api_client):
    # Create a record that already has a punch-out time.
    punch_in = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc)
    seed_attendance(main.db, punch_in, punch_out)

    # Try to punch out again after the record has been closed.
    response = api_client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": epoch_ms(datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)),
        },
    )

    # The API should reject a repeated punch-out with HTTP 409.
    assert response.status_code == 409


# Test multiple invalid timestamps with the same test function.
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
    # Send a punch-out request with an invalid timestamp value.
    response = api_client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": timestamp,
        },
    )

    # Invalid timestamps should be rejected with HTTP 422.
    assert response.status_code == 422


def test_regularization_recalculates_work_hours_and_records_changes(api_client):
    # Create an attendance record with an existing punch-out time.
    punch_in = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc)
    seed_attendance(main.db, punch_in, punch_out)

    # Prepare the corrected punch-out time.
    corrected_punch_out = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)

    # Submit the correction through the regularization endpoint.
    response = api_client.patch(
        "/attendance/EMP0001/2026-10-13",
        json={
            "reason": "Corrected punch-out time",
            "regularized_by": "HR Admin",
            "punch_out": epoch_ms(corrected_punch_out),
        },
    )

    # Check that the correction was accepted.
    assert response.status_code == 200

    # Read the updated attendance record.
    body = response.json()

    # Work hours should be recalculated after changing punch-out.
    assert body["work_hours"] == 8.0
    assert body["half_day"] is False

    # Check that the correction was added to the audit history.
    assert len(body["history"]) == 1
    changes = body["history"][0]["changes"]

    # Changed fields should include punch-out and work hours.
    assert "punch_out" in changes
    assert "work_hours" in changes
    assert changes["work_hours"]["from"] == 9.0
    assert changes["work_hours"]["to"] == 8.0

    # Unchanged fields should not appear in the change history.
    assert "status" not in changes
    assert "punch_in" not in changes


def test_regularization_rejects_a_correction_that_changes_nothing(api_client):
    # Create a record that already has complete attendance times.
    punch_in = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc)
    seed_attendance(main.db, punch_in, punch_out)

    # Submit a correction without changing any attendance fields.
    response = api_client.patch(
        "/attendance/EMP0001/2026-10-13",
        json={
            "reason": "No actual change",
            "regularized_by": "HR Admin",
        },
    )

    # A request that changes nothing should be rejected.
    assert response.status_code == 422
    assert "does not change anything" in response.json()["detail"]

    # A rejected request should not add an audit-history entry.
    saved = main.db.attendance_logs.find_one(
        {"emp_code": "EMP0001", "date": "2026-10-13"}
    )
    assert saved["history"] == []


def test_regularization_succeeds_when_history_field_is_missing(api_client):
    # Create an attendance record without a history field.
    # This checks that older records can still be corrected.
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

    # Correct punch-out so that work hours decrease from 9 to 8.
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

    # Check the recalculated hours and new audit-history entry.
    assert body["work_hours"] == 8.0
    assert len(body["history"]) == 1
    assert body["history"][0]["by"] == "HR Admin"
    assert body["history"][0]["reason"] == "Corrected punch-out time"
