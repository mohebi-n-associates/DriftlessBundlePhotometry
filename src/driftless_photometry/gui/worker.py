"""Qt worker that owns a headless acquisition engine."""

from __future__ import annotations

import traceback
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal, Slot

from driftless_photometry.acquisition import (
    AcquisitionCancelled,
    AcquisitionEngine,
    AcquisitionProgress,
    preview_duration_s,
    run_live_view,
    run_preview,
)
from driftless_photometry.config import RWDSourceConfig, SessionConfig
from driftless_photometry.hardware import RigSource, RWDRig, SimulatedRig
from driftless_photometry.hardware.rwd_devices import RWDDeviceReport
from driftless_photometry.state import AcquisitionState

RigFactory = Callable[[SessionConfig], RigSource]
DeviceChecker = Callable[[SessionConfig], RWDDeviceReport]


def default_rig_factory(config: SessionConfig) -> RigSource:
    """RWD settings drive the RWD hardware; Custom settings run the simulator."""

    if isinstance(config.source, RWDSourceConfig):
        return RWDRig(config)
    return SimulatedRig(config, realtime=True)


class AcquisitionWorker(QObject):
    progress = Signal(object)
    finalization_progress = Signal(object)
    state_changed = Signal(str)
    completed = Signal(object)
    failed = Signal(str)
    cancelled = Signal(str)

    def __init__(
        self,
        config: SessionConfig,
        duration_s: float,
        rig_factory: RigFactory = default_rig_factory,
    ) -> None:
        super().__init__()
        self._config = config
        self._duration_s = duration_s
        self._engine = AcquisitionEngine()
        self._rig_factory = rig_factory

    @Slot()
    def run(self) -> None:
        try:
            result = self._engine.run(
                self._config,
                self._rig_factory(self._config),
                duration_s=self._duration_s,
                on_progress=self._emit_progress,
                on_state=self._emit_state,
                on_finalization=self.finalization_progress.emit,
            )
        except AcquisitionCancelled as cancelled:
            self.cancelled.emit(str(cancelled))
        except BaseException:
            self.failed.emit(traceback.format_exc())
        else:
            self.completed.emit(result)

    def request_stop(self) -> None:
        """Thread-safe stop request; callable directly from the GUI thread."""

        self._engine.stop()

    def mark_event(self, name: str, source: str) -> None:
        """Thread-safe operator event mark, timed now."""

        self._engine.mark_event(name, source)

    def trigger_output(self, port: int) -> None:
        """Thread-safe manual start of a TTL output program."""

        self._engine.trigger_output(port)

    def _emit_progress(self, progress: AcquisitionProgress) -> None:
        self.progress.emit(progress)

    def _emit_state(self, state: AcquisitionState) -> None:
        self.state_changed.emit(state.value)


class PreviewWorker(QObject):
    """Run the non-recording acquisition preflight off the GUI thread."""

    progress = Signal(object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        config: SessionConfig,
        rig_factory: RigFactory = default_rig_factory,
        *,
        duration_s: float | None = None,
        allow_power_changes: bool = False,
    ) -> None:
        super().__init__()
        self._config = config
        self.duration_s = preview_duration_s(config) if duration_s is None else duration_s
        self._allow_power_changes = allow_power_changes
        self._source = rig_factory(config)

    @Slot()
    def run(self) -> None:
        try:
            result = run_preview(
                self._config,
                self._source,
                duration_s=self.duration_s,
                on_progress=self.progress.emit,
                allow_power_changes=self._allow_power_changes,
            )
        except BaseException:
            self.failed.emit(traceback.format_exc())
        else:
            self.completed.emit(result)

    def request_stop(self) -> None:
        """Thread-safe preview stop request."""

        self._source.stop()

    def request_led_power(self, wavelength, percent: float) -> None:
        request = getattr(self._source, "request_led_power", None)
        if request is not None:
            request(wavelength, percent)


class LiveViewWorker(QObject):
    """Stream camera frames for focusing and ROI placement; records nothing."""

    frame = Signal(object)
    completed = Signal(int)
    failed = Signal(str)

    def __init__(
        self, config: SessionConfig, rig_factory: RigFactory = default_rig_factory
    ) -> None:
        super().__init__()
        self._config = config
        self._source = rig_factory(config)

    @Slot()
    def run(self) -> None:
        try:
            count = run_live_view(self._config, self._source, on_frame=self.frame.emit)
        except BaseException:
            self.failed.emit(traceback.format_exc())
        else:
            self.completed.emit(count)

    def request_stop(self) -> None:
        """Thread-safe live-view stop request."""

        self._source.stop()

    def request_led_power(self, wavelength, percent: float) -> None:
        request = getattr(self._source, "request_led_power", None)
        if request is not None:
            request(wavelength, percent)

    def request_gain(self, gain_db: float) -> None:
        """Change camera gain while streaming, if the source supports it."""

        request = getattr(self._source, "request_gain", None)
        if request is not None:
            request(gain_db)


class DeviceCheckWorker(QObject):
    """Check the RWD controller and cameras off the GUI thread; no LED is switched on."""

    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, config: SessionConfig, checker: DeviceChecker) -> None:
        super().__init__()
        self._config = config
        self._checker = checker

    @Slot()
    def run(self) -> None:
        try:
            report = self._checker(self._config)
        except BaseException:
            self.failed.emit(traceback.format_exc())
        else:
            self.finished.emit(report)


class BoardTaskWorker(QObject):
    """Run one short controller task off the GUI thread, such as an output test."""

    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, task: Callable[[], object]) -> None:
        super().__init__()
        self._task = task

    @Slot()
    def run(self) -> None:
        try:
            result = self._task()
        except BaseException:
            self.failed.emit(traceback.format_exc())
        else:
            self.finished.emit(result)
