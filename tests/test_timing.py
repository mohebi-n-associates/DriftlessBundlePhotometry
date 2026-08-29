import pytest

from driftless_photometry.config import ChannelConfig, Wavelength
from driftless_photometry.timing import LinearClockMapper, TickUnwrapper, WavelengthSchedule


def test_schedule_cycles_configured_order() -> None:
    channels = tuple(ChannelConfig(wavelength_nm=wavelength) for wavelength in Wavelength)
    schedule = WavelengthSchedule(channels)
    assert [schedule.channel_for_sequence(index).wavelength_nm for index in range(5)] == [
        Wavelength.CONTROL_405,
        Wavelength.GREEN_470,
        Wavelength.RED_565,
        Wavelength.CONTROL_405,
        Wavelength.GREEN_470,
    ]


def test_tick_unwrapper_handles_32_bit_rollover() -> None:
    unwrap = TickUnwrapper(32)
    assert unwrap.unwrap(2**32 - 2) == 2**32 - 2
    assert unwrap.unwrap(3) == 2**32 + 3
    assert unwrap.unwrap(10) == 2**32 + 10


def test_tick_unwrapper_rejects_out_of_order_tick() -> None:
    unwrap = TickUnwrapper(32)
    unwrap.unwrap(100)
    with pytest.raises(ValueError, match="out-of-order"):
        unwrap.unwrap(90)


def test_linear_clock_mapper_fits_drift() -> None:
    mapper = LinearClockMapper()
    mapper.add_anchor(0, 0.25)
    mapper.add_anchor(1_000_000, 1.251)
    assert mapper.map(500_000) == pytest.approx(0.7505)
