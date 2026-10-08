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


# SQL Server's reserved keywords: as names they must be bracketed.
_RESERVED = frozenset("""
ADD ALL ALTER AND ANY AS ASC AUTHORIZATION BACKUP BEGIN BETWEEN BREAK BROWSE BULK
BY CASCADE CASE CHECK CHECKPOINT CLOSE CLUSTERED COALESCE COLLATE COLUMN COMMIT
COMPUTE CONSTRAINT CONTAINS CONTAINSTABLE CONTINUE CONVERT CREATE CROSS CURRENT
CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER CURSOR DATABASE DBCC
DEALLOCATE DECLARE DEFAULT DELETE DENY DESC DISK DISTINCT DISTRIBUTED DOUBLE DROP
DUMP ELSE END ERRLVL ESCAPE EXCEPT EXEC EXECUTE EXISTS EXIT EXTERNAL FETCH FILE
FILLFACTOR FOR FOREIGN FREETEXT FREETEXTTABLE FROM FULL FUNCTION GOTO GRANT GROUP
HAVING HOLDLOCK IDENTITY IDENTITY_INSERT IDENTITYCOL IF IN INDEX INNER INSERT
INTERSECT INTO IS JOIN KEY KILL LEFT LIKE LINENO LOAD MERGE NATIONAL NOCHECK
NONCLUSTERED NOT NULL NULLIF OF OFF OFFSETS ON OPEN OPENDATASOURCE OPENQUERY
OPENROWSET OPENXML OPTION OR ORDER OUTER OVER PERCENT PIVOT PLAN PRECISION PRIMARY
PRINT PROC PROCEDURE PUBLIC RAISERROR READ READTEXT RECONFIGURE REFERENCES
REPLICATION RESTORE RESTRICT RETURN REVERT REVOKE RIGHT ROLLBACK ROWCOUNT
ROWGUIDCOL RULE SAVE SCHEMA SECURITYAUDIT SELECT SEMANTICKEYPHRASETABLE
SEMANTICSIMILARITYDETAILSTABLE SEMANTICSIMILARITYTABLE SESSION_USER SET SETUSER
SHUTDOWN SOME STATISTICS SYSTEM_USER TABLE TABLESAMPLE TEXTSIZE THEN TO TOP TRAN
TRANSACTION TRIGGER TRUNCATE TRY_CONVERT TSEQUAL UNION UNIQUE UNPIVOT UPDATE
UPDATETEXT USE USER VALUES VARYING VIEW WAITFOR WHEN WHERE WHILE WITH
WITHIN WRITETEXT
""".split())

# A regular T-SQL identifier: a letter or _ first, then letters, digits, _, @,
# # or $. (A leading @ or # would mean a variable or temp table: bracket it.)
_REGULAR = re.compile(r"[A-Za-z_][A-Za-z0-9_@#$]*")


def ident(name: str, quote_all: bool = False) -> str:
    """``name`` as T-SQL needs it: bare when that's safe, else ``[name]``.

    Brackets go on reserved words (``Order``, ``User``), names with spaces or
    symbols, and names starting with a digit; ``quote_all`` brackets every
    name, for a query that runs whatever the names are.
    """
    if quote_all or not _REGULAR.fullmatch(name) or name.upper() in _RESERVED:
        return quote(name)
    return name


def _table_ref(table: Table, q=quote) -> str:
    if table.schema:
        return f"{q(table.schema)}.{q(table.plain_name)}"
    return q(table.plain_name)


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


def _on_clause(rel: Relationship, aliases: dict[str, str], q=quote) -> str | None:
    """``c.x = p.y AND …``, or None when the columns aren't known."""
    if not rel.child_columns or len(rel.child_columns) != len(rel.parent_columns):
        return None
    child = aliases[rel.child_table.lower()]
    parent = aliases[rel.parent_table.lower()]
    return " AND ".join(
        f"{q(child)}.{q(c)} = {q(parent)}.{q(p)}"
        for c, p in zip(rel.child_columns, rel.parent_columns)
    )


def generate_select(
    schema: Schema,
    root: str | None = None,
    join: str = "INNER",
    top: int = 0,
    quote_all: bool = False,
    latest_only: bool = False,
    active_only: bool = False,
    full_schema: Schema | None = None,
) -> str:
    """A ``SELECT`` over ``schema.tables``, joined along ``schema.relationships``.

    ``root`` is the table in ``FROM`` (default: the first table). Joins follow a
    breadth-first walk of the foreign keys out from it, in either direction.
    Anything that can't become a clean join is written as a ``--`` comment so
    it's visible rather than silently dropped: a second foreign key between two
    tables already joined, a self-reference, unknown join columns, and tables
    with no foreign-key path to the root.

    Names are bracketed only where T-SQL needs it (see :func:`ident`);
    ``quote_all`` brackets every name.

    For Dynamics data:

    - ``latest_only`` is for an append-only copy (Azure Synapse Link, Fabric),
      where every change adds a row: each table becomes a subquery ranking its
      rows per primary key by ``versionnumber`` (newest first) without the
      ``IsDelete`` rows, and the joins keep rank 1. Those copies add both
      columns to every table, so they needn't be in the spreadsheet.
    - ``active_only`` keeps ``statecode = 0`` for every table that has one.

    Both go in each join's ``ON`` (so LEFT JOINs keep their meaning) and, for
    the ``FROM`` table, in ``WHERE``. ``full_schema`` (the whole sheet) is
    where columns are looked up, as ``schema`` may have had some left out.
    """

    def q(name: str) -> str:
        return ident(name, quote_all)

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
    # that appears in more than one table gets an "AS Table_Column" so the
    # result can feed a view or SELECT INTO (which reject duplicate names).
    picked = [(name, column.name) for name in joined for column in by_name[name].columns]
    counts: dict[str, int] = {}
    for _name, col in picked:
        counts[col.lower()] = counts.get(col.lower(), 0) + 1
    select_cols = [
        f"{q(aliases[name])}.{q(col)}"
        + (f" AS {q(by_name[name].name + '_' + col)}" if counts[col.lower()] > 1 else "")
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

    full = {t.name.lower(): t for t in (full_schema.tables if full_schema else tables)}
    unranked: list[str] = []  # no primary key to rank by
    no_state: list[str] = []  # no statecode column

    def source(table: Table, alias: str) -> tuple[str, list[str]]:
        """The table (or its latest-version subquery) and its row filters."""
        ref, conditions = _table_ref(table, q), []
        columns = full.get(table.name.lower(), table).columns
        if latest_only:
            keys = [c.name for c in columns if c.is_primary_key]
            if keys:
                ref = (
                    "(\n"
                    f"    SELECT *, ROW_NUMBER() OVER (PARTITION BY "
                    f"{', '.join(q(k) for k in keys)} ORDER BY versionnumber DESC)"
                    " AS _version_rank\n"
                    f"    FROM {ref}\n"
                    "    WHERE ISNULL(IsDelete, 0) = 0\n"
                    ")"
                )
                conditions.append(f"{q(alias)}._version_rank = 1")
            else:
                unranked.append(table.name)
        if active_only:
            state = next((c.name for c in columns if c.name.lower() == "statecode"), None)
            if state:
                conditions.append(f"{q(alias)}.{q(state)} = 0")
            else:
                no_state.append(table.name)
        return ref, conditions

    root_alias = aliases[root_table.name.lower()]
    root_ref, where = source(root_table, root_alias)
    lines.append(f"FROM {root_ref} AS {q(root_alias)}")
    keyword = "INNER JOIN" if join == "INNER" else "LEFT JOIN"
    for rel, new in tree:
        table = by_name[new]
        ref, conditions = source(table, aliases[new])
        lines.append(f"{keyword} {ref} AS {q(aliases[new])}")
        on = _on_clause(rel, aliases, q)
        extra = "".join(f" AND {c}" for c in conditions)
        if on is None:
            lines.append(
                f"    ON 1 = 1{extra}  -- TODO: join columns unknown for "
                f"{rel.child_table}.{rel.label or '?'} → {rel.parent_table}"
            )
        else:
            lines.append(f"    ON {on}{extra}")
    if where:
        lines.append("WHERE " + "\n  AND ".join(where))
    if unranked:
        notes.append(
            "Latest version only: no primary key to rank versions by, so all rows "
            f"are kept for {', '.join(unranked)}."
        )
    if no_state:
        notes.append(f"Active records only: no statecode column in {', '.join(no_state)}.")

    # Everything that didn't become a JOIN, said out loud.
    for i, rel in enumerate(schema.relationships):
        if i in used:
            continue
        parent, child = rel.parent_table.lower(), rel.child_table.lower()
        if parent not in by_name or child not in by_name:
            continue
        on = _on_clause(rel, aliases, q)
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
            f"{root_table.name}): {', '.join(missing)}. Use “Grow selection” in the "
            "table list (Add related tables, or trace a path) to bring in the tables "
            "that connect them."
        )

    text = "\n".join(lines) + ";\n"
    if notes:
        text += "\n" + "\n".join(f"-- {note}" for note in notes) + "\n"
    return text
