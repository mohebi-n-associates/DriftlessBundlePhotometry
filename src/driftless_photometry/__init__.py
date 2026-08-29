"""Driftless Bundle Photometry acquisition package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("driftless-bundle-photometry")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0+unknown"

__all__ = ["__version__"]
