from __future__ import annotations

from types import SimpleNamespace

import pytest

from driftless_photometry.hardware.power_meter import PowerMeterUnavailable, ThorlabsPowerMeter


class FakeInstrument:
    def __init__(self, identity: str) -> None:
        self.identity = identity
        self.written: list[str] = []
        self.closed = False
        self.timeout = None

    def query(self, command: str) -> str:
        if command == "*IDN?":
            return self.identity + "\n"
        assert command == "MEAS:POW?"
        return "1.25E-04\n"

    def write(self, command: str) -> None:
        self.written.append(command)

    def close(self) -> None:
        self.closed = True


def _visa(resources: dict[str, FakeInstrument]):
    class Manager:
        closed = False

        def list_resources(self):
            return tuple(resources)

        def open_resource(self, name):
            return resources[name]

        def close(self):
            Manager.closed = True

    return lambda: SimpleNamespace(ResourceManager=Manager)


def test_opens_the_first_thorlabs_pm_meter_and_reads_watts() -> None:
    other = FakeInstrument("Keysight,34465A")
    meter_instrument = FakeInstrument("Thorlabs,PM100D,P0012345,2.8.0")
    visa = _visa(
        {
            "USB0::0x0957::0x1A07::MY1::INSTR": other,
            "USB0::0x1313::0x8075::P5000::INSTR": FakeInstrument("Thorlabs,TSP01,1,1"),
            "USB0::0x1313::0x8078::P0012345::INSTR": meter_instrument,
        }
    )

    meter = ThorlabsPowerMeter(visa_loader=visa)
    meter.set_wavelength(470)

    assert meter.description == "Thorlabs,PM100D,P0012345,2.8.0"
    assert meter.read_power_w() == pytest.approx(1.25e-4)
    assert meter_instrument.written == [
        "SENS:POW:UNIT W",
        "SENS:POW:RANG:AUTO ON",
        "CONF:POW",
        "SENS:CORR:WAV 470",
    ]
    assert not other.written
    meter.close()
    assert meter_instrument.closed


def test_reports_a_missing_meter_or_pyvisa() -> None:
    with pytest.raises(PowerMeterUnavailable, match="no Thorlabs PM100 or PM400"):
        ThorlabsPowerMeter(visa_loader=_visa({}))

    def missing():
        raise PowerMeterUnavailable("pyvisa is not installed")

    with pytest.raises(PowerMeterUnavailable, match="pyvisa"):
        ThorlabsPowerMeter(visa_loader=missing)
