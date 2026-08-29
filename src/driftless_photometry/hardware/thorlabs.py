"""Lazy boundary for the vendor scientific-camera SDK.

Physical acquisition is intentionally not claimed by this module: the exact SDK build,
trigger mode, pixel format, and CS505MU behavior must be characterized on Windows.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from types import ModuleType


class ThorlabsSDKUnavailable(RuntimeError):
    """The supported Windows runtime or vendor SDK is unavailable."""


@dataclass(frozen=True, slots=True)
class ThorlabsSDKStatus:
    platform_supported: bool
    sdk_importable: bool
    detail: str


def sdk_status() -> ThorlabsSDKStatus:
    """Probe availability without importing the SDK during normal simulator startup."""

    if sys.platform != "win32":
        return ThorlabsSDKStatus(
            platform_supported=False,
            sdk_importable=False,
            detail="physical CS505MU mode is supported only on a validated Windows host",
        )
    try:
        importlib.import_module("thorlabs_tsi_sdk.tl_camera")
    except (ImportError, OSError) as error:
        return ThorlabsSDKStatus(
            platform_supported=True,
            sdk_importable=False,
            detail=f"Thorlabs Scientific Imaging SDK could not be loaded: {error}",
        )
    return ThorlabsSDKStatus(
        platform_supported=True,
        sdk_importable=True,
        detail="vendor SDK import succeeded; physical camera is not yet bench-validated",
    )


def require_sdk() -> ModuleType:
    """Load the vendor camera module only when physical mode is explicitly requested."""

    status = sdk_status()
    if not status.platform_supported or not status.sdk_importable:
        raise ThorlabsSDKUnavailable(status.detail)
    return importlib.import_module("thorlabs_tsi_sdk.tl_camera")
