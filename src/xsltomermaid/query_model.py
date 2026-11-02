"""A query designed in the Query view, and the T-SQL ``SELECT`` it stands for.

Pure Python (no Qt), like :mod:`sql_query`. Where the SQL view writes one
query joining every drawn table on its foreign keys, a :class:`QueryDesign` is
built by hand: the tables placed (a table can be placed twice, under two
aliases), the joins between them and their types, a tree of filter
conditions, and the output columns with their aggregates and sort order.
:func:`generate_designed_select` turns it into T-SQL.

The design refers to tables and columns by name, so it can be saved (see
:meth:`QueryDesign.to_dict`) and outlive the spreadsheet it was made from:
anything that no longer resolves is written as a ``--`` comment, never
silently dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Union

from .excel_to_mermaid import Column, Schema, Table
from .sql_query import ident, table_source, unique_alias

# ``left KIND JOIN right``: LEFT keeps every row of ``left``, RIGHT of ``right``.
JOIN_KINDS = ("INNER", "LEFT", "RIGHT", "FULL")
_JOIN_KEYWORDS = {"INNER": "INNER JOIN", "LEFT": "LEFT JOIN", "RIGHT": "RIGHT JOIN",
                  "FULL": "FULL OUTER JOIN"}
_FLIPPED = {"LEFT": "RIGHT", "RIGHT": "LEFT"}

# Filter operators. The value is typed text, written as a T-SQL literal that
# suits the column's data type.
OPERATORS = (
    "=", "<>", ">", ">=", "<", "<=",
    "contains", "does not contain", "starts with", "ends with",
    "in", "not in", "between", "is null", "is not null",
)
NO_VALUE_OPERATORS = frozenset({"is null", "is not null"})
LIST_OPERATORS = frozenset({"in", "not in"})

# Aggregates for an output column ("" = the value itself, grouped on when
# any other output column is aggregated).
AGGREGATES = ("", "COUNT", "COUNT DISTINCT", "SUM", "AVG", "MIN", "MAX")
SORTS = ("", "ASC", "DESC")

_NUMERIC_TYPES = frozenset({
    "int", "integer", "bigint", "smallint", "tinyint", "decimal", "numeric", "dec",
    "float", "real", "money", "smallmoney", "number", "double",
})
_BIT_TYPES = frozenset({"bit", "bool", "boolean", "twooptions"})
_UNICODE_TYPES = frozenset({"nvarchar", "nchar", "ntext", "string", "memo", ""})
_NUMBER = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")


# ---------------------------------------------------------------------------
# The design
# ---------------------------------------------------------------------------
@dataclass
class TableRef:
    """A table placed in the query. ``alias`` is unique within the design."""

    table: str  # Table.name in the schema
    alias: str


@dataclass
class Join:
    """``left KIND JOIN right ON left.a = right.b [AND …]`` (by alias)."""

    left: str
    right: str
    pairs: tuple[tuple[str, str], ...] = ()  # (left column, right column)
    kind: str = "INNER"


@dataclass
class Condition:
    alias: str
    column: str
    op: str = "="
    value: str = ""  # comma-separated for in / not in
    value2: str = ""  # the upper bound for between


@dataclass
class Group:
    """Conditions and sub-groups combined with AND or OR, optionally negated."""

    mode: str = "AND"  # "AND" | "OR"
    negate: bool = False
    items: list[Union["Condition", "Group"]] = field(default_factory=list)

    def conditions(self) -> list[Condition]:
        """Every condition in this group and the groups inside it."""
        found: list[Condition] = []
        for item in self.items:
            found += item.conditions() if isinstance(item, Group) else [item]
        return found

    def remove_alias(self, alias: str) -> None:
        """Drop the conditions on ``alias``, here and in every sub-group."""
        kept: list[Condition | Group] = []
        for item in self.items:
            if isinstance(item, Group):
                item.remove_alias(alias)
                kept.append(item)
            elif item.alias != alias:
                kept.append(item)
        self.items = kept


@dataclass
class OutputColumn:
    """A column of the result. ``alias`` empty means ``COUNT(*)``."""

    alias: str
    column: str = ""
    aggregate: str = ""
    sort: str = ""
    label: str = ""  # the AS name; a default is made up when needed

    @property
    def is_row_count(self) -> bool:
        return not self.alias


@dataclass
class QueryDesign:
    tables: list[TableRef] = field(default_factory=list)
    joins: list[Join] = field(default_factory=list)
    where: Group = field(default_factory=Group)
    outputs: list[OutputColumn] = field(default_factory=list)
    base: str = ""  # alias of the table in FROM; the first table when empty
    distinct: bool = False
    top: int = 0
    latest_only: bool = False
    active_only: bool = False

    # --- tables -----------------------------------------------------------

    def ref(self, alias: str) -> TableRef | None:
        return next((t for t in self.tables if t.alias == alias), None)

    def add_table(self, schema: Schema, name: str, auto_join: bool = True) -> TableRef:
        """Place table ``name`` under a new alias; with ``auto_join``, join it
        to the first table already placed that a foreign key links it to."""
        table = _lookup(schema, name)
        taken = {t.alias for t in self.tables}
        ref = TableRef(table.name if table else name, unique_alias(name, taken))
        self.tables.append(ref)
        if not self.base:
            self.base = ref.alias
        if auto_join:
            for other in self.tables[:-1]:
                candidates = candidate_joins(schema, other, ref)
                if candidates:
                    self.joins.append(candidates[0])
                    break
        return ref

    def remove_table(self, alias: str) -> None:
        """Remove a placed table and everything that refers to it."""
        self.tables = [t for t in self.tables if t.alias != alias]
        self.joins = [j for j in self.joins if alias not in (j.left, j.right)]
        self.outputs = [o for o in self.outputs if o.alias != alias]
        self.where.remove_alias(alias)
        if self.base == alias:
            self.base = self.tables[0].alias if self.tables else ""

    # --- saving -----------------------------------------------------------

    def to_dict(self) -> dict:
        """Plain values only (no None), so it can be written to TOML."""
        return {
            "tables": [{"table": t.table, "alias": t.alias} for t in self.tables],
            "joins": [
                {"left": j.left, "right": j.right, "kind": j.kind,
                 "pairs": [list(p) for p in j.pairs]}
                for j in self.joins
            ],
            "where": _group_to_dict(self.where),
            "outputs": [
                {"alias": o.alias, "column": o.column, "aggregate": o.aggregate,
                 "sort": o.sort, "label": o.label}
                for o in self.outputs
            ],
            "base": self.base,
            "distinct": self.distinct,
            "top": self.top,
            "latest_only": self.latest_only,
            "active_only": self.active_only,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "QueryDesign":
        """The inverse of :meth:`to_dict`; unknown or missing keys take their defaults."""
        design = cls(
            tables=[TableRef(str(t["table"]), str(t["alias"])) for t in data.get("tables", [])],
            joins=[
                Join(str(j["left"]), str(j["right"]),
                     tuple((str(a), str(b)) for a, b in j.get("pairs", [])),
                     str(j.get("kind", "INNER")).upper())
                for j in data.get("joins", [])
            ],
            where=_group_from_dict(data.get("where", {})),
            outputs=[
                OutputColumn(str(o.get("alias", "")), str(o.get("column", "")),
                             str(o.get("aggregate", "")), str(o.get("sort", "")),
                             str(o.get("label", "")))
                for o in data.get("outputs", [])
            ],
            base=str(data.get("base", "")),
            distinct=bool(data.get("distinct", False)),
            top=int(data.get("top", 0) or 0),
            latest_only=bool(data.get("latest_only", False)),
            active_only=bool(data.get("active_only", False)),
        )
        return design


def _group_to_dict(group: Group) -> dict:
    items = []
    for item in group.items:
        if isinstance(item, Group):
            items.append({"group": _group_to_dict(item)})
        else:
            items.append({"alias": item.alias, "column": item.column, "op": item.op,
                          "value": item.value, "value2": item.value2})
    return {"mode": group.mode, "negate": group.negate, "items": items}


def _group_from_dict(data: dict) -> Group:
    items: list[Condition | Group] = []
    for item in data.get("items", []):
        if "group" in item:
            items.append(_group_from_dict(item["group"]))
        else:
            items.append(Condition(str(item["alias"]), str(item["column"]),
                                   str(item.get("op", "=")), str(item.get("value", "")),
                                   str(item.get("value2", ""))))
    mode = str(data.get("mode", "AND")).upper()
    return Group("OR" if mode == "OR" else "AND", bool(data.get("negate", False)), items)


# ---------------------------------------------------------------------------
# Joins suggested by the schema's foreign keys
# ---------------------------------------------------------------------------
def _lookup(schema: Schema, name: str) -> Table | None:
    lowered = name.lower()
    return next((t for t in schema.tables if t.name.lower() == lowered), None)


def candidate_joins(schema: Schema, left: TableRef, right: TableRef) -> list[Join]:
    """The joins the foreign keys suggest between two placed tables, with
    ``left`` on the left of each. A foreign key from a table to itself joins
    two placements of that table (``e.ManagerId = m.Id``). Only keys whose
    columns are known are offered."""
    a, b = left.table.lower(), right.table.lower()
    joins: list[Join] = []
    for rel in schema.relationships:
        cols = rel.child_columns
        if not cols or len(cols) != len(rel.parent_columns):
            continue
        child, parent = rel.child_table.lower(), rel.parent_table.lower()
        if (child, parent) == (a, b):
            pairs = tuple(zip(rel.child_columns, rel.parent_columns))
        elif (child, parent) == (b, a):
            pairs = tuple(zip(rel.parent_columns, rel.child_columns))
        else:
            continue
        join = Join(left.alias, right.alias, pairs)
        if join not in joins:
            joins.append(join)
    return joins


# ---------------------------------------------------------------------------
# Literals
# ---------------------------------------------------------------------------
def _base_type(column: Column | None) -> str:
    return re.sub(r"\(.*", "", (column.data_type if column else "")).strip().lower()


def literal(value: str, column: Column | None) -> str:
    """``value`` as a T-SQL literal for ``column``'s type: a number for numeric
    types (when it is one), 1/0 for bit, ``N'…'`` for Unicode and unknown
    text types, ``'…'`` otherwise. Quotes are doubled."""
    text = value.strip()
    kind = _base_type(column)
    if kind in _NUMERIC_TYPES and _NUMBER.fullmatch(text):
        return text
    if kind in _BIT_TYPES and text.lower() in ("1", "0", "true", "false", "yes", "no"):
        return "1" if text.lower() in ("1", "true", "yes") else "0"
    if text.upper() == "NULL":
        return "NULL"
    escaped = "'" + text.replace("'", "''") + "'"
    return "N" + escaped if kind in _UNICODE_TYPES or kind.startswith("n") else escaped


def _like_pattern(value: str) -> str:
    """Escape LIKE's wildcards so ``value`` matches literally."""
    return value.replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")


# ---------------------------------------------------------------------------
# The SELECT
# ---------------------------------------------------------------------------
class _Writer:
    """State shared while writing one query."""

    def __init__(self, design: QueryDesign, schema: Schema, quote_all: bool):
        self.design, self.schema = design, schema
        self.quote_all = quote_all
        self.notes: list[str] = []
        self.tables: dict[str, Table] = {}  # alias -> Table, for placements that resolve
        for ref in design.tables:
            table = _lookup(schema, ref.table)
            if table is None:
                self.notes.append(f"{ref.table} ({ref.alias}) isn't in the spreadsheet; left out.")
            else:
                self.tables[ref.alias] = table

    def q(self, name: str) -> str:
        return ident(name, self.quote_all)

    def column(self, alias: str, name: str) -> Column | None:
        table = self.tables.get(alias)
        if table is None:
            return None
        lowered = name.lower()
        return next((c for c in table.columns if c.name.lower() == lowered), None)

    def ref(self, alias: str, column: str) -> str:
        return f"{self.q(alias)}.{self.q(column)}"

    # --- WHERE ------------------------------------------------------------

    def condition(self, cond: Condition, placed: set[str]) -> str | None:
        where = f"{cond.alias}.{cond.column}"
        if cond.alias not in placed:
            self.notes.append(f"Filter on {where} left out: {cond.alias} isn't joined.")
            return None
        col = self.column(cond.alias, cond.column)
        if col is None:
            self.notes.append(f"Filter on {where} left out: no such column.")
            return None
        ref, op, value = self.ref(cond.alias, cond.column), cond.op.lower(), cond.value.strip()
        if op not in OPERATORS:
            self.notes.append(f"Filter on {where} left out: unknown operator {cond.op!r}.")
            return None
        if op == "is null":
            return f"{ref} IS NULL"
        if op == "is not null":
            return f"{ref} IS NOT NULL"
        if not value or (op == "between" and not cond.value2.strip()):
            self.notes.append(f"Filter on {where} {cond.op} left out: it has no value yet.")
            return None
        if op in LIST_OPERATORS:
            items = [v for v in (part.strip() for part in value.split(",")) if v]
            keyword = "IN" if op == "in" else "NOT IN"
            return f"{ref} {keyword} ({', '.join(literal(v, col) for v in items)})"
        if op == "between":
            return f"{ref} BETWEEN {literal(value, col)} AND {literal(cond.value2, col)}"
        patterns = {"contains": "%{}%", "does not contain": "%{}%",
                    "starts with": "{}%", "ends with": "%{}"}
        if op in patterns:
            pattern = patterns[op].format(_like_pattern(value))
            text = literal(pattern, col if _base_type(col) not in _NUMERIC_TYPES else None)
            return f"{ref} {'NOT LIKE' if op == 'does not contain' else 'LIKE'} {text}"
        return f"{ref} {op} {literal(value, col)}"

    def group(self, group: Group, placed: set[str], depth: int = 0) -> str | None:
        parts = []
        for item in group.items:
            part = (self.group(item, placed, depth + 1) if isinstance(item, Group)
                    else self.condition(item, placed))
            if part:
                parts.append(part)
        if not parts:
            return None
        pad = "  " * (depth + 1)
        text = f"\n{pad}{group.mode} ".join(parts)
        if group.negate:
            return f"NOT ({text})"
        return f"({text})" if depth and len(parts) > 1 else text


def _join_order(design: QueryDesign, aliases: list[str]) -> tuple[str, list[tuple[str, str, list[Join]]]]:
    """The FROM alias, then each joined alias in order with its join keyword
    (relative to the tables already placed) and the joins making its ON."""
    base = design.base if design.base in aliases else aliases[0]
    placed = [base]
    steps: list[tuple[str, str, list[Join]]] = []
    progress = True
    while progress:
        progress = False
        for alias in aliases:
            if alias in placed:
                continue
            joins = [j for j in design.joins
                     if (j.right == alias and j.left in placed)
                     or (j.left == alias and j.right in placed)]
            if not joins:
                continue
            first = joins[0]
            kind = first.kind.upper() if first.kind.upper() in JOIN_KINDS else "INNER"
            if first.left == alias:  # written the other way round: mirror it
                kind = _FLIPPED.get(kind, kind)
            placed.append(alias)
            steps.append((alias, kind, joins))
            progress = True
    return base, steps


def generate_designed_select(design: QueryDesign, schema: Schema, quote_all: bool = False) -> str:
    """The T-SQL for ``design``, looked up against ``schema`` (the whole sheet).

    Joins follow the design from its base table; a table no join reaches is
    left out (rather than cross-joined) and named in a comment, as is anything
    else that couldn't be written: unknown tables or columns, filters without a
    value, joins whose columns are missing.
    """
    w = _Writer(design, schema, quote_all)
    q = w.q
    aliases = [t.alias for t in design.tables if t.alias in w.tables]
    if not aliases:
        text = "-- Add tables to the query.\n"
        return text + "".join(f"-- {n}\n" for n in w.notes)

    base, steps = _join_order(design, aliases)
    placed = {base, *(alias for alias, _kind, _joins in steps)}
    for alias in aliases:
        if alias not in placed:
            w.notes.append(
                f"Not joined: {w.tables[alias].name} ({alias}). Join it to a table "
                "in the query, or it's left out."
            )

    # SELECT list
    outputs = [o for o in design.outputs if o.is_row_count or o.alias in placed]
    for o in design.outputs:
        if not o.is_row_count and o.alias not in placed:
            w.notes.append(f"Output {o.alias}.{o.column} left out: {o.alias} isn't joined.")
    select: list[str] = []
    group_by: list[str] = []
    order_by: list[str] = []
    used: set[str] = set()

    def name_for(text: str) -> str:
        name, n = text, 2
        while name.lower() in used:
            name, n = f"{text}_{n}", n + 1
        used.add(name.lower())
        return name

    if outputs:
        aggregated = any(o.aggregate or o.is_row_count for o in outputs)
        for o in outputs:
            agg = "COUNT" if o.is_row_count else o.aggregate.upper()
            if not o.is_row_count and w.column(o.alias, o.column) is None:
                w.notes.append(f"Output {o.alias}.{o.column} left out: no such column.")
                continue
            expr = "*" if o.is_row_count else w.ref(o.alias, o.column)
            if agg:
                func = "COUNT(DISTINCT {})" if agg == "COUNT DISTINCT" else f"{agg}({{}})"
                default = ("row_count" if o.is_row_count
                           else f"{agg.lower().replace(' ', '_')}_{o.column}")
                label = name_for(o.label or default)
                select.append(f"{func.format(expr)} AS {q(label)}")
                sort_key = q(label)
            else:
                if aggregated:
                    group_by.append(expr)
                if o.label or o.column.lower() in used:
                    label = name_for(o.label or f"{o.alias}_{o.column}")
                    select.append(f"{expr} AS {q(label)}")
                    sort_key = q(label)
                else:
                    used.add(o.column.lower())
                    select.append(expr)
                    sort_key = expr
            if o.sort.upper() in ("ASC", "DESC"):
                order_by.append(f"{sort_key} {o.sort.upper()}")
    else:
        # Nothing chosen: every column of every joined table, a repeated name
        # made unique with its alias (a view or SELECT INTO needs that).
        ordered = [base] + [alias for alias, _k, _j in steps]
        counts: dict[str, int] = {}
        for alias in ordered:
            for c in w.tables[alias].columns:
                counts[c.name.lower()] = counts.get(c.name.lower(), 0) + 1
        for alias in ordered:
            for c in w.tables[alias].columns:
                expr = w.ref(alias, c.name)
                select.append(expr + (f" AS {q(alias + '_' + c.name)}"
                                      if counts[c.name.lower()] > 1 else ""))

    if not select:
        select = ["*"]
    head = "SELECT" + (" DISTINCT" if design.distinct else "")
    head += f" TOP ({int(design.top)})" if design.top and design.top > 0 else ""
    lines = [head] + [f"    {s}{',' if i < len(select) - 1 else ''}" for i, s in enumerate(select)]

    # FROM and JOINs, with the Dynamics row filters where they apply
    unranked: list[str] = []
    no_state: list[str] = []

    def source(alias: str) -> tuple[str, list[str]]:
        table = w.tables[alias]
        ref, conditions, missing = table_source(
            table, alias, q, table.columns, design.latest_only, design.active_only
        )
        if "key" in missing:
            unranked.append(table.name)
        if "state" in missing:
            no_state.append(table.name)
        return ref, conditions

    base_ref, row_filters = source(base)
    lines.append(f"FROM {base_ref} AS {q(base)}")
    for alias, kind, joins in steps:
        ref, conditions = source(alias)
        lines.append(f"{_JOIN_KEYWORDS[kind]} {ref} AS {q(alias)}")
        on: list[str] = []
        for j in joins:
            for left_col, right_col in j.pairs:
                ok = all(w.column(a, c) for a, c in ((j.left, left_col), (j.right, right_col)))
                if ok:
                    on.append(f"{w.ref(j.left, left_col)} = {w.ref(j.right, right_col)}")
                else:
                    w.notes.append(f"Join {j.left}.{left_col} = {j.right}.{right_col} "
                                   "left out: no such column.")
        on += conditions
        if on:
            lines.append("    ON " + "\n    AND ".join(on))
        else:
            lines.append(f"    ON 1 = 1  -- TODO: pick the columns that join {alias}")

    where = w.group(design.where, placed)
    if where and row_filters and "\n" in where:
        # Bracketed after the row filters, its continuation lines indented to match.
        where = "(" + where.replace("\n", "\n  ") + ")"
    clauses = row_filters + ([where] if where else [])
    if clauses:
        lines.append("WHERE " + "\n  AND ".join(clauses))
    if group_by:
        lines.append("GROUP BY " + ", ".join(group_by))
    if order_by:
        lines.append("ORDER BY " + ", ".join(order_by))

    if unranked:
        w.notes.append("Latest version only: no primary key to rank versions by, so all "
                       f"rows are kept for {', '.join(unranked)}.")
    if no_state:
        w.notes.append(f"Active records only: no statecode column in {', '.join(no_state)}.")

    text = "\n".join(lines) + ";\n"
    if w.notes:
        text += "\n" + "\n".join(f"-- {note}" for note in w.notes) + "\n"
    return text
