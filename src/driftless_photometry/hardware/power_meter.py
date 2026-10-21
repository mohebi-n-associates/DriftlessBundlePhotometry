"""Thorlabs PM100/PM400 optical power meters over VISA, for measuring LED power.

``pyvisa`` is imported only when a meter is opened (the ``power-meter`` extra), and it
needs a VISA runtime that can reach the meter's USB TMC interface, such as NI-VISA or
Thorlabs' own TLPM driver. The SCPI commands are those of the PM100 family, which the
PM400 shares. Not yet checked against a physical meter.
"""

from __future__ import annotations

import contextlib
import importlib
from collections.abc import Callable
from types import ModuleType
from typing import Any, Protocol

THORLABS_USB_VENDOR = "0x1313"
SUPPORTED_MODELS = ("PM100", "PM101", "PM102", "PM400")


class PowerMeterUnavailable(RuntimeError):
    """No usable power meter: pyvisa, the VISA runtime, or the meter itself is missing."""


class PowerMeter(Protocol):
    description: str

    def set_wavelength(self, wavelength_nm: float) -> None: ...

    def read_power_w(self) -> float: ...

    def close(self) -> None: ...


def load_pyvisa() -> ModuleType:
    try:
        return importlib.import_module("pyvisa")
    except ImportError as error:
        raise PowerMeterUnavailable(
            "pyvisa is not installed; install driftless-bundle-photometry[power-meter] and "
            "a VISA runtime such as NI-VISA or Thorlabs' TLPM driver"
        ) from error


class ThorlabsPowerMeter:
    """The first Thorlabs PM100-family or PM400 meter VISA lists, or a named resource."""

    def __init__(
        self,
        resource_name: str | None = None,
        *,
        visa_loader: Callable[[], ModuleType] = load_pyvisa,
        timeout_ms: int = 3000,
    ) -> None:
        pyvisa = visa_loader()
        try:
            self._manager = pyvisa.ResourceManager()
        except Exception as error:
            raise PowerMeterUnavailable(
                f"no VISA runtime could be loaded; install NI-VISA or Thorlabs' TLPM driver: "
                f"{error}"
            ) from error
        if resource_name:
            names: tuple[str, ...] = (resource_name,)
        else:
            names = tuple(
                name
                for name in self._manager.list_resources()
                if f"::{THORLABS_USB_VENDOR}::" in name.lower()
            )
        self._instrument: Any = None
        for name in names:
            try:
                instrument = self._manager.open_resource(name)
                instrument.timeout = timeout_ms
                identity = str(instrument.query("*IDN?")).strip()
            except Exception:
                continue
            if any(model in identity.upper() for model in SUPPORTED_MODELS):
                self._instrument = instrument
                self.description = identity
                break
            with contextlib.suppress(Exception):
                instrument.close()
        if self._instrument is None:
            with contextlib.suppress(Exception):
                self._manager.close()
            raise PowerMeterUnavailable(
                "no Thorlabs PM100 or PM400 power meter was found; connect it by USB and "
                "close Thorlabs' Optical Power Monitor, which holds the meter open"
            )
        self._instrument.write("SENS:POW:UNIT W")
        self._instrument.write("SENS:POW:RANG:AUTO ON")
        self._instrument.write("CONF:POW")

    def set_wavelength(self, wavelength_nm: float) -> None:
        """The sensor's responsivity correction; set it to the LED being measured."""

        self._instrument.write(f"SENS:CORR:WAV {wavelength_nm:g}")

    def read_power_w(self) -> float:
        return float(self._instrument.query("MEAS:POW?"))

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._instrument.close()
        with contextlib.suppress(Exception):
            self._manager.close()
