"""Hardware interfaces and simulator backends."""

from driftless_photometry.faults import AcquisitionFault, AcquisitionFaultCode

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
from .rwd_controller_protocol import (
    RWDCommand,
    RWDExcitationMode,
    RWDFrameError,
    RWDFrameStreamDecoder,
    RWDFunction,
    RWDLed,
    RWDMalformedFrame,
    RWDMessage,
    RWDMessageType,
    decode_rwd_frame,
    encode_rwd_message,
)
from .rwd_usb_capture import (
    RWDUSBCaptureDecode,
    RWDUSBCaptureError,
    decode_rwd_usb_capture,
    write_rwd_usb_decode_csv,
)
from .simulator import SimulatedRig
from .thorlabs import ThorlabsSDKStatus, ThorlabsSDKUnavailable, require_sdk, sdk_status

__all__ = [
    "PROTOCOL_VERSION",
    "AcquisitionFault",
    "AcquisitionFaultCode",
    "ControllerLifecycleGuard",
    "ControllerMessage",
    "ControllerState",
    "MessageStreamDecoder",
    "MessageType",
    "ProtocolError",
    "RWDCommand",
    "RWDExcitationMode",
    "RWDFrameError",
    "RWDFrameStreamDecoder",
    "RWDFunction",
    "RWDLed",
    "RWDMalformedFrame",
    "RWDMessage",
    "RWDMessageType",
    "RWDUSBCaptureDecode",
    "RWDUSBCaptureError",
    "RigPacket",
    "RigSource",
    "SerialControllerTransport",
    "SimulatedRig",
    "ThorlabsSDKStatus",
    "ThorlabsSDKUnavailable",
    "decode_message",
    "decode_rwd_frame",
    "decode_rwd_usb_capture",
    "encode_message",
    "encode_rwd_message",
    "require_sdk",
    "sdk_status",
    "write_rwd_usb_decode_csv",
]
