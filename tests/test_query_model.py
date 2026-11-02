"""Tests for the Query view's model and its T-SQL (no Qt needed)."""

import pytest

from xsltomermaid.excel_to_mermaid import Column, Relationship, Schema, Table, build_schema
from xsltomermaid.query_model import (
    Condition,
    Group,
    Join,
    OutputColumn,
    QueryDesign,
    candidate_joins,
    generate_designed_select,
    literal,
)

from test_excel_to_mermaid import COMPOSITE_ROWS, SAMPLE_ROWS


def _customer_orders():
    schema = build_schema(SAMPLE_ROWS)
    design = QueryDesign()
    design.add_table(schema, "Customer")
    design.add_table(schema, "Order")
    return schema, design


def test_adding_tables_joins_them_on_their_foreign_key():
    schema, design = _customer_orders()
    assert [(t.table, t.alias) for t in design.tables] == [("Customer", "c"), ("Order", "o")]
    assert design.joins == [Join("c", "o", (("CustomerID", "CustomerID"),), "INNER")]
    assert design.base == "c"
    sql = generate_designed_select(design, schema)
    assert "FROM dbo.Customer AS c\nINNER JOIN dbo.[Order] AS o\n    ON c.CustomerID = o.CustomerID" in sql
    # With no output columns chosen, every column, repeated names made unique.
    assert "c.CustomerID AS c_CustomerID" in sql and "o.CustomerID AS o_CustomerID" in sql
    assert sql.rstrip().endswith(";")


def test_auto_join_can_be_turned_off_and_unjoined_tables_are_left_out():
    schema = build_schema(SAMPLE_ROWS)
    design = QueryDesign()
    design.add_table(schema, "Customer")
    design.add_table(schema, "Order", auto_join=False)
    assert design.joins == []
    sql = generate_designed_select(design, schema)
    assert "[Order]" not in sql.split(";")[0]  # never cross-joined
    assert "-- Not joined: Order (o)." in sql


def test_composite_keys_join_on_every_column():
    schema = build_schema(COMPOSITE_ROWS)
    design = QueryDesign()
    design.add_table(schema, "Shipment")
    design.add_table(schema, "OrderLine")
    # Shipment has two composite keys to OrderLine: both are offered, the first is used.
    s, ol = design.tables
    assert len(candidate_joins(schema, s, ol)) == 2
    sql = generate_designed_select(design, schema)
    assert "    ON s.OrderID = ol.OrderID\n    AND s.[LineNo] = ol.[LineNo]" in sql


def test_every_join_type_and_mirroring_from_the_base():
    schema, design = _customer_orders()
    for kind, keyword in (("INNER", "INNER JOIN"), ("LEFT", "LEFT JOIN"),
                          ("RIGHT", "RIGHT JOIN"), ("FULL", "FULL OUTER JOIN")):
        design.joins[0].kind = kind
        assert f"\n{keyword} dbo.[Order] AS o\n" in generate_designed_select(design, schema)
    # "c LEFT JOIN o" keeps every customer; starting FROM o, that's a RIGHT JOIN.
    design.joins[0].kind = "LEFT"
    design.base = "o"
    sql = generate_designed_select(design, schema)
    assert "FROM dbo.[Order] AS o\nRIGHT JOIN dbo.Customer AS c" in sql


def test_a_self_reference_joins_two_placements_of_a_table():
    schema = Schema(
        tables=[Table("", "Emp", [Column("", "Emp", 1, "Id", "int", is_primary_key=True),
                                  Column("", "Emp", 2, "ManagerId", "int")])],
        relationships=[Relationship("Emp", "Emp", "ManagerId", ("ManagerId",), ("Id",))],
    )
    design = QueryDesign()
    design.add_table(schema, "Emp")
    manager = design.add_table(schema, "Emp")
    assert manager.alias == "e2"
    design.joins[0].kind = "LEFT"
    design.outputs = [OutputColumn("e", "Id"), OutputColumn("e2", "Id", label="ManagerId")]
    sql = generate_designed_select(design, schema)
    assert "FROM Emp AS e\nLEFT JOIN Emp AS e2\n    ON e.ManagerId = e2.Id" in sql
    assert "    e.Id,\n    e2.Id AS ManagerId" in sql


def test_nested_filters_with_typed_literals():
    schema, design = _customer_orders()
    design.where = Group("AND", False, [
        Condition("c", "Name", "starts with", "A_b%"),
        Group("OR", False, [
            Condition("o", "Total", ">", "100"),
            Condition("o", "Total", "is null"),
            Group("AND", True, [Condition("c", "Name", "in", "x, O'Brien")]),
        ]),
    ])
    sql = generate_designed_select(design, schema)
    assert (
        "WHERE c.Name LIKE 'A[_]b[%]%'\n"
        "  AND (o.Total > 100\n"
        "    OR o.Total IS NULL\n"
        "    OR NOT (c.Name IN ('x', 'O''Brien')))"
    ) in sql


def test_literals_follow_the_column_type():
    def col(data_type):
        return Column("", "T", 1, "x", data_type)

    assert literal("42", col("int")) == "42" and literal("1.5e3", col("decimal(18,2)")) == "1.5e3"
    assert literal("4x", col("int")) == "'4x'"  # not a number: quoted, SQL Server will say
    assert literal("true", col("bit")) == "1" and literal("No", col("bit")) == "0"
    assert literal("Zoë", col("nvarchar")) == "N'Zoë'" and literal("a", col("varchar")) == "'a'"
    assert literal("a", None) == "N'a'"  # unknown type: Unicode is the safe choice
    assert literal("2026-01-31", col("datetime")) == "'2026-01-31'"
    assert literal("null", col("int")) == "NULL"


def test_every_operator():
    schema, design = _customer_orders()
    cases = {
        ("=", "5", ""): "o.Total = 5", ("<>", "5", ""): "o.Total <> 5",
        (">=", "5", ""): "o.Total >= 5", ("<", "5", ""): "o.Total < 5",
        ("between", "1", "9"): "o.Total BETWEEN 1 AND 9",
        ("not in", "1,2", ""): "o.Total NOT IN (1, 2)",
        ("is not null", "", ""): "o.Total IS NOT NULL",
        ("contains", "5", ""): "o.Total LIKE N'%5%'",
        ("does not contain", "5", ""): "o.Total NOT LIKE N'%5%'",
        ("ends with", "5", ""): "o.Total LIKE N'%5'",
    }
    for (op, value, value2), expected in cases.items():
        design.where = Group(items=[Condition("o", "Total", op, value, value2)])
        assert f"WHERE {expected}" in generate_designed_select(design, schema), op


def test_incomplete_or_stale_filters_are_noted_not_guessed():
    schema, design = _customer_orders()
    design.where = Group(items=[
        Condition("c", "Name", "=", ""),
        Condition("o", "Missing", "=", "1"),
        Condition("zz", "Name", "=", "x"),
        Condition("o", "Total", "between", "1", ""),
    ])
    sql = generate_designed_select(design, schema)
    assert "WHERE" not in sql
    assert "-- Filter on c.Name = left out: it has no value yet." in sql
    assert "-- Filter on o.Missing left out: no such column." in sql
    assert "-- Filter on zz.Name left out: zz isn't joined." in sql
    assert "-- Filter on o.Total between left out: it has no value yet." in sql


def test_aggregates_group_sort_top_and_distinct():
    schema, design = _customer_orders()
    design.outputs = [
        OutputColumn("c", "Name", sort="ASC"),
        OutputColumn("", aggregate="COUNT", sort="DESC"),
        OutputColumn("o", "Total", "SUM"),
        OutputColumn("o", "OrderID", "COUNT DISTINCT"),
    ]
    design.top, design.distinct = 10, True
    sql = generate_designed_select(design, schema)
    assert sql.startswith(
        "SELECT DISTINCT TOP (10)\n"
        "    c.Name,\n"
        "    COUNT(*) AS row_count,\n"
        "    SUM(o.Total) AS sum_Total,\n"
        "    COUNT(DISTINCT o.OrderID) AS count_distinct_OrderID\n"
    )
    assert "GROUP BY c.Name\nORDER BY c.Name ASC, row_count DESC;" in sql


def test_output_labels_are_unique_and_bracketed_when_needed():
    schema, design = _customer_orders()
    design.outputs = [
        OutputColumn("c", "CustomerID"),
        OutputColumn("o", "CustomerID"),
        OutputColumn("o", "Total", label="Order Total"),
    ]
    sql = generate_designed_select(design, schema)
    assert "    c.CustomerID,\n    o.CustomerID AS o_CustomerID,\n    o.Total AS [Order Total]" in sql


def test_dynamics_row_filters_combine_with_the_designed_where():
    schema = Schema(
        tables=[
            Table("dbo", "account", [
                Column("dbo", "account", 1, "accountid", "uniqueidentifier", is_primary_key=True),
                Column("dbo", "account", 2, "name", "nvarchar"),
                Column("dbo", "account", 3, "statecode", "int"),
            ]),
            Table("dbo", "contact", [
                Column("dbo", "contact", 1, "contactid", "uniqueidentifier", is_primary_key=True),
                Column("dbo", "contact", 2, "parentcustomerid", "uniqueidentifier"),
            ]),
        ],
        relationships=[Relationship("account", "contact", "parentcustomerid",
                                    ("parentcustomerid",), ("accountid",))],
    )
    design = QueryDesign(latest_only=True, active_only=True)
    design.add_table(schema, "account")
    design.add_table(schema, "contact")
    design.joins[0].kind = "LEFT"
    design.where = Group("OR", False, [Condition("acc", "name", "=", "A"),
                                       Condition("acc", "name", "=", "B")])
    sql = generate_designed_select(design, schema)
    assert "    ON acc.accountid = con.parentcustomerid\n    AND con._version_rank = 1" in sql
    assert ("WHERE acc._version_rank = 1\n  AND acc.statecode = 0\n"
            "  AND (acc.name = N'A'\n    OR acc.name = N'B')") in sql
    assert "-- Active records only: no statecode column in contact." in sql


def test_removing_a_table_removes_what_refers_to_it():
    schema, design = _customer_orders()
    design.outputs = [OutputColumn("c", "Name"), OutputColumn("o", "Total")]
    design.where = Group(items=[Condition("o", "Total", ">", "1"),
                                Group(items=[Condition("o", "OrderID", "=", "2"),
                                             Condition("c", "Name", "=", "x")])])
    design.remove_table("o")
    assert design.joins == [] and [o.alias for o in design.outputs] == ["c"]
    assert [(c.alias, c.column) for c in design.where.conditions()] == [("c", "Name")]
    design.remove_table("c")
    assert design.base == "" and generate_designed_select(design, schema) == "-- Add tables to the query.\n"


def test_tables_missing_from_the_spreadsheet_are_noted():
    schema, design = _customer_orders()
    design.tables.append(design.tables[0].__class__("Gone", "g"))
    sql = generate_designed_select(design, schema)
    assert "-- Gone (g) isn't in the spreadsheet; left out." in sql


def test_a_design_round_trips_through_plain_values():
    schema, design = _customer_orders()
    design.joins[0].kind = "FULL"
    design.where = Group("OR", True, [Condition("c", "Name", "contains", "x"),
                                      Group(items=[Condition("o", "Total", "between", "1", "2")])])
    design.outputs = [OutputColumn("", aggregate="COUNT", sort="DESC", label="n")]
    design.top, design.latest_only = 5, True
    data = design.to_dict()

    def plain(value):  # TOML has no None
        if isinstance(value, dict):
            return all(plain(v) for v in value.values())
        if isinstance(value, list):
            return all(plain(v) for v in value)
        return value is not None

    assert plain(data)
    again = QueryDesign.from_dict(data)
    assert again == design
    assert generate_designed_select(again, schema) == generate_designed_select(design, schema)


def test_quote_all_brackets_every_name():
    schema, design = _customer_orders()
    sql = generate_designed_select(design, schema, quote_all=True)
    assert "FROM [dbo].[Customer] AS [c]\nINNER JOIN [dbo].[Order] AS [o]" in sql


def test_output_parses_as_tsql():
    sqlglot = pytest.importorskip("sqlglot")
    schema, design = _customer_orders()
    designs = []
    for kind in ("INNER", "LEFT", "RIGHT", "FULL"):
        design.joins[0].kind = kind
        designs.append(QueryDesign.from_dict(design.to_dict()))
    design.where = Group("AND", False, [
        Condition("c", "Name", "contains", "a'b"),
        Group("OR", True, [Condition("o", "Total", "between", "1", "2"),
                           Condition("o", "OrderID", "in", "1, 2, 3")]),
    ])
    design.outputs = [OutputColumn("c", "Name", sort="ASC"),
                      OutputColumn("", aggregate="COUNT", sort="DESC"),
                      OutputColumn("o", "Total", "AVG", label="Average Total")]
    design.top, design.distinct, design.latest_only = 20, True, True
    designs.append(design)
    for d in designs:
        sql = generate_designed_select(d, schema)
        assert sqlglot.parse_one(sql.split(";")[0], read="tsql") is not None, sql
