# Import datetime to create test timestamps.
from datetime import datetime

# Import the timezone and calculation functions from the application.
from app.main import (
    IST,
    calculate_work_hours,
    compute_late_minutes,
    compute_overtime,
)


def test_late_grace_period_is_exactly_ten_minutes():
    # Set punch-in to exactly ten minutes after the shift starts.
    punch_in = datetime(2026, 10, 13, 9, 40, 0, tzinfo=IST)

    # The ten-minute grace period should not count as lateness.
    assert compute_late_minutes(punch_in, "09:30", "2026-10-13") == 0


def test_one_second_after_grace_period_is_late():
    # Set punch-in one second after the grace period ends.
    punch_in = datetime(2026, 10, 13, 9, 40, 1, tzinfo=IST)

    # Verify that lateness is calculated from the shift start.
    assert compute_late_minutes(punch_in, "09:30", "2026-10-13") == 10


def test_late_minutes_after_grace_period():
    # Set punch-in to eleven minutes after the shift starts.
    punch_in = datetime(2026, 10, 13, 9, 41, 0, tzinfo=IST)

    # Verify the lateness value after the grace period.
    assert compute_late_minutes(punch_in, "09:30", "2026-10-13") == 11


def test_overtime_requires_at_least_thirty_minutes():
    # Set punch-out to 29 minutes and 59 seconds after shift end.
    punch_out = datetime(2026, 10, 14, 18, 59, 59, tzinfo=IST)

    # Verify the number of complete overtime minutes.
    assert compute_overtime(punch_out, "18:30", "2026-10-14") == 29


def test_overtime_at_thirty_minutes():
    # Set punch-out to exactly thirty minutes after shift end.
    punch_out = datetime(2026, 10, 14, 19, 0, 0, tzinfo=IST)

    # Check the calculation at the overtime threshold.
    assert compute_overtime(punch_out, "18:30", "2026-10-14") == 30


def test_work_hours_round_half_up():
    # Create a work duration of four hours and thirty minutes.
    punch_in = datetime(2026, 10, 13, 9, 0, 0, tzinfo=IST)
    punch_out = datetime(2026, 10, 13, 13, 30, 0, tzinfo=IST)

    # Verify that the calculated duration is 4.5 hours.
    assert calculate_work_hours(punch_in, punch_out) == 4.5


def test_work_hours_round_to_two_decimal_places():
    # Create a work duration of eight hours and fifty minutes.
    punch_in = datetime(2026, 10, 13, 9, 0, 0, tzinfo=IST)
    punch_out = datetime(2026, 10, 13, 17, 50, 0, tzinfo=IST)

    # Verify that work hours are rounded to two decimal places.
    assert calculate_work_hours(punch_in, punch_out) == 8.83
