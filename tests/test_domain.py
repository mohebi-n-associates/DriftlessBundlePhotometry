import pytest

from driftless_photometry.domain import InvalidTimeInterval


def test_invalid_time_interval_requires_ordered_finite_bounds() -> None:
    with pytest.raises(ValueError, match="ordered and non-negative"):
        InvalidTimeInterval(1.0, 0.5, "clock reset", "controller_reset")
    with pytest.raises(ValueError, match="finite"):
        InvalidTimeInterval(0.0, float("inf"), "clock reset", "controller_reset")


def test_invalid_time_interval_requires_reason_and_source() -> None:
    with pytest.raises(ValueError, match="reason and source_event"):
        InvalidTimeInterval(0.0, 0.5, "", "controller_reset")
