from datetime import datetime

from app.main import (
    IST,
    calculate_work_hours,
    compute_late_minutes,
    compute_overtime,
)


def test_late_grace_period_is_exactly_ten_minutes():
    punch_in = datetime(2026, 10, 13, 9, 40, 0, tzinfo=IST)

    assert compute_late_minutes(punch_in, "09:30", "2026-10-13") == 0


def test_one_second_after_grace_period_is_late():
    punch_in = datetime(2026, 10, 13, 9, 40, 1, tzinfo=IST)

    assert compute_late_minutes(punch_in, "09:30", "2026-10-13") == 10


def test_late_minutes_after_grace_period():
    punch_in = datetime(2026, 10, 13, 9, 41, 0, tzinfo=IST)

    assert compute_late_minutes(punch_in, "09:30", "2026-10-13") == 11


def test_overtime_requires_at_least_thirty_minutes():
    punch_out = datetime(2026, 10, 14, 18, 59, 59, tzinfo=IST)

    assert compute_overtime(punch_out, "18:30", "2026-10-14") == 29


def test_overtime_at_thirty_minutes():
    punch_out = datetime(2026, 10, 14, 19, 0, 0, tzinfo=IST)

    assert compute_overtime(punch_out, "18:30", "2026-10-14") == 30


def test_work_hours_round_half_up():
    punch_in = datetime(2026, 10, 13, 9, 0, 0, tzinfo=IST)
    punch_out = datetime(2026, 10, 13, 13, 30, 0, tzinfo=IST)

    assert calculate_work_hours(punch_in, punch_out) == 4.5


def test_work_hours_round_to_two_decimal_places():
    punch_in = datetime(2026, 10, 13, 9, 0, 0, tzinfo=IST)
    punch_out = datetime(2026, 10, 13, 17, 50, 0, tzinfo=IST)

    assert calculate_work_hours(punch_in, punch_out) == 8.83
