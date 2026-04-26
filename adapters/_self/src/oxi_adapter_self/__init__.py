"""Dogfood adapter for oxi.

Internal package. Drives oxi dispatching against its own repo for
the Phase 2 dogfooding loop. Not published to PyPI.

Public surface: `SelfAdapter`. Importing does NOT register the adapter
— callers must call `register_adapter()` explicitly.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .adapter import InferenceGatewayConfig, SelfAdapter

try:
    __version__ = version("oxi-adapter-self")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = ["InferenceGatewayConfig", "SelfAdapter", "__version__"]
