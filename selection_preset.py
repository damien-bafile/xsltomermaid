"""Save/load table-selection presets as TOML."""

from __future__ import annotations

import json

try:  # Python 3.11+
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None


def dump_selection_toml(filename: str, selected_tables: list[str]) -> str:
    """Return TOML text for a table-selection preset."""
    if not isinstance(filename, str):
        raise ValueError("'filename' must be a string in the TOML preset.")
    if not isinstance(selected_tables, list) or not all(
        isinstance(name, str) for name in selected_tables
    ):
        raise ValueError("'selected_tables' must be a list of strings.")
    unique = _normalise_selected_tables(selected_tables)
    return (
        "# xsltomermaid table selection preset\n"
        f"filename = {json.dumps(filename or '')}\n"
        f"selected_tables = {json.dumps(unique)}\n"
    )


def load_selection_toml(text: str) -> tuple[str, list[str]]:
    """Parse filename + selected table list from TOML text."""
    data = _load_toml(text)
    filename = data.get("filename", "")
    selected = data.get("selected_tables", [])
    if not isinstance(filename, str):
        raise ValueError("'filename' must be a string in the TOML preset.")
    if not isinstance(selected, list) or not all(isinstance(v, str) for v in selected):
        raise ValueError("'selected_tables' must be an array of strings in the TOML preset.")
    return filename.strip(), _normalise_selected_tables(selected)


def _load_toml(text: str) -> dict:
    invalid_message = "Invalid TOML preset content."
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except Exception as exc:
            if exc.__class__.__name__ == "TOMLDecodeError":
                raise ValueError(invalid_message) from exc
            raise
        if not isinstance(data, dict):
            raise ValueError(invalid_message)
        return data

    # Lightweight fallback for Python 3.10: supports the exact format we write.
    data: dict[str, object] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(invalid_message)
        key, rhs = line.split("=", 1)
        key = key.strip()
        rhs = rhs.strip()
        if key not in {"filename", "selected_tables"}:
            raise ValueError(invalid_message)
        try:
            data[key] = json.loads(rhs)
        except json.JSONDecodeError as exc:
            raise ValueError(invalid_message) from exc
    return data


def _normalise_selected_tables(names) -> list[str]:
    cleaned = [str(name).strip() for name in names if str(name).strip()]
    unique: list[str] = []
    seen: set[str] = set()
    for name in cleaned:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(name)
    return unique
