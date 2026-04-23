"""Reference adapter for oxi.

Public surface: `ReferenceAdapter`. Importing this module does NOT
register the adapter — callers must call `register_adapter()`
explicitly. Keeping registration side-effect-free avoids surprises
when the package is pulled in as a transitive dependency.
"""

from __future__ import annotations

from .adapter import ReferenceAdapter

__version__ = "0.0.0"
__all__ = ["ReferenceAdapter", "__version__"]
