from datetime import datetime, timezone

import mongomock
import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def api_client(monkeypatch):
    """Use an isolated in-memory database for each test."""
    mock_client = mongomock.MongoClient()
    mock_db = mock_client["attendance_test_db"]

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

    # Disable automatic lifespan startup for this isolated test client.
    with TestClient(main.app, raise_server_exceptions=True) as client:
        yield client

    mock_client.close()


def epoch_ms(value):
    """Convert an aware datetime to epoch milliseconds."""
    return int(value.timestamp() * 1000)


def seed_attendance(db, punch_in, punch_out=None):
    """Insert a consistent test attendance record."""
    work_hours = (
        main.calculate_work_hours(punch_in, punch_out)
        if punch_out is not None
        else None
    )

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
    punch_in = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc)

    seed_attendance(main.db, punch_in)

    response = api_client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": epoch_ms(punch_out),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["punch_out"] == epoch_ms(punch_out)
    assert body["work_hours"] == 9.0
    assert body["half_day"] is False

    saved = main.db.attendance_logs.find_one(
        {"emp_code": "EMP0001", "date": "2026-10-13"}
    )
    assert saved["punch_out"].replace(tzinfo=timezone.utc) == punch_out


def test_punch_out_without_matching_record_returns_404(api_client):
    response = api_client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": 1791900000000,
        },
    )

    assert response.status_code == 404


def test_punch_out_already_closed_record_returns_409(api_client):
    punch_in = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc)

    seed_attendance(main.db, punch_in, punch_out)

    response = api_client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": epoch_ms(datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)),
        },
    )

    assert response.status_code == 409


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
    response = api_client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": timestamp,
        },
    )

    assert response.status_code == 422


def test_regularization_recalculates_work_hours_and_records_changes(api_client):
    punch_in = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc)

    seed_attendance(main.db, punch_in, punch_out)

    corrected_punch_out = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)

    response = api_client.patch(
        "/attendance/EMP0001/2026-10-13",
        json={
            "reason": "Corrected punch-out time",
            "regularized_by": "HR Admin",
            "punch_out": epoch_ms(corrected_punch_out),
        },
    )

    assert response.status_code == 200

    body = response.json()
    assert body["work_hours"] == 8.0
    assert body["half_day"] is False
    assert len(body["history"]) == 1

    changes = body["history"][0]["changes"]
    assert "punch_out" in changes
    assert "work_hours" in changes
    assert changes["work_hours"]["from"] == 9.0
    assert changes["work_hours"]["to"] == 8.0

    # Fields that did not change should not appear in the audit entry.
    assert "status" not in changes
    assert "punch_in" not in changes


def test_regularization_rejects_a_correction_that_changes_nothing(api_client):
    punch_in = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc)

    seed_attendance(main.db, punch_in, punch_out)

    response = api_client.patch(
        "/attendance/EMP0001/2026-10-13",
        json={
            "reason": "No actual change",
            "regularized_by": "HR Admin",
        },
    )

    assert response.status_code == 422
    assert "does not change anything" in response.json()["detail"]

    saved = main.db.attendance_logs.find_one(
        {"emp_code": "EMP0001", "date": "2026-10-13"}
    )
    assert saved["history"] == []
