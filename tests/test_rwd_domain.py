import pytest

from driftless_photometry.rwd import (
    RWDEventRecord,
    RWDFluorescenceRecord,
    RWDFluorescenceSample,
    RWDWavelength,
)


def test_rwd_fluorescence_domain_preserves_raw_and_scaled_values() -> None:
    sample = RWDFluorescenceSample(
        wavelength_nm=RWDWavelength.LED_410,
        timestamp_tick=1234,
        raw_value=5678,
        scaled_value=5.678,
    )
    record = RWDFluorescenceRecord(
        machine_name=b"ABCD",
        device_channel=7,
        samples=(sample,),
    )

    assert record.machine_name == b"ABCD"
    assert record.device_channel == 7
    assert record.samples[0].raw_value == 5678


def test_rwd_fluorescence_domain_rejects_duplicate_wavelengths() -> None:
    sample = RWDFluorescenceSample(RWDWavelength.LED_470, 1, 2, 0.002)
    with pytest.raises(ValueError, match="wavelengths must be unique"):
        RWDFluorescenceRecord(b"ABCD", 0, (sample, sample))


def test_rwd_event_domain_preserves_fixed_field_and_on_off_semantics() -> None:
    event = RWDEventRecord(
        machine_name=b"RWD1",
        timestamp_tick=42,
        event_name_raw=b"lever press\x00        ",
        status=0,
    )

    assert event.event_name == "lever press"
    assert event.active is True
    assert RWDEventRecord(b"RWD1", 43, b"lever press\x00        ", 1).active is False


def test_rwd_domain_rejects_out_of_range_wire_values() -> None:
    with pytest.raises(ValueError, match="unsigned 32-bit"):
        RWDFluorescenceSample(RWDWavelength.LED_560, 2**32, 1, 0.001)
    with pytest.raises(ValueError, match="status"):
        RWDEventRecord(b"RWD1", 1, b"event".ljust(20, b"\x00"), 2)
