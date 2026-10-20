"""A simulated RWD R821 bench shared by the RWD rig and acquisition tests."""

from pathlib import Path

from driftless_photometry.config import SessionConfig, Wavelength, demo_config
from driftless_photometry.hardware.rwd_board_simulator import SimulatedRWDBoard
from driftless_photometry.hardware.rwd_rig import RWDRig, RWDRigTiming
from driftless_photometry.hardware.rwd_rig_simulator import SimulatedRWDCameras, VirtualClock
from driftless_photometry.hardware.rwd_transport import RWDControllerTransport

W410, W470, W560 = Wavelength.CONTROL_410, Wavelength.GREEN_470, Wavelength.RED_560
START_DELAY_S = 0.0005


def rwd_config(
    tmp_path: Path,
    enabled=(W410, W470, W560),
    *,
    fiber_count: int = 2,
    camera2_shift_px: float = 0.0,
    raw_capture: bool = False,
    **camera,
) -> SessionConfig:
    """RWD settings; camera-2 circles sit ``camera2_shift_px`` right of camera 1's."""

    payload = demo_config(
        tmp_path, fiber_count=fiber_count, system="rwd", raw_capture=raw_capture
    ).model_dump()
    for roi in payload["rois"]:
        roi["camera2_center_x_px"] = roi["center_x_px"] + camera2_shift_px
    for channel in payload["channels"]:
        channel["enabled"] = channel["wavelength_nm"] in enabled
    payload["channels"][1]["power_percent"] = 12.5
    payload["camera"].update(camera)
    return SessionConfig.model_validate(payload)


class Bench:
    """A simulated R821 with its two cameras on one virtual clock."""

    def __init__(
        self,
        config: SessionConfig,
        *,
        model_code: int = 0,
        ports: tuple[str, ...] = ("COM9",),
        timing: RWDRigTiming | None = None,
        **camera_options,
    ) -> None:
        self.clock = VirtualClock()
        self.board = SimulatedRWDBoard(clock=self.clock, model_code=model_code)
        self.cameras: SimulatedRWDCameras | None = None
        self.opened_ports: list[str] = []
        self.camera_ids: tuple[str, ...] = ()

        def transport(port: str) -> RWDControllerTransport:
            self.opened_ports.append(port)
            return RWDControllerTransport(
                port, serial_factory=lambda _: self.board, clock=self.clock
            )

        def cameras(settings, user_ids):
            self.camera_ids = user_ids
            self.cameras = SimulatedRWDCameras(
                self.board,
                self.clock,
                config,
                user_ids=user_ids,
                settings=settings,
                start_delay_s=START_DELAY_S,
                **camera_options,
            )
            self.pending_setup(self.cameras)
            return self.cameras

        self.pending_setup = lambda cams: None
        self.rig = RWDRig(
            config,
            transport_factory=transport,
            camera_factory=cameras,
            port_finder=lambda: ports,
            clock=self.clock,
            timing=timing,
        )

    def run(self, duration_s: float):
        return list(self.rig.packets(duration_s))

    def assert_all_off(self) -> None:
        state = self.board.state
        assert not state.sequencer_enabled
        assert not any(state.led_enabled.values())
        assert not any(state.led_value.values())
        assert not any(state.output_enabled.values())
        assert not state.light_on

    def session_s(self, clock_s: float, run: int = 0) -> float:
        """Session time of a clock instant, given session zero at run 0's first trigger."""

        return clock_s - (self.board.runs[0].start_s + START_DELAY_S)


def frames_of(packets):
    return [packet.frame for packet in packets if packet.frame is not None]


def events_of(packets):
    return [event for packet in packets for event in packet.system_events]


def invalid_of(packets):
    return [interval for packet in packets for interval in packet.invalid_times]
