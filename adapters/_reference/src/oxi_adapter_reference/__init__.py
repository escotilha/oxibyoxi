"""Reference adapter for oxi.

Public surface: `ReferenceAdapter`. Importing this module does NOT
register the adapter — callers must call `register_adapter()`
explicitly. Keeping registration side-effect-free avoids surprises
when the package is pulled in as a transitive dependency.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .adapter import ReferenceAdapter

try:
    __version__ = version("oxi-adapter-reference")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = ["ReferenceAdapter", "__version__"]
