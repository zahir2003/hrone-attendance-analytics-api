# Import datetime to create timestamps for attendance calculation tests.
from datetime import datetime

# Import the timezone and calculation functions from the application.
from app.main import (
    IST,
    calculate_work_hours,
    compute_late_minutes,
    compute_overtime,
)


def test_late_grace_period_is_exactly_ten_minutes():
    # Set punch-in time to exactly ten minutes after the shift starts.
    punch_in = datetime(2026, 10, 13, 9, 40, 0, tzinfo=IST)

    # Ten minutes of lateness falls within the grace period.
    assert compute_late_minutes(punch_in, "09:30", "2026-10-13") == 0


def test_one_second_after_grace_period_is_late():
    # Set punch-in time to one second beyond the ten-minute grace period.
    punch_in = datetime(2026, 10, 13, 9, 40, 1, tzinfo=IST)

    # Verify that the late minutes are calculated from the shift start.
    assert compute_late_minutes(punch_in, "09:30", "2026-10-13") == 10


def test_late_minutes_after_grace_period():
    # Set punch-in time to eleven minutes after the shift starts.
    punch_in = datetime(2026, 10, 13, 9, 41, 0, tzinfo=IST)

    # Verify that the recorded lateness is eleven minutes.
    assert compute_late_minutes(punch_in, "09:30", "2026-10-13") == 11


def test_overtime_requires_at_least_thirty_minutes():
    # Set punch-out time to 29 minutes and 59 seconds after shift end.
    punch_out = datetime(2026, 10, 14, 18, 59, 59, tzinfo=IST)

    # Verify that overtime is reported as 29 whole minutes.
    assert compute_overtime(punch_out, "18:30", "2026-10-14") == 29


def test_overtime_at_thirty_minutes():
    # Set punch-out time to exactly thirty minutes after shift end.
    punch_out = datetime(2026, 10, 14, 19, 0, 0, tzinfo=IST)

    # Verify the overtime calculation at the thirty-minute threshold.
    assert compute_overtime(punch_out, "18:30", "2026-10-14") == 30


def test_work_hours_round_half_up():
    # Set a work duration of exactly four hours and thirty minutes.
    punch_in = datetime(2026, 10, 13, 9, 0, 0, tzinfo=IST)
    punch_out = datetime(2026, 10, 13, 13, 30, 0, tzinfo=IST)

    # Verify that the calculated work duration is 4.5 hours.
    assert calculate_work_hours(punch_in, punch_out) == 4.5


def test_work_hours_round_to_two_decimal_places():
    # Set a work duration of eight hours and fifty minutes.
    punch_in = datetime(2026, 10, 13, 9, 0, 0, tzinfo=IST)
    punch_out = datetime(2026, 10, 13, 17, 50, 0, tzinfo=IST)

    # Verify that work hours are rounded to two decimal places.
    assert calculate_work_hours(punch_in, punch_out) == 8.83
