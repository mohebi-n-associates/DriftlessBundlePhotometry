"""Allied Vision Alvium photometry cameras of an RWD system, triggered by its controller.

Bench-measured on an R821 with two Alvium 1800 U-040m cameras (see
``docs/rwd-direct-control.md``):

- ``DeviceUserID`` "1" images 410/470 nm and "2" images 560 nm;
- the controller triggers only the camera for the active LED, on Line0 falling edges;
- the cameras power up untriggered, so every session must apply the trigger setup.

``vmbpy`` is imported only when cameras are opened, so simulator mode never needs it.
"""

from __future__ import annotations

import contextlib
import importlib
import queue
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import numpy as np
from numpy.typing import NDArray

RWD_CAMERA_USER_IDS = ("1", "2")
_RESTORED_FEATURES = (
    "TriggerMode",
    "TriggerSource",
    "TriggerActivation",
    "PixelFormat",
    "ExposureTime",
    "Gain",
    "ReverseX",
)


class AlviumSDKUnavailable(RuntimeError):
    """Vimba X, VmbPy, or the expected RWD cameras are not available."""


@dataclass(frozen=True, slots=True)
class AlviumSettings:
    exposure_us: float
    gain_db: float = 0.0
    reverse_x_user_ids: tuple[str, ...] = ("2",)
    """OFRS mirrors camera 2 so both cameras show the fiber face the same way round."""

    def __post_init__(self) -> None:
        if self.exposure_us <= 0:
            raise ValueError("exposure must be positive")
        if self.gain_db < 0:
            raise ValueError("gain must be non-negative")


@dataclass(frozen=True, slots=True)
class AlviumFrame:
    camera_user_id: str
    frame_id: int
    camera_timestamp_ns: int
    image: NDArray[np.uint16]
    host_received_s: float


@dataclass(frozen=True, slots=True)
class AlviumCameraDiagnostics:
    delivered: int
    incomplete: int
    queue_overflows: int


def _is_usb3_vision(camera: Any) -> bool:
    """True unless the camera reports a transport layer other than USB3 Vision."""

    try:
        kind = camera.get_transport_layer().get_type()
    except Exception:
        return True
    return getattr(kind, "name", str(kind)) == "U3V"


def load_vmbpy() -> ModuleType:
    try:
        return importlib.import_module("vmbpy")
    except ImportError as error:
        raise AlviumSDKUnavailable(
            "VmbPy is not installed; install driftless-bundle-photometry[rwd] and Allied "
            "Vision Vimba X"
        ) from error


class AlviumTriggeredCameras:
    """Own both RWD photometry cameras for one acquisition. Use as a context manager."""

    def __init__(
        self,
        settings: AlviumSettings,
        *,
        user_ids: Iterable[str] = RWD_CAMERA_USER_IDS,
        queue_size: int = 512,
        clock: Callable[[], float] = time.monotonic,
        vmbpy_loader: Callable[[], ModuleType] = load_vmbpy,
        discovery_timeout_s: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if queue_size <= 0:
            raise ValueError("queue size must be positive")
        if discovery_timeout_s < 0:
            raise ValueError("discovery timeout must be non-negative")
        self._settings = settings
        self._user_ids = tuple(user_ids)
        self._clock = clock
        self._discovery_timeout_s = discovery_timeout_s
        self._sleep = sleep
        self._loader = vmbpy_loader
        self._frames: queue.Queue[AlviumFrame] = queue.Queue(maxsize=queue_size)
        self._vmbpy: ModuleType | None = None
        self._system: Any = None
        self._cameras: dict[str, Any] = {}
        self._originals: dict[str, dict[str, Any]] = {}
        self._streaming: list[Any] = []
        self._delivered = 0
        self._incomplete = 0
        self._overflows = 0

    @property
    def diagnostics(self) -> AlviumCameraDiagnostics:
        return AlviumCameraDiagnostics(self._delivered, self._incomplete, self._overflows)

    def __enter__(self) -> AlviumTriggeredCameras:
        self._vmbpy = self._loader()
        try:
            self._system = self._vmbpy.VmbSystem.get_instance()
            self._system.__enter__()
        except Exception as error:
            self._system = None
            raise AlviumSDKUnavailable(
                "Vimba X could not load its camera transport layers. Install Vimba X with "
                "its USB transport layer and restart DBF so it sees GENICAM_GENTL64_PATH: "
                f"{error}"
            ) from error
        try:
            self._open_cameras()
            for user_id in self._user_ids:
                self._configure(user_id)
            for user_id in self._user_ids:
                camera = self._cameras[user_id]
                camera.start_streaming(self._handler(user_id), buffer_count=16)
                self._streaming.append(camera)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc_info: object) -> None:
        for camera in self._streaming:
            with contextlib.suppress(Exception):
                camera.stop_streaming()
        self._streaming.clear()
        for user_id, camera in self._cameras.items():
            for name, value in self._originals.get(user_id, {}).items():
                with contextlib.suppress(Exception):
                    camera.get_feature_by_name(name).set(value)
            with contextlib.suppress(Exception):
                camera.__exit__(None, None, None)
        self._cameras.clear()
        if self._system is not None:
            try:
                self._system.__exit__(None, None, None)
            finally:
                self._system = None

    def get(self, timeout_s: float) -> AlviumFrame | None:
        """Return the next frame from either camera, or None if none arrives in time."""

        try:
            return self._frames.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def _open_cameras(self) -> None:
        # Right after another session closes them, Vimba X may not list both cameras yet
        # (bench, 2026-10-20), so keep scanning briefly for the missing ones.
        found: dict[str, Any] = {}
        self._cameras = found
        deadline = self._clock() + self._discovery_timeout_s
        while True:
            self._scan(found)
            missing = [user_id for user_id in self._user_ids if user_id not in found]
            if not missing or self._clock() >= deadline:
                break
            self._sleep(0.5)
        if missing:
            raise AlviumSDKUnavailable(
                f"RWD photometry camera(s) with DeviceUserID {', '.join(missing)} not found"
            )

    def _scan(self, found: dict[str, Any]) -> None:
        for camera in self._system.get_all_cameras():
            if not _is_usb3_vision(camera) or any(camera is held for held in found.values()):
                # Vimba X's camera simulators and other transports are never RWD cameras;
                # opening each costs about half a second (bench, 2026-10-20).
                continue
            try:
                camera.__enter__()
            except Exception:
                continue
            try:
                user_id = str(camera.get_feature_by_name("DeviceUserID").get())
            except Exception:
                user_id = ""
            if user_id in self._user_ids and user_id not in found:
                found[user_id] = camera
            else:
                camera.__exit__(None, None, None)

    def _configure(self, user_id: str) -> None:
        camera = self._cameras[user_id]
        feature = camera.get_feature_by_name
        originals: dict[str, Any] = {}
        for name in _RESTORED_FEATURES:
            with contextlib.suppress(Exception):
                originals[name] = feature(name).get()
        self._originals[user_id] = originals
        steps: list[tuple[str, Any]] = [
            ("TriggerSelector", "FrameStart"),
            ("TriggerMode", "Off"),
            ("PixelFormat", "Mono12"),
            ("ExposureAuto", "Off"),
            ("GainAuto", "Off"),
            ("ExposureMode", "Timed"),
            ("ExposureTime", float(self._settings.exposure_us)),
            ("Gain", float(self._settings.gain_db)),
            ("ReverseX", user_id in self._settings.reverse_x_user_ids),
            ("AcquisitionFrameRateEnable", False),
            ("TriggerSource", "Line0"),
            ("TriggerActivation", "FallingEdge"),
            ("TriggerMode", "On"),
        ]
        for name, value in steps:
            feature(name).set(value)

    def _handler(self, user_id: str) -> Callable[[Any, Any, Any], None]:
        complete = self._vmbpy.FrameStatus.Complete if self._vmbpy is not None else None

        def handler(camera: Any, _stream: Any, frame: Any) -> None:
            try:
                if frame.get_status() != complete:
                    self._incomplete += 1
                    return
                image = np.array(frame.as_numpy_ndarray()[..., 0], dtype=np.uint16, copy=True)
                record = AlviumFrame(
                    user_id,
                    int(frame.get_id()),
                    int(frame.get_timestamp()),
                    image,
                    self._clock(),
                )
                try:
                    self._frames.put_nowait(record)
                    self._delivered += 1
                except queue.Full:
                    self._overflows += 1
            finally:
                camera.queue_frame(frame)

        return handler
