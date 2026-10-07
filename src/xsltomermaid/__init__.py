"""Excel schema to Mermaid diagram application."""

# Single source of truth for the version: pyproject.toml reads it from here
# (``[tool.setuptools.dynamic]``), and the app compares it against the latest
# GitHub release. Lives in code rather than package metadata so the PyInstaller
# exe, which doesn't bundle dist-info, still knows its own version.
__version__ = "0.12.0"
