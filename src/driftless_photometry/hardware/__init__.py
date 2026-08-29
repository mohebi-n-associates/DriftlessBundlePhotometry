"""Hardware interfaces and simulator backends."""

from .base import RigPacket, RigSource
from .controller_protocol import (
    PROTOCOL_VERSION,
    ControllerLifecycleGuard,
    ControllerMessage,
    ControllerState,
    MessageStreamDecoder,
    MessageType,
    ProtocolError,
    decode_message,
    encode_message,
)
from .controller_serial import SerialControllerTransport
from .simulator import SimulatedRig
from .thorlabs import ThorlabsSDKStatus, ThorlabsSDKUnavailable, require_sdk, sdk_status

__all__ = [
    "PROTOCOL_VERSION",
    "ControllerLifecycleGuard",
    "ControllerMessage",
    "ControllerState",
    "MessageStreamDecoder",
    "MessageType",
    "ProtocolError",
    "RigPacket",
    "RigSource",
    "SerialControllerTransport",
    "SimulatedRig",
    "ThorlabsSDKStatus",
    "ThorlabsSDKUnavailable",
    "decode_message",
    "encode_message",
    "require_sdk",
    "sdk_status",
]
