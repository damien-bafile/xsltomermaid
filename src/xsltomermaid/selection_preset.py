"""Save/load table-selection presets as TOML.

A preset holds the ticked tables and, since 1.3, the column choices (which
columns are left out, each table's dragged order, the shared sort) and the
moved relationship labels. Every value is written as one line of JSON that
is also valid TOML, so the Python 3.10 fallback reader handles it too. A
preset without the newer keys leaves those choices as they are.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

try:  # Python 3.11+
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None


@dataclass
class Preset:
    """What a preset file holds. ``None`` means the file didn't say."""

    filename: str = ""
    selected_tables: list[str] = field(default_factory=list)
    hidden_columns: set[tuple[str, str]] | None = None  # (table, column), lowercase
    column_order: dict[str, list[str]] | None = None  # table -> dragged column order
    column_sort: str | None = None  # "order", "name", "name_desc" or "type"
    keys_first: bool | None = None
    label_layout: dict[str, list[float]] | None = None  # label key -> [angle, dx, dy]


def dump_preset(preset: Preset) -> str:
    """Return TOML text for ``preset`` (keys left as None aren't written)."""
    if not isinstance(preset.filename, str):
        raise ValueError("'filename' must be a string in the TOML preset.")
    if not isinstance(preset.selected_tables, list) or not all(
        isinstance(name, str) for name in preset.selected_tables
    ):
        raise ValueError("'selected_tables' must be a list of strings.")
    lines = [
        "# xsltomermaid table selection preset",
        f"filename = {json.dumps(preset.filename or '')}",
        f"selected_tables = {json.dumps(_normalise_selected_tables(preset.selected_tables))}",
    ]
    if preset.hidden_columns is not None:
        pairs = sorted([t, c] for t, c in preset.hidden_columns)
        lines.append(f"hidden_columns = {json.dumps(pairs)}")
    if preset.column_order is not None:
        rows = [[t, list(cols)] for t, cols in sorted(preset.column_order.items())]
        lines.append(f"column_order = {json.dumps(rows)}")
    if preset.column_sort is not None:
        lines.append(f"column_sort = {json.dumps(preset.column_sort)}")
    if preset.keys_first is not None:
        lines.append(f"keys_first = {json.dumps(bool(preset.keys_first))}")
    if preset.label_layout is not None:
        rows = [[k, *(float(x) for x in v)] for k, v in sorted(preset.label_layout.items())]
        lines.append(f"label_layout = {json.dumps(rows)}")
    return "\n".join(lines) + "\n"


def load_preset(text: str) -> Preset:
    """Parse a preset; raises ValueError naming the key that's wrong."""
    data = _load_toml(text)
    filename = data.get("filename", "")
    selected = data.get("selected_tables", [])
    if not isinstance(filename, str):
        raise ValueError("'filename' must be a string in the TOML preset.")
    if not isinstance(selected, list) or not all(isinstance(v, str) for v in selected):
        raise ValueError("'selected_tables' must be an array of strings in the TOML preset.")
    preset = Preset(filename.strip(), _normalise_selected_tables(selected))
    if "hidden_columns" in data:
        pairs = data["hidden_columns"]
        if not isinstance(pairs, list) or not all(_strings(p, 2) for p in pairs):
            raise ValueError("'hidden_columns' must be [table, column] pairs in the TOML preset.")
        preset.hidden_columns = {(t.lower(), c.lower()) for t, c in pairs}
    if "column_order" in data:
        rows = data["column_order"]
        if not isinstance(rows, list) or not all(
            isinstance(r, list) and len(r) == 2 and isinstance(r[0], str) and _strings(r[1])
            for r in rows
        ):
            raise ValueError("'column_order' must be [table, [columns]] pairs in the TOML preset.")
        preset.column_order = {t.lower(): [c.lower() for c in cols] for t, cols in rows}
    if "column_sort" in data:
        if data["column_sort"] not in ("order", "name", "name_desc", "type"):
            raise ValueError(
                "'column_sort' must be order, name, name_desc or type in the TOML preset."
            )
        preset.column_sort = data["column_sort"]
    if "keys_first" in data:
        if not isinstance(data["keys_first"], bool):
            raise ValueError("'keys_first' must be true or false in the TOML preset.")
        preset.keys_first = data["keys_first"]
    if "label_layout" in data:
        rows = data["label_layout"]
        if not isinstance(rows, list) or not all(
            isinstance(r, list) and len(r) == 4 and isinstance(r[0], str)
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in r[1:])
            for r in rows
        ):
            raise ValueError("'label_layout' must be [label, angle, dx, dy] rows in the TOML preset.")
        preset.label_layout = {r[0]: [float(v) for v in r[1:]] for r in rows}
    return preset


def _strings(value, length: int | None = None) -> bool:
    return (
        isinstance(value, list)
        and (length is None or len(value) == length)
        and all(isinstance(v, str) for v in value)
    )


def dump_selection_toml(filename: str, selected_tables: list[str]) -> str:
    """Return TOML text for a preset of just the ticked tables."""
    return dump_preset(Preset(filename, selected_tables))


def load_selection_toml(text: str) -> tuple[str, list[str]]:
    """Parse filename + selected table list from TOML text."""
    preset = load_preset(text)
    return preset.filename, preset.selected_tables


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
        try:
            data[key] = json.loads(rhs)
        except json.JSONDecodeError as exc:
            raise ValueError(invalid_message) from exc
    return data


def _normalise_selected_tables(names) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for raw in names:
        if not isinstance(raw, str):
            raise ValueError("'selected_tables' must be an array of strings in the TOML preset.")
        name = raw.strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(name)
    return unique
