"""MCP server for Kener v4 status pages."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("kener-mcp")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.0.0"
