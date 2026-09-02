import pytest

from driftless_photometry.domain import InvalidTimeInterval, SystemEvent


def test_invalid_time_interval_requires_ordered_finite_bounds() -> None:
    with pytest.raises(ValueError, match="ordered and non-negative"):
        InvalidTimeInterval(1.0, 0.5, "clock reset", "controller_reset")
    with pytest.raises(ValueError, match="finite"):
        InvalidTimeInterval(0.0, float("inf"), "clock reset", "controller_reset")


def test_invalid_time_interval_requires_reason_and_source() -> None:
    with pytest.raises(ValueError, match="reason and source_event"):
        InvalidTimeInterval(0.0, 0.5, "", "controller_reset")


def test_system_event_requires_a_finite_timestamp_and_classification() -> None:
    assert SystemEvent(0.5, "camera_disconnect", "USB link lost").detail == "USB link lost"
    with pytest.raises(ValueError, match="timestamp"):
        SystemEvent(float("nan"), "camera_disconnect")
    with pytest.raises(ValueError, match="classification"):
        SystemEvent(0.0, "")
