"""Check that an RWD controller and its photometry cameras are present, like OFRS's
connection screen. The controller check handshakes and forces the board all-off; the
camera check only reads camera identities. No LED is switched on."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

from driftless_photometry.config import SessionConfig

from .alvium import AlviumCameraInfo, list_alvium_cameras
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
