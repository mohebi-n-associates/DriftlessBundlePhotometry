"""Typed acquisition faults shared by hardware adapters and the coordinator."""

from __future__ import annotations

from enum import StrEnum


class AcquisitionFaultCode(StrEnum):
    CAMERA_DISCONNECT = "camera_disconnect"
    CONTROLLER_DISCONNECT = "controller_disconnect"
    MALFORMED_CONTROLLER_STREAM = "malformed_controller_stream"
    CLOCK_DISCONTINUITY = "clock_discontinuity"
    QUEUE_BACKPRESSURE = "queue_backpressure"
    STORAGE_WRITE_FAILURE = "storage_write_failure"
    ACQUISITION_FAILURE = "acquisition_failure"


class AcquisitionFault(RuntimeError):
    """A source or coordinator fault with a stable machine-readable code."""

    def __init__(self, code: AcquisitionFaultCode, message: str) -> None:
        super().__init__(message)
        self.code = code
