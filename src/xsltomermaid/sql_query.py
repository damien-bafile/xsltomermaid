"""Write a T-SQL ``SELECT`` that joins the diagram's tables on their foreign keys.

Pure Python (no Qt), like :mod:`excel_to_mermaid`. The input is the schema the
diagram shows: the selected tables, with the columns chosen in the Columns tab
in their chosen order, and the relationships between those tables.
"""

from __future__ import annotations

import re
from collections import deque

from .excel_to_mermaid import Relationship, Schema, Table

JOIN_TYPES = ("INNER", "LEFT")


def quote(name: str) -> str:
    """``[name]``, with ``]`` doubled, so any identifier is safe in T-SQL."""
    return "[" + name.replace("]", "]]") + "]"


def _table_ref(table: Table) -> str:
    if table.schema:
        return f"{quote(table.schema)}.{quote(table.name)}"
    return quote(table.name)


def _alias_base(name: str) -> str:
    """A short, readable alias: initials of the name's words, lowercased.

    ``OrderLine`` → ``ol``, ``order_line`` → ``ol``, ``Customer`` → ``c``.
    A single lowercase word (common in Dynamics: ``bookableresource``) has no
    word breaks to use, so it takes its first three letters instead.
    """
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", name)
    if len(words) > 1:
        base = "".join(w[0] for w in words)
    elif words:
        base = words[0][:3] if words[0].islower() and len(words[0]) > 6 else words[0][0]
    else:
        base = "t"
    base = re.sub(r"[^0-9A-Za-z_]", "", base).lower() or "t"
    return f"t{base}" if base[0].isdigit() else base


def _aliases(tables: list[Table]) -> dict[str, str]:
    """One unique alias per table (``c``, ``o``, ``ol``; ``o2`` on a clash)."""
    taken: set[str] = set()
    result: dict[str, str] = {}
    for table in tables:
        base = _alias_base(table.name)
        alias, n = base, 2
        while alias in taken:
            alias, n = f"{base}{n}", n + 1
        taken.add(alias)
        result[table.name.lower()] = alias
    return result


def _on_clause(rel: Relationship, aliases: dict[str, str]) -> str | None:
    """``[c].[x] = [p].[y] AND …``, or None when the columns aren't known."""
    if not rel.child_columns or len(rel.child_columns) != len(rel.parent_columns):
        return None
    child = aliases[rel.child_table.lower()]
    parent = aliases[rel.parent_table.lower()]
    return " AND ".join(
        f"{quote(child)}.{quote(c)} = {quote(parent)}.{quote(p)}"
        for c, p in zip(rel.child_columns, rel.parent_columns)
    )


def generate_select(
    schema: Schema,
    root: str | None = None,
    join: str = "INNER",
    top: int = 0,
) -> str:
    """A ``SELECT`` over ``schema.tables``, joined along ``schema.relationships``.

    ``root`` is the table in ``FROM`` (default: the first table). Joins follow a
    breadth-first walk of the foreign keys out from it, in either direction.
    Anything that can't become a clean join is written as a ``--`` comment so
    it's visible rather than silently dropped: a second foreign key between two
    tables already joined, a self-reference, unknown join columns, and tables
    with no foreign-key path to the root.
    """
    tables = list(schema.tables)
    if not tables:
        return "-- No tables selected.\n"
    join = join.upper()
    if join not in JOIN_TYPES:
        raise ValueError(f"join must be one of {JOIN_TYPES}, not {join!r}")

    by_name = {t.name.lower(): t for t in tables}
    root_table = by_name.get((root or "").lower(), tables[0])
    aliases = _aliases(tables)

    # Breadth-first from the root. Each relationship that first reaches a table
    # becomes its JOIN; any other relationship between joined tables is noted.
    joined = [root_table.name.lower()]
    seen = {root_table.name.lower()}
    tree: list[tuple[Relationship, str]] = []  # (relationship, table it brings in)
    used: set[int] = set()
    queue = deque([root_table.name.lower()])
    while queue:
        current = queue.popleft()
        for i, rel in enumerate(schema.relationships):
            parent, child = rel.parent_table.lower(), rel.child_table.lower()
            if parent == child or current not in (parent, child):
                continue
            other = child if current == parent else parent
            if other in seen or other not in by_name:
                continue
            seen.add(other)
            joined.append(other)
            tree.append((rel, other))
            used.add(i)
            queue.append(other)

    lines: list[str] = []
    notes: list[str] = []

    # SELECT list: every chosen column, in the Columns tab's order. A name
    # that appears in more than one table gets an "AS [Table_Column]" so the
    # result can feed a view or SELECT INTO (which reject duplicate names).
    picked = [(name, column.name) for name in joined for column in by_name[name].columns]
    counts: dict[str, int] = {}
    for _name, col in picked:
        counts[col.lower()] = counts.get(col.lower(), 0) + 1
    select_cols = [
        f"{quote(aliases[name])}.{quote(col)}"
        + (f" AS {quote(by_name[name].name + '_' + col)}" if counts[col.lower()] > 1 else "")
        for name, col in picked
    ]
    head = "SELECT" + (f" TOP ({int(top)})" if top and top > 0 else "")
    if select_cols:
        lines.append(head)
        lines.extend(
            f"    {col}{',' if i < len(select_cols) - 1 else ''}"
            for i, col in enumerate(select_cols)
        )
    else:
        lines.append(f"{head} *  -- every column was left out in the Columns tab")

    lines.append(f"FROM {_table_ref(root_table)} AS {quote(aliases[root_table.name.lower()])}")
    keyword = "INNER JOIN" if join == "INNER" else "LEFT JOIN"
    for rel, new in tree:
        table = by_name[new]
        lines.append(f"{keyword} {_table_ref(table)} AS {quote(aliases[new])}")
        on = _on_clause(rel, aliases)
        if on is None:
            lines.append(
                f"    ON 1 = 1  -- TODO: join columns unknown for "
                f"{rel.child_table}.{rel.label or '?'} → {rel.parent_table}"
            )
        else:
            lines.append(f"    ON {on}")

    # Everything that didn't become a JOIN, said out loud.
    for i, rel in enumerate(schema.relationships):
        if i in used:
            continue
        parent, child = rel.parent_table.lower(), rel.child_table.lower()
        if parent not in by_name or child not in by_name:
            continue
        on = _on_clause(rel, aliases)
        if parent == child:
            notes.append(
                f"{rel.child_table} references itself through {rel.label or '?'}; "
                "join it to a second alias of the table if you need that."
            )
        elif on is not None:
            notes.append(f"Also related: {on}  ({rel.child_table}.{rel.label})")
        else:
            notes.append(
                f"Also related: {rel.child_table}.{rel.label or '?'} → {rel.parent_table} "
                "(join columns unknown)"
            )
    missing = [t.name for t in tables if t.name.lower() not in seen]
    if missing:
        notes.append(
            "Not joined (no foreign-key path to "
            f"{root_table.name}): {', '.join(missing)}. Use “Trace path between "
            "tables” or “Add related tables” to bring in the tables that connect them."
        )

    text = "\n".join(lines) + ";\n"
    if notes:
        text += "\n" + "\n".join(f"-- {note}" for note in notes) + "\n"
    return text
