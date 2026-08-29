"""Acquisition lifecycle state machine."""

from __future__ import annotations

from enum import StrEnum


class AcquisitionState(StrEnum):
    DISCONNECTED = "disconnected"
    READY = "ready"
    CALIBRATING = "calibrating"
    ARMED = "armed"
    RECORDING = "recording"
    DRAINING = "draining"
    ERROR = "error"


_ALLOWED: dict[AcquisitionState, set[AcquisitionState]] = {
    AcquisitionState.DISCONNECTED: {AcquisitionState.READY, AcquisitionState.ERROR},
    AcquisitionState.READY: {
        AcquisitionState.DISCONNECTED,
        AcquisitionState.CALIBRATING,
        AcquisitionState.ARMED,
        AcquisitionState.ERROR,
    },
    AcquisitionState.CALIBRATING: {
        AcquisitionState.READY,
        AcquisitionState.ARMED,
        AcquisitionState.ERROR,
    },
    AcquisitionState.ARMED: {
        AcquisitionState.READY,
        AcquisitionState.RECORDING,
        AcquisitionState.ERROR,
    },
    AcquisitionState.RECORDING: {AcquisitionState.DRAINING, AcquisitionState.ERROR},
    AcquisitionState.DRAINING: {AcquisitionState.READY, AcquisitionState.ERROR},
    AcquisitionState.ERROR: {AcquisitionState.DISCONNECTED, AcquisitionState.READY},
}


class AcquisitionStateMachine:
    def __init__(self) -> None:
        self._state = AcquisitionState.DISCONNECTED

    @property
    def state(self) -> AcquisitionState:
        return self._state

    def transition(self, target: AcquisitionState) -> None:
        if target == self._state:
            return
        if target not in _ALLOWED[self._state]:
            raise RuntimeError(f"invalid acquisition transition: {self._state} -> {target}")
        self._state = target
