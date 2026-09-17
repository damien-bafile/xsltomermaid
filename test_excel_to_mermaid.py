"""Tests for the pure-Python core (no Qt / no Excel needed).

Run: python test_excel_to_mermaid.py   (or: python -m pytest test_excel_to_mermaid.py)
"""

from excel_to_mermaid import (
    DiagramOptions,
    Relationship,
    Schema,
    Table,
    _parse_reference_table,
    build_schema,
    filter_columns,
    filter_schema,
    generate_mermaid,
    shortest_path,
)


def _chain_schema():
    # A - B - C - D  (plus an isolated E), edges are undirected FK links.
    tables = [Table("", n) for n in ["A", "B", "C", "D", "E"]]
    rels = [
        Relationship("A", "B", "ab"),
        Relationship("B", "C", "bc"),
        Relationship("C", "D", "cd"),
    ]
    return Schema(tables=tables, relationships=rels)


def test_shortest_path_along_chain():
    schema = _chain_schema()
    assert shortest_path(schema, "A", "D") == ["A", "B", "C", "D"]
    # Undirected: works the other way too.
    assert shortest_path(schema, "D", "A") == ["D", "C", "B", "A"]


def test_shortest_path_picks_shortcut():
    schema = _chain_schema()
    # Add a direct A-D edge; the shortest path is now just the two endpoints.
    schema.relationships.append(Relationship("A", "D", "ad"))
    assert shortest_path(schema, "A", "D") == ["A", "D"]


def test_shortest_path_disconnected_and_self():
    schema = _chain_schema()
    assert shortest_path(schema, "A", "E") is None  # E is isolated
    assert shortest_path(schema, "A", "Z") is None  # unknown table
    assert shortest_path(schema, "B", "B") == ["B"]  # same endpoint


def test_shortest_path_is_case_insensitive():
    schema = _chain_schema()
    assert shortest_path(schema, "a", "c") == ["A", "B", "C"]

SAMPLE_ROWS = [
    {"SchemaName": "dbo", "TableName": "Customer", "ColumnOrder": 1,
     "ColumnName": "CustomerID", "DataType": "int", "IsPrimaryKey": 1, "IsNullable": 0},
    {"SchemaName": "dbo", "TableName": "Customer", "ColumnOrder": 2,
     "ColumnName": "Name", "DataType": "varchar", "Length": 100, "IsNullable": 0},
    {"SchemaName": "dbo", "TableName": "Order", "ColumnOrder": 1,
     "ColumnName": "OrderID", "DataType": "int", "IsPrimaryKey": 1},
    {"SchemaName": "dbo", "TableName": "Order", "ColumnOrder": 2,
     "ColumnName": "CustomerID", "DataType": "int",
     "ForeignKeyReference": "dbo.Customer.CustomerID"},
    {"SchemaName": "dbo", "TableName": "Order", "ColumnOrder": 3,
     "ColumnName": "Total", "DataType": "decimal", "Precision": 18, "Scale": 2},
]


def test_reference_parsing():
    assert _parse_reference_table("dbo.Customer.CustomerID") == "Customer"
    assert _parse_reference_table("Customer.CustomerID") == "Customer"
    assert _parse_reference_table("Customer(CustomerID)") == "Customer"
    assert _parse_reference_table("Customer") == "Customer"
    assert _parse_reference_table("") is None


def test_build_schema_tables_and_columns():
    schema = build_schema(SAMPLE_ROWS)
    names = {t.name for t in schema.tables}
    assert names == {"Customer", "Order"}
    customer = next(t for t in schema.tables if t.name == "Customer")
    assert [c.name for c in customer.columns] == ["CustomerID", "Name"]
    pk = customer.columns[0]
    assert pk.is_primary_key and not pk.is_nullable


def test_rendered_types():
    schema = build_schema(SAMPLE_ROWS)
    order = next(t for t in schema.tables if t.name == "Order")
    total = next(c for c in order.columns if c.name == "Total")
    assert total.rendered_type() == "decimal(18,2)"
    customer = next(t for t in schema.tables if t.name == "Customer")
    name = next(c for c in customer.columns if c.name == "Name")
    assert name.rendered_type() == "varchar(100)"


def test_relationships():
    schema = build_schema(SAMPLE_ROWS)
    assert len(schema.relationships) == 1
    rel = schema.relationships[0]
    assert rel.parent_table == "Customer"
    assert rel.child_table == "Order"
    assert rel.label == "CustomerID"


def test_filter_schema_keeps_only_selected():
    schema = build_schema(SAMPLE_ROWS)
    filtered = filter_schema(schema, ["Customer"])
    assert [t.name for t in filtered.tables] == ["Customer"]
    # The FK relationship needs both endpoints, so it's dropped here.
    assert filtered.relationships == []


def test_filter_schema_is_case_insensitive():
    schema = build_schema(SAMPLE_ROWS)
    filtered = filter_schema(schema, ["customer", "ORDER"])
    assert {t.name for t in filtered.tables} == {"Customer", "Order"}
    assert len(filtered.relationships) == 1


def test_filter_schema_include_related_pulls_in_neighbours():
    schema = build_schema(SAMPLE_ROWS)
    # Selecting only the child (Order) should pull in the referenced Customer.
    filtered = filter_schema(schema, ["Order"], include_related=True)
    assert {t.name for t in filtered.tables} == {"Customer", "Order"}
    assert len(filtered.relationships) == 1


def test_filter_schema_include_related_is_single_layer():
    # Chain: A -> B -> C. Selecting A with related should pull in B (one hop),
    # but not C (which is two hops away), regardless of relationship order.
    from excel_to_mermaid import Relationship, Schema, Table

    schema = Schema(
        tables=[Table("", "A"), Table("", "B"), Table("", "C")],
        relationships=[
            Relationship(parent_table="A", child_table="B", label="a"),
            Relationship(parent_table="B", child_table="C", label="b"),
        ],
    )
    filtered = filter_schema(schema, ["A"], include_related=True)
    assert {t.name for t in filtered.tables} == {"A", "B"}
    # Only the A–B edge survives (C isn't present).
    assert len(filtered.relationships) == 1
    assert filtered.relationships[0].child_table == "B"


def test_filter_schema_empty_selection():
    schema = build_schema(SAMPLE_ROWS)
    filtered = filter_schema(schema, [])
    assert filtered.tables == []
    assert filtered.relationships == []


def test_filter_columns_drops_selected_columns():
    schema = build_schema(SAMPLE_ROWS)
    # Drop the "Name" column from Customer.
    filtered = filter_columns(schema, {("customer", "name")})
    customer = next(t for t in filtered.tables if t.name == "Customer")
    assert [c.name for c in customer.columns] == ["CustomerID"]
    # Other tables are untouched.
    order = next(t for t in filtered.tables if t.name == "Order")
    assert len(order.columns) == 3
    # Relationships are kept even if a column was dropped.
    assert len(filtered.relationships) == 1


def test_filter_columns_empty_is_noop():
    schema = build_schema(SAMPLE_ROWS)
    assert filter_columns(schema, set()) is schema


def test_generate_mermaid():
    schema = build_schema(SAMPLE_ROWS)
    text = generate_mermaid(schema)
    assert text.startswith("erDiagram")
    assert "Customer ||--o{ Order" in text
    assert "int CustomerID PK" in text
    # Mermaid's ER parser rejects parentheses, so decimal(18,2) is flattened.
    assert "decimal_18_2 Total" in text
    # Every entity block is present.
    assert "Customer {" in text
    assert "Order {" in text


def test_options_default_matches_bare_call():
    schema = build_schema(SAMPLE_ROWS)
    assert generate_mermaid(schema) == generate_mermaid(schema, DiagramOptions())


def test_options_hide_relationship_labels():
    schema = build_schema(SAMPLE_ROWS)
    text = generate_mermaid(schema, DiagramOptions(show_rel_labels=False))
    assert 'Customer ||--o{ Order : ""' in text
    assert "CustomerID" in text  # still present as a column, just not as a label


def test_options_hide_comments():
    schema = build_schema(SAMPLE_ROWS)
    # "not null" is a note on non-nullable columns; it should vanish.
    with_notes = generate_mermaid(schema, DiagramOptions(show_comments=True))
    without = generate_mermaid(schema, DiagramOptions(show_comments=False))
    assert "not null" in with_notes
    assert "not null" not in without


def test_options_keys_only():
    schema = build_schema(SAMPLE_ROWS)
    text = generate_mermaid(schema, DiagramOptions(keys_only=True))
    assert "int CustomerID PK" in text  # PK kept
    # Non-key columns dropped from the attribute lists.
    assert "varchar Name" not in text
    assert "decimal_18_2 Total" not in text


def test_options_prefix_schema():
    schema = build_schema(SAMPLE_ROWS)
    text = generate_mermaid(schema, DiagramOptions(prefix_schema=True))
    # Entity ids become schema-qualified (dots are flattened to underscores).
    assert "dbo_Customer {" in text
    assert "dbo_Customer ||--o{ dbo_Order" in text


def _run():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as exc:  # noqa: PERF203
            failures += 1
            print(f"FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if _run() else 0)
