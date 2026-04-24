"""oxi-core — the engine package.

Project-agnostic. All project specifics live in adapters.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("oxi-core")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
