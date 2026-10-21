from pathlib import Path

from rwd_bench import W410, W470, rwd_config

from driftless_photometry.hardware.alvium import AlviumCameraInfo
from driftless_photometry.hardware.rwd_board_simulator import SimulatedRWDBoard
from driftless_photometry.hardware.rwd_devices import check_rwd_devices
from driftless_photometry.hardware.rwd_rig_simulator import VirtualClock
from driftless_photometry.hardware.rwd_transport import RWDControllerTransport


def _check(config, *, board=None, cameras=(), ports=("COM3",), lister_error=None):
    clock = VirtualClock()
    board = board or SimulatedRWDBoard(clock=clock)

    def lister():
        if lister_error:
            raise RuntimeError(lister_error)
        return cameras

    report = check_rwd_devices(
        config,
        transport_factory=lambda port: RWDControllerTransport(
            port, serial_factory=lambda _: board, clock=clock
        ),
        port_finder=lambda: ports,
        camera_lister=lister,
    )
    return report, board


CAMERAS = (
    AlviumCameraInfo("1", "1800 U-040m", "0721G", "DEV_1"),
    AlviumCameraInfo("2", "1800 U-040m", "06WW9", "DEV_2"),
)


def test_device_check_reports_the_controller_and_both_cameras(tmp_path: Path) -> None:
    report, board = _check(rwd_config(tmp_path), cameras=CAMERAS)

    assert report.ok
    assert report.controller.detail == "R821 on COM3, firmware 2.0.0, build 37619"
    assert [camera.detail for camera in report.cameras] == [
        "1800 U-040m, serial 0721G",
        "1800 U-040m, serial 06WW9",
    ]
    assert not board.is_open
    assert not board.state.light_on
    assert not board.runs


def test_device_check_names_each_missing_device(tmp_path: Path) -> None:
    report, _board = _check(rwd_config(tmp_path), cameras=CAMERAS[:1], ports=())

    assert not report.ok
    assert "no FTDI serial port" in report.controller.detail
    assert report.cameras[0].ok
    assert not report.cameras[1].ok
    assert "no camera with DeviceUserID 2" in report.cameras[1].detail


def test_device_check_ignores_a_camera_these_leds_do_not_use(tmp_path: Path) -> None:
    report, _board = _check(rwd_config(tmp_path, (W410, W470)), cameras=CAMERAS[:1])

    assert report.ok
    assert not report.cameras[1].needed


def test_device_check_reports_a_missing_camera_sdk(tmp_path: Path) -> None:
    report, _board = _check(rwd_config(tmp_path), lister_error="Vimba X is not installed")

    assert report.controller.ok
    assert not report.ok
    assert all("Vimba X is not installed" in camera.detail for camera in report.cameras)


def test_device_check_refuses_other_models(tmp_path: Path) -> None:
    board = SimulatedRWDBoard(clock=VirtualClock(), model_code=2)
    report, _board = _check(rwd_config(tmp_path), board=board, cameras=CAMERAS)

    assert not report.controller.ok
    assert "R811" in report.controller.detail


def test_output_test_drives_one_output_with_the_leds_dark(tmp_path: Path) -> None:
    from driftless_photometry.config import RWDSourceConfig, RWDTTLOutputConfig, SessionConfig
    from driftless_photometry.hardware.rwd_controller_protocol import RWDFunction
    from driftless_photometry.hardware.rwd_devices import drive_rwd_output_test

    payload = rwd_config(tmp_path, (W410, W470)).model_dump()
    payload["source"] = RWDSourceConfig(
        ttl_outputs=(RWDTTLOutputConfig(port=3, frequency_hz=40.0, pulse_width_ms=5),)
    ).model_dump()
    config = SessionConfig.model_validate(payload)
    clock = VirtualClock()
    board = SimulatedRWDBoard(clock=clock)
    lit = []

    def sleep(seconds: float) -> None:
        lit.append(board.state.light_on)
        clock.advance_to(clock.now_s + seconds)

    summary = drive_rwd_output_test(
        config,
        3,
        transport_factory=lambda port: RWDControllerTransport(
            port, serial_factory=lambda _: board, clock=clock
        ),
        port_finder=lambda: ("COM3",),
        clock=clock,
        sleep=sleep,
    )

    assert summary == "Output 3 on COM3 drove 40 Hz, 5 ms pulses for 1 s with the LEDs dark"
    switches = [m.data for m in board.commands(RWDFunction.OUTPUT_ENABLE) if m.data[0] == 3]
    assert [bool(data[1]) for data in switches] == [False, True, False, False]
    assert board.runs and not any(board.runs[0].led_enabled.values())
    assert lit and not any(lit)
    assert not board.state.sequencer_enabled
    assert not any(board.state.output_enabled.values())
