"""Tests for the pure-Python core (no Qt / no Excel needed).

Run: python test_excel_to_mermaid.py   (or: python -m pytest test_excel_to_mermaid.py)
"""

from excel_to_mermaid import (
    _parse_reference_table,
    build_schema,
    filter_schema,
    generate_mermaid,
)

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


def test_filter_schema_empty_selection():
    schema = build_schema(SAMPLE_ROWS)
    filtered = filter_schema(schema, [])
    assert filtered.tables == []
    assert filtered.relationships == []


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
