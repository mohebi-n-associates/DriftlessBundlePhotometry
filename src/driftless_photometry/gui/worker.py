"""Qt worker that owns a headless acquisition engine."""

from __future__ import annotations

import traceback

from PySide6.QtCore import QObject, Signal, Slot

from driftless_photometry.acquisition import (
    AcquisitionEngine,
    AcquisitionProgress,
    preview_duration_s,
    run_preview,
)
from driftless_photometry.config import SessionConfig
from driftless_photometry.hardware import SimulatedRig
from driftless_photometry.state import AcquisitionState


class AcquisitionWorker(QObject):
    progress = Signal(object)
    finalization_progress = Signal(object)
    state_changed = Signal(str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, config: SessionConfig, duration_s: float) -> None:
        super().__init__()
        self._config = config
        self._duration_s = duration_s
        self._engine = AcquisitionEngine()

    @Slot()
    def run(self) -> None:
        try:
            result = self._engine.run(
                self._config,
                SimulatedRig(self._config, realtime=True),
                duration_s=self._duration_s,
                on_progress=self._emit_progress,
                on_state=self._emit_state,
                on_finalization=self.finalization_progress.emit,
            )
        except BaseException:
            self.failed.emit(traceback.format_exc())
        else:
            self.completed.emit(result)

    def request_stop(self) -> None:
        """Thread-safe stop request; callable directly from the GUI thread."""

        self._engine.stop()

    def _emit_progress(self, progress: AcquisitionProgress) -> None:
        self.progress.emit(progress)

    def _emit_state(self, state: AcquisitionState) -> None:
        self.state_changed.emit(state.value)


class PreviewWorker(QObject):
    """Run the non-recording acquisition preflight off the GUI thread."""

    progress = Signal(object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, config: SessionConfig) -> None:
        super().__init__()
        self._config = config
        self.duration_s = preview_duration_s(config)
        self._source = SimulatedRig(config, realtime=True)

    @Slot()
    def run(self) -> None:
        try:
            result = run_preview(
                self._config,
                self._source,
                duration_s=self.duration_s,
                on_progress=self.progress.emit,
            )
        except BaseException:
            self.failed.emit(traceback.format_exc())
        else:
            self.completed.emit(result)

    def request_stop(self) -> None:
        """Thread-safe preview stop request."""

        self._source.stop()
