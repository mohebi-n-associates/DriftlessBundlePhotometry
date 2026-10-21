"""Check that an RWD controller and its photometry cameras are present, like OFRS's
connection screen. The controller check handshakes and forces the board all-off; the
camera check only reads camera identities. No LED is switched on."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

from driftless_photometry.config import SessionConfig

from .alvium import AlviumCameraInfo, list_alvium_cameras
from .rwd_controller_protocol import device_enable, output_config, output_enable
from .rwd_rig import RWDTransportFactory, connect_rwd_controller
from .rwd_timing import RWDExcitationPlan
from .rwd_transport import RWDControllerTransport, candidate_rwd_ports

_CAMERA_ROLES = {"1": "Camera 1 (410/470 nm)", "2": "Camera 2 (560 nm)"}


@dataclass(frozen=True, slots=True)
class DeviceStatus:
    name: str
    ok: bool
    detail: str
    needed: bool = True


@dataclass(frozen=True, slots=True)
class RWDDeviceReport:
    controller: DeviceStatus
    cameras: tuple[DeviceStatus, ...]

    @property
    def devices(self) -> tuple[DeviceStatus, ...]:
        return (self.controller, *self.cameras)

    @property
    def ok(self) -> bool:
        return all(device.ok for device in self.devices if device.needed)


def check_rwd_devices(
    config: SessionConfig,
    *,
    transport_factory: RWDTransportFactory = RWDControllerTransport,
    port_finder: Callable[[], tuple[str, ...]] = candidate_rwd_ports,
    camera_lister: Callable[[], tuple[AlviumCameraInfo, ...]] = list_alvium_cameras,
) -> RWDDeviceReport:
    """Report the controller and both cameras; never raises for a missing device."""

    controller = _check_controller(config, transport_factory, port_finder)
    needed = {str(camera) for camera in RWDExcitationPlan.from_config(config).cameras}
    try:
        found = camera_lister()
        error = None
    except Exception as failure:  # the SDK itself is missing or broken
        found, error = (), str(failure)
    cameras = []
    for user_id in ("1", "2"):
        name = _CAMERA_ROLES[user_id]
        matches = [camera for camera in found if camera.user_id == user_id]
        if error is not None:
            status = DeviceStatus(name, False, error, user_id in needed)
        elif len(matches) == 1:
            camera = matches[0]
            status = DeviceStatus(
                name, True, f"{camera.model}, serial {camera.serial}", user_id in needed
            )
        elif matches:
            status = DeviceStatus(
                name,
                False,
                f"{len(matches)} cameras have DeviceUserID {user_id}",
                user_id in needed,
            )
        else:
            busy = [camera for camera in found if "in use" in camera.camera_id]
            hint = " (a camera is open in another program)" if busy else ""
            status = DeviceStatus(
                name, False, f"no camera with DeviceUserID {user_id}{hint}", user_id in needed
            )
        if not status.needed and status.ok:
            status = DeviceStatus(name, True, status.detail + "; not used by these LEDs", False)
        cameras.append(status)
    return RWDDeviceReport(controller, tuple(cameras))


def _check_controller(
    config: SessionConfig,
    transport_factory: RWDTransportFactory,
    port_finder: Callable[[], tuple[str, ...]],
) -> DeviceStatus:
    if config.source.kind != "rwd":
        raise ValueError("the device check needs RWD settings")
    try:
        transport, port, identity = connect_rwd_controller(
            config.source.controller_port,  # type: ignore[union-attr]
            transport_factory=transport_factory,
            port_finder=port_finder,
        )
    except Exception as error:
        return DeviceStatus("Controller", False, str(error))
    with suppress(Exception):
        transport.close()
    firmware = ".".join(str(part) for part in identity.firmware)
    return DeviceStatus(
        "Controller",
        True,
        f"{identity.model_name} on {port}, firmware {firmware}, build {identity.build}",
    )


def drive_rwd_output_test(
    config: SessionConfig,
    port: int,
    *,
    seconds: float = 1.0,
    transport_factory: RWDTransportFactory = RWDControllerTransport,
    port_finder: Callable[[], tuple[str, ...]] = candidate_rwd_ports,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Drive one TTL output's first-step pulse train for ``seconds`` with the LEDs dark.

    The sequencer runs with every LED disabled at 0 %, as when DBF waits for a TTL
    start, because outputs were only bench-checked with it running. Anything connected
    to the output, such as a laser, is driven for that time. Returns a summary.
    """

    if config.source.kind != "rwd":
        raise ValueError("the output test needs RWD settings")
    if not 0 < seconds <= 10:
        raise ValueError("an output test lasts between 0 and 10 s")
    output = next(
        (item for item in config.source.ttl_outputs if item.port == port),  # type: ignore[union-attr]
        None,
    )
    frequency_hz, pulse_width_ms = (
        (10.0, 10)
        if output is None
        else (
            output.frequency_hz,
            output.pulse_width_ms,
        )
    )
    transport, controller_port, _identity = connect_rwd_controller(
        config.source.controller_port,  # type: ignore[union-attr]
        transport_factory=transport_factory,
        port_finder=port_finder,
    )
    try:
        for command in RWDExcitationPlan.from_config(config).dark_commands():
            transport.command(command)
        transport.command(device_enable(True))
        transport.command(output_config(port, frequency_hz, pulse_width_ms))
        transport.command(output_enable(port, True))
        deadline = clock() + seconds
        while clock() < deadline:
            transport.poll()
            sleep(0.01)
        transport.command(output_enable(port, False))
        transport.command(device_enable(False))
    finally:
        transport.close()
    return (
        f"Output {port} on {controller_port} drove {frequency_hz:g} Hz, {pulse_width_ms} ms "
        f"pulses for {seconds:g} s with the LEDs dark"
    )
