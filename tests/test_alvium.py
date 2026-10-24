from types import SimpleNamespace

import numpy as np
import pytest

from driftless_photometry.hardware.alvium import (
    AlviumCameraDiagnostics,
    AlviumSDKUnavailable,
    AlviumSettings,
    AlviumTriggeredCameras,
    load_vmbpy,
)

COMPLETE, INCOMPLETE = "complete", "incomplete"


class FakeFeature:
    def __init__(self, camera: "FakeCamera", name: str) -> None:
        self._camera, self._name = camera, name

    def get(self) -> object:
        return self._camera.values[self._name]

    def set(self, value: object) -> None:
        self._camera.values[self._name] = value
        self._camera.history.append((self._name, value))


class FakeFrame:
    def __init__(self, frame_id: int, status: str = COMPLETE) -> None:
        self.frame_id, self.status = frame_id, status
        self.pixels = np.full((4, 6, 1), frame_id, dtype=np.uint16)

    def get_status(self) -> str:
        return self.status

    def get_id(self) -> int:
        return self.frame_id

    def get_timestamp(self) -> int:
        return 1_000_000 * self.frame_id

    def as_numpy_ndarray(self) -> np.ndarray:
        return self.pixels


class FakeCamera:
    def __init__(self, user_id: str, transport: str = "U3V") -> None:
        self.transport = transport
        self.values: dict[str, object] = {
            "DeviceUserID": user_id,
            "TriggerMode": "Off",
            "TriggerSource": "Software",
            "TriggerActivation": "RisingEdge",
            "PixelFormat": "Mono8",
            "ExposureTime": 5000.687,
            "Gain": 0.0,
            "ReverseX": False,
        }
        self.history: list[tuple[str, object]] = []
        self.handler = None
        self.open = False
        self.requeued: list[FakeFrame] = []

    def __enter__(self) -> "FakeCamera":
        self.open = True
        return self

    def __exit__(self, *exc: object) -> None:
        self.open = False

    def get_feature_by_name(self, name: str) -> FakeFeature:
        return FakeFeature(self, name)

    def get_transport_layer(self) -> SimpleNamespace:
        kind = SimpleNamespace(name=self.transport)
        return SimpleNamespace(get_type=lambda: kind)

    def start_streaming(self, handler, buffer_count: int) -> None:
        self.handler = handler
        self.buffer_count = buffer_count

    def stop_streaming(self) -> None:
        self.handler = None

    def queue_frame(self, frame: FakeFrame) -> None:
        self.requeued.append(frame)

    def emit(self, frame: FakeFrame) -> None:
        self.handler(self, None, frame)


class FakeSystem:
    def __init__(self, cameras: list[FakeCamera], fail_on_enter: bool = False) -> None:
        self.cameras, self.fail_on_enter, self.entered = cameras, fail_on_enter, False

    def __enter__(self) -> "FakeSystem":
        if self.fail_on_enter:
            raise RuntimeError("VmbError.NoTL")
        self.entered = True
        return self

    def __exit__(self, *exc: object) -> None:
        self.entered = False

    def get_all_cameras(self) -> list[FakeCamera]:
        return self.cameras


def _module(system: FakeSystem) -> SimpleNamespace:
    return SimpleNamespace(
        VmbSystem=SimpleNamespace(get_instance=lambda: system),
        FrameStatus=SimpleNamespace(Complete=COMPLETE, Incomplete=INCOMPLETE),
    )


def _cameras(system: FakeSystem, **kwargs: object) -> AlviumTriggeredCameras:
    kwargs.setdefault("discovery_timeout_s", 0.0)
    return AlviumTriggeredCameras(
        AlviumSettings(exposure_us=10_000.0), vmbpy_loader=lambda: _module(system), **kwargs
    )


def test_configures_hardware_trigger_and_restores_settings() -> None:
    green, red, other = FakeCamera("1"), FakeCamera("2"), FakeCamera("0")
    system = FakeSystem([red, other, green])

    with _cameras(system) as cameras:
        assert green.open and red.open and not other.open
        for camera in (green, red):
            assert camera.values["TriggerMode"] == "On"
            assert camera.values["TriggerSource"] == "Line0"
            assert camera.values["TriggerActivation"] == "FallingEdge"
            assert camera.values["PixelFormat"] == "Mono12"
            assert camera.values["ExposureTime"] == 10_000.0
            assert camera.history[:3] == [
                ("TriggerSelector", "FrameStart"),
                ("TriggerMode", "Off"),
                ("PixelFormat", "Mono12"),
            ]
            assert camera.history[-1] == ("TriggerMode", "On")
        assert green.values["ReverseX"] is False
        assert red.values["ReverseX"] is True

        green.emit(FakeFrame(7))
        frame = cameras.get(timeout_s=0.1)

    assert frame is not None
    assert (frame.camera_user_id, frame.frame_id, frame.camera_timestamp_ns) == ("1", 7, 7_000_000)
    assert frame.image.dtype == np.uint16
    assert frame.image.shape == (4, 6)
    assert frame.image.base is None
    for camera in (green, red):
        assert camera.values["TriggerMode"] == "Off"
        assert camera.values["PixelFormat"] == "Mono8"
        assert camera.values["ReverseX"] is False
        assert not camera.open and camera.handler is None
    assert not system.entered


def test_incomplete_frames_and_queue_overflow_are_counted_and_requeued() -> None:
    green, red = FakeCamera("1"), FakeCamera("2")
    with _cameras(FakeSystem([green, red]), queue_size=1) as cameras:
        green.emit(FakeFrame(1))
        green.emit(FakeFrame(2))
        red.emit(FakeFrame(3, INCOMPLETE))
        assert cameras.diagnostics == AlviumCameraDiagnostics(1, 1, 1)
        assert cameras.get(0.01).frame_id == 1
        assert cameras.get(0.01) is None
    assert len(green.requeued) == 2
    assert len(red.requeued) == 1


def test_missing_camera_is_reported_and_others_are_released() -> None:
    green = FakeCamera("1")
    system = FakeSystem([green])
    with pytest.raises(AlviumSDKUnavailable, match="DeviceUserID 2"):
        _cameras(system).__enter__()
    assert not green.open
    assert not system.entered


def test_missing_transport_layers_explain_the_fix() -> None:
    with pytest.raises(AlviumSDKUnavailable, match="GENICAM_GENTL64_PATH"):
        _cameras(FakeSystem([], fail_on_enter=True)).__enter__()


def test_missing_vmbpy_points_to_the_rwd_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_name: str) -> object:
        raise ImportError("No module named 'vmbpy'")

    monkeypatch.setattr("importlib.import_module", missing)
    with pytest.raises(AlviumSDKUnavailable, match=r"\[rwd\]"):
        load_vmbpy()


def test_settings_validation() -> None:
    with pytest.raises(ValueError, match="exposure"):
        AlviumSettings(exposure_us=0)
    with pytest.raises(ValueError, match="gain"):
        AlviumSettings(exposure_us=1, gain_db=-1)
    with pytest.raises(ValueError, match="queue"):
        AlviumTriggeredCameras(AlviumSettings(exposure_us=1), queue_size=0)


def test_skips_cameras_on_other_transport_layers() -> None:
    green, red = FakeCamera("1"), FakeCamera("2")
    simulator = FakeCamera("1", transport="Custom")
    system = FakeSystem([simulator, green, red])

    with _cameras(system):
        assert green.open and red.open
        assert not simulator.open
        assert simulator.history == []


def test_rescans_for_a_camera_that_vimba_has_not_listed_yet() -> None:
    green, red = FakeCamera("1"), FakeCamera("2")
    system = FakeSystem([red])
    now = {"s": 0.0}
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["s"] += seconds
        system.cameras = [red, green]

    with _cameras(system, discovery_timeout_s=5.0, clock=lambda: now["s"], sleep=sleep):
        assert green.open and red.open
    assert sleeps == [0.5]


def test_gives_up_after_the_discovery_timeout() -> None:
    red = FakeCamera("2")
    system = FakeSystem([red])
    now = {"s": 0.0}

    def sleep(seconds: float) -> None:
        now["s"] += seconds

    with pytest.raises(AlviumSDKUnavailable, match="DeviceUserID 1"):
        _cameras(system, discovery_timeout_s=2.0, clock=lambda: now["s"], sleep=sleep).__enter__()
    assert now["s"] == pytest.approx(2.0)
    assert not red.open


def test_streams_with_enough_buffers_for_high_frame_rates() -> None:
    green, red = FakeCamera("1"), FakeCamera("2")

    with _cameras(FakeSystem([green, red])):
        assert green.buffer_count == red.buffer_count == 64
    with _cameras(FakeSystem([green, red]), buffer_count=8):
        assert green.buffer_count == 8
    with pytest.raises(ValueError, match="two stream buffers"):
        _cameras(FakeSystem([green, red]), buffer_count=1)


def test_each_camera_is_opened_with_its_own_gain() -> None:
    green, red = FakeCamera("1"), FakeCamera("2")
    settings = AlviumSettings(exposure_us=10_000, gain_db=12.0, camera2_gain_db=3.0)
    cameras = AlviumTriggeredCameras(
        settings, vmbpy_loader=lambda: _module(FakeSystem([green, red])), discovery_timeout_s=0.0
    )

    with cameras:
        assert (green.values["Gain"], red.values["Gain"]) == (12.0, 3.0)
        cameras.set_gain(5.0, "2")
        assert (green.values["Gain"], red.values["Gain"]) == (12.0, 5.0)
        cameras.set_gain(1.0)
        assert (green.values["Gain"], red.values["Gain"]) == (1.0, 1.0)
