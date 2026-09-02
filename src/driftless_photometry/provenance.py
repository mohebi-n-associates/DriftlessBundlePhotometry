"""Runtime software and adapter provenance captured for every recording."""

from __future__ import annotations

import platform
import sys
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from driftless_photometry import __version__

_DEPENDENCIES = (
    "hdmf",
    "ndx-fiber-photometry",
    "ndx-ophys-devices",
    "numpy",
    "nwbinspector",
    "pydantic",
    "pynwb",
)

NWB_RUNTIME_PROVENANCE_SCRATCH_NAME = "driftless_runtime_provenance_json"


@dataclass(frozen=True, slots=True)
class RuntimeProvenance:
    """Versions that determine acquisition, parsing, and file-writing behavior."""

    application_version: str
    python_version: str
    operating_system: str
    adapter_name: str
    adapter_version: str
    protocol_version: str
    dependencies: tuple[tuple[str, str], ...]

    def to_document(self) -> dict[str, Any]:
        document = asdict(self)
        document["dependencies"] = dict(self.dependencies)
        return document

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> RuntimeProvenance:
        dependencies = document.get("dependencies")
        if not isinstance(dependencies, dict):
            raise ValueError("runtime provenance dependencies must be an object")
        return cls(
            application_version=str(document["application_version"]),
            python_version=str(document["python_version"]),
            operating_system=str(document["operating_system"]),
            adapter_name=str(document["adapter_name"]),
            adapter_version=str(document["adapter_version"]),
            protocol_version=str(document["protocol_version"]),
            dependencies=tuple(
                sorted((str(name), str(value)) for name, value in dependencies.items())
            ),
        )


def capture_runtime_provenance(
    *,
    adapter_name: str,
    adapter_version: str = "not supplied",
    protocol_version: str = "not supplied",
) -> RuntimeProvenance:
    """Capture deterministic package and platform versions for a recording."""

    dependencies: list[tuple[str, str]] = []
    for package in _DEPENDENCIES:
        try:
            package_version = version(package)
        except PackageNotFoundError:
            package_version = "not installed"
        dependencies.append((package, package_version))
    return RuntimeProvenance(
        application_version=__version__,
        python_version=platform.python_version(),
        operating_system=f"{platform.system()} {platform.release()} ({sys.platform})",
        adapter_name=adapter_name,
        adapter_version=adapter_version,
        protocol_version=protocol_version,
        dependencies=tuple(dependencies),
    )
