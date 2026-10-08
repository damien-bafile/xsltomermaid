"""Tests for the T-SQL SELECT generator (no Qt needed)."""

import pytest

from xsltomermaid.excel_to_mermaid import (
    Column,
    Relationship,
    Schema,
    Table,
    build_schema,
    filter_columns,
)
from xsltomermaid.sql_query import generate_select, ident, quote

from test_excel_to_mermaid import COMPOSITE_ROWS, SAMPLE_ROWS


def test_relationships_record_join_columns():
    rels = {(r.child_table, r.label): r for r in build_schema(COMPOSITE_ROWS).relationships}
    composite = rels[("Shipment", "OrderID, LineNo")]
    assert composite.child_columns == ("OrderID", "LineNo")
    assert composite.parent_columns == ("OrderID", "LineNo")
    assert rels[("Audit", "CreatedBy")].parent_columns == ("OrderID",)


def test_bare_table_reference_joins_on_a_single_primary_key():
    rows = [
        {"TableName": "Customer", "ColumnName": "CustomerID", "IsPrimaryKey": 1},
        {"TableName": "Order", "ColumnName": "OrderID", "IsPrimaryKey": 1},
        {"TableName": "Order", "ColumnName": "Cust", "ForeignKeyReference": "Customer"},
    ]
    (rel,) = build_schema(rows).relationships
    assert rel.child_columns == ("Cust",) and rel.parent_columns == ("CustomerID",)


def test_simple_join():
    sql = generate_select(build_schema(SAMPLE_ROWS), quote_all=True)
    assert "FROM [dbo].[Customer] AS [c]" in sql
    assert "INNER JOIN [dbo].[Order] AS [o]\n    ON [o].[CustomerID] = [c].[CustomerID]" in sql
    # A column name in two tables is aliased so the result has unique names.
    assert "[o].[CustomerID] AS [Order_CustomerID]" in sql
    assert sql.rstrip().endswith(";")


def test_composite_join_left_and_top():
    sql = generate_select(build_schema(COMPOSITE_ROWS), root="Shipment", join="LEFT", top=50,
                          quote_all=True)
    assert sql.startswith("SELECT TOP (50)")
    assert "FROM [dbo].[Shipment] AS [s]" in sql
    assert ("LEFT JOIN [dbo].[OrderLine] AS [ol]\n"
            "    ON [s].[OrderID] = [ol].[OrderID] AND [s].[LineNo] = [ol].[LineNo]") in sql
    # The second FK pair to OrderLine and Audit.ModifiedBy are noted, not joined.
    assert "-- Also related: [s].[ReturnOrderID] = [ol].[OrderID]" in sql
    assert "-- Also related: [a].[ModifiedBy] = [o].[OrderID]" in sql


def test_unconnected_tables_and_unknown_columns_are_commented():
    schema = Schema(
        tables=[Table("", "A", [Column("", "A", 1, "Id", "int")]),
                Table("", "B", [Column("", "B", 1, "AId", "int")]),
                Table("", "Z", [Column("", "Z", 1, "Id", "int")])],
        relationships=[Relationship("A", "B", "AId")],  # columns unknown
    )
    sql = generate_select(schema)
    assert "ON 1 = 1  -- TODO: join columns unknown for B.AId → A" in sql
    assert "-- Not joined (no foreign-key path to A): Z." in sql


def test_self_reference_is_noted():
    schema = Schema(
        tables=[Table("", "Emp", [Column("", "Emp", 1, "Id", "int")])],
        relationships=[Relationship("Emp", "Emp", "ManagerId", ("ManagerId",), ("Id",))],
    )
    assert "-- Emp references itself through ManagerId" in generate_select(schema)


def test_excluded_columns_are_left_out_of_the_select_list():
    schema = filter_columns(build_schema(SAMPLE_ROWS), {("customer", "name")})
    sql = generate_select(schema, quote_all=True)
    assert "[c].[Name]" not in sql and "[c].[CustomerID]" in sql


def test_names_are_bracketed_only_where_tsql_needs_it():
    assert ident("bookableresourcebooking") == "bookableresourcebooking"
    assert ident("hsl_dayrule") == "hsl_dayrule" and ident("_x$1") == "_x$1"
    assert ident("Order") == "[Order]" and ident("user") == "[user]"  # reserved
    assert ident("Order Line") == "[Order Line]" and ident("Amount($)") == "[Amount($)]"
    assert ident("2024_Sales") == "[2024_Sales]" and ident("#temp") == "[#temp]"
    assert ident("anything", quote_all=True) == "[anything]"
    sql = generate_select(build_schema(SAMPLE_ROWS))
    assert "FROM dbo.Customer AS c" in sql
    assert "INNER JOIN dbo.[Order] AS o\n    ON o.CustomerID = c.CustomerID" in sql
    assert "o.CustomerID AS Order_CustomerID" in sql


def test_quoting_escapes_brackets():
    assert quote("we]ird") == "[we]]ird]"
    schema = Schema(tables=[Table("odd]schema", "t]1", [Column("", "t]1", 1, "c]1", "int")])])
    sql = generate_select(schema)
    assert "FROM [odd]]schema].[t]]1]" in sql and ".[c]]1]" in sql


def test_rejects_unknown_join_type_and_handles_no_tables():
    with pytest.raises(ValueError):
        generate_select(build_schema(SAMPLE_ROWS), join="CROSS")
    assert generate_select(Schema()) == "-- No tables selected.\n"


def test_output_parses_as_tsql():
    sqlglot = pytest.importorskip("sqlglot")
    for sql in (
        generate_select(build_schema(SAMPLE_ROWS)),
        generate_select(build_schema(COMPOSITE_ROWS), root="Shipment", join="LEFT", top=10),
    ):
        assert sqlglot.parse_one(sql.split(";")[0], read="tsql") is not None
