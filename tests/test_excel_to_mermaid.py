"""Tests for the pure-Python core (no Qt / no Excel needed).

Run: python -m pytest tests/test_excel_to_mermaid.py
"""

from xsltomermaid.excel_to_mermaid import (
    DiagramOptions,
    Relationship,
    Schema,
    Table,
    _parse_reference_table,
    all_shortest_paths,
    build_schema,
    filter_columns,
    filter_schema,
    generate_mermaid,
    related_tables,
    route_paths,
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


def test_all_shortest_paths_along_a_chain_both_ways():
    schema = _chain_schema()
    assert all_shortest_paths(schema, "A", "D") == [["A", "B", "C", "D"]]
    assert all_shortest_paths(schema, "D", "A") == [["D", "C", "B", "A"]]  # undirected


def test_all_shortest_paths_takes_a_shortcut():
    schema = _chain_schema()
    schema.relationships.append(Relationship("A", "D", "ad"))
    assert all_shortest_paths(schema, "A", "D") == [["A", "D"]]


def test_all_shortest_paths_disconnected_self_and_case():
    schema = _chain_schema()
    assert all_shortest_paths(schema, "A", "E") == []  # E is isolated
    assert all_shortest_paths(schema, "A", "Z") == []  # unknown table
    assert all_shortest_paths(schema, "B", "B") == [["B"]]  # same endpoint
    assert all_shortest_paths(schema, "a", "c") == [["A", "B", "C"]]  # any case


def _diamond_schema():
    # A is parent of B and C; B and C are each parent of D. So A→D has two
    # equally short undirected paths: A-B-D and A-C-D.
    tables = [Table("", n) for n in ["A", "B", "C", "D"]]
    rels = [
        Relationship("A", "B", "ab"),
        Relationship("A", "C", "ac"),
        Relationship("B", "D", "bd"),
        Relationship("C", "D", "cd"),
    ]
    return Schema(tables=tables, relationships=rels)


def test_all_shortest_paths_returns_every_tie_sorted():
    schema = _diamond_schema()
    paths = all_shortest_paths(schema, "A", "D")
    assert paths == [["A", "B", "D"], ["A", "C", "D"]]  # both ties, sorted


def test_all_shortest_paths_respects_fk_direction():
    schema = _chain_schema()  # A parent-of B parent-of C parent-of D
    # "forward" follows the FK reference child→parent, so you can walk D up to A
    # but not A down to D.
    assert all_shortest_paths(schema, "D", "A", "forward") == [["D", "C", "B", "A"]]
    assert all_shortest_paths(schema, "A", "D", "forward") == []
    # "reverse" follows dependents parent→child: the mirror image.
    assert all_shortest_paths(schema, "A", "D", "reverse") == [["A", "B", "C", "D"]]
    assert all_shortest_paths(schema, "D", "A", "reverse") == []


def test_route_paths_visits_via_stop_in_order():
    schema = _chain_schema()
    routes, broken = route_paths(schema, ["A", "C", "D"])
    assert broken is None
    assert routes == [["A", "B", "C", "D"]]


def test_route_paths_reports_broken_segment():
    schema = _chain_schema()  # E is isolated
    routes, broken = route_paths(schema, ["A", "E", "D"])
    assert routes == []
    assert broken == ("A", "E")


def test_route_paths_combines_alternatives_across_segments():
    schema = _diamond_schema()
    # A→D has two ties; routing A→D→(back) keeps the alternatives distinct.
    routes, broken = route_paths(schema, ["A", "D"])
    assert broken is None
    assert routes == [["A", "B", "D"], ["A", "C", "D"]]


def test_related_tables_honours_direction():
    schema = _diamond_schema()  # A parent of B,C; B,C parent of D
    assert related_tables(schema, ["A"]) == {"B", "C"}  # both ways
    assert related_tables(schema, ["A"], "reverse") == {"B", "C"}  # dependents
    assert related_tables(schema, ["A"], "forward") == set()  # A references nothing
    assert related_tables(schema, ["D"], "forward") == {"B", "C"}  # D references B,C
    assert related_tables(schema, ["D"], "reverse") == set()
    assert related_tables(schema, ["B"]) == {"A", "D"}  # up to A, down to D


def test_related_tables_excludes_selected_and_is_case_insensitive():
    schema = _diamond_schema()
    assert related_tables(schema, ["a", "b"]) == {"C", "D"}


def test_unresolved_foreign_keys():
    from xsltomermaid.excel_to_mermaid import Column, unresolved_foreign_keys

    a = Table("dbo", "A", [Column("dbo", "A", 1, "AID", "int", is_primary_key=True)])
    b = Table(
        "dbo",
        "B",
        [
            Column("dbo", "B", 1, "BID", "int", is_primary_key=True),
            Column("dbo", "B", 2, "AID", "int", foreign_key_reference="dbo.A.AID"),
            Column("dbo", "B", 3, "GID", "int", foreign_key_reference="dbo.Ghost.GID"),
        ],
    )
    schema = Schema(tables=[a, b], relationships=[])
    # A.AID resolves (A is present); Ghost does not.
    assert unresolved_foreign_keys(schema) == [("B", "dbo.Ghost.GID")]

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
    # A schema-qualified table with "(Column)" — previously misread as "dbo".
    assert _parse_reference_table("dbo.Customer(CustomerID)") == "Customer"
    assert _parse_reference_table("dbo.OrderLine(OrderID, LineNo)") == "OrderLine"
    assert _parse_reference_table("[dbo].[Customer].[CustomerID]") == "Customer"


def _row(table, order, column, pk=False, fk=""):
    return {"SchemaName": "dbo", "TableName": table, "ColumnOrder": order,
            "ColumnName": column, "DataType": "int", "IsPrimaryKey": int(pk),
            "IsNullable": int(not pk), "ForeignKeyReference": fk}


COMPOSITE_ROWS = [
    _row("Order", 1, "OrderID", pk=True),
    # Composite PK where one part is also an FK.
    _row("OrderLine", 1, "OrderID", pk=True, fk="dbo.Order.OrderID"),
    _row("OrderLine", 2, "LineNo", pk=True),
    # A composite FK spread over two columns, plus a second one to the same key.
    _row("Shipment", 1, "ShipmentID", pk=True),
    _row("Shipment", 2, "OrderID", fk="dbo.OrderLine.OrderID"),
    _row("Shipment", 3, "LineNo", fk="dbo.OrderLine.LineNo"),
    _row("Shipment", 4, "ReturnOrderID", fk="dbo.OrderLine.OrderID"),
    _row("Shipment", 5, "ReturnLineNo", fk="dbo.OrderLine.LineNo"),
    # Two separate single-column FKs to one parent stay separate.
    _row("Audit", 1, "AuditID", pk=True),
    _row("Audit", 2, "CreatedBy", fk="dbo.Order.OrderID"),
    _row("Audit", 3, "ModifiedBy", fk="dbo.Order.OrderID"),
]


def test_composite_foreign_keys_become_one_relationship_each():
    schema = build_schema(COMPOSITE_ROWS)
    rels = {(r.parent_table, r.child_table, r.label) for r in schema.relationships}
    assert rels == {
        ("Order", "OrderLine", "OrderID"),
        ("OrderLine", "Shipment", "OrderID, LineNo"),
        ("OrderLine", "Shipment", "ReturnOrderID, ReturnLineNo"),
        ("Order", "Audit", "CreatedBy"),
        ("Order", "Audit", "ModifiedBy"),
    }


def test_partial_composite_foreign_key_keeps_its_column():
    rows = COMPOSITE_ROWS[:3] + [
        _row("Note", 1, "NoteID", pk=True),
        _row("Note", 2, "OrderID", fk="dbo.OrderLine.OrderID"),
    ]
    rels = build_schema(rows).relationships
    assert ("OrderLine", "Note", "OrderID") in {
        (r.parent_table, r.child_table, r.label) for r in rels
    }


def test_composite_keys_in_mermaid():
    mermaid = generate_mermaid(build_schema(COMPOSITE_ROWS))
    assert "int OrderID PK,FK" in mermaid
    assert "int LineNo PK" in mermaid
    assert 'OrderLine ||--o{ Shipment : "OrderID, LineNo"' in mermaid


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


def test_schema_import_service_coordinates_import_steps(monkeypatch):
    from xsltomermaid import services

    rows = [{"TableName": "Customer"}]
    schema = Schema()
    monkeypatch.setattr(
        services,
        "read_rows",
        lambda path, progress: (progress(0.5), rows)[1],
    )
    monkeypatch.setattr(
        services,
        "build_schema",
        lambda values, progress: (progress(0.75), schema)[1],
    )
    monkeypatch.setattr(services, "generate_mermaid", lambda value: "erDiagram\n")
    progress = []

    result = services.SchemaImportService().load(
        "schema.xlsx", lambda stage, percent: progress.append((stage, percent))
    )

    assert result == (rows, schema, "erDiagram\n")
    assert progress == [
        ("Reading file…", 0),
        ("Reading file…", 25),
        ("Building schema…", 50),
        ("Building schema…", 80),
        ("Generating diagram…", 90),
        ("Generating diagram…", 95),
    ]
    # The window skips the whole-schema Mermaid: it draws only ticked tables.
    called = []
    monkeypatch.setattr(services, "generate_mermaid", lambda value: called.append(1) or "x")
    assert services.SchemaImportService().load("schema.xlsx", with_mermaid=False) == (
        rows, schema, ""
    )
    assert not called


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


def test_wrap_mermaid_html_escapes_source_and_takes_a_local_script():
    from xsltomermaid.excel_to_mermaid import MERMAID_CDN, wrap_mermaid_html

    page = wrap_mermaid_html('erDiagram\n    A_B { int x "a<b & c" }')
    assert MERMAID_CDN in page
    assert "a&lt;b &amp; c" in page  # Mermaid reads the decoded text
    local = wrap_mermaid_html("erDiagram", script_src="mermaid.min.js")
    assert '<script src="mermaid.min.js">' in local and MERMAID_CDN not in local


def test_mermaid_entity_ids_match_the_generated_ids():
    from xsltomermaid.excel_to_mermaid import mermaid_entity_ids

    schema = build_schema(SAMPLE_ROWS)
    ids = mermaid_entity_ids(schema)
    assert ids == {"Customer": "Customer", "Order": "Order"}
    mermaid = generate_mermaid(schema)
    assert all(f"    {eid} {{" in mermaid for eid in ids)
    prefixed = mermaid_entity_ids(schema, DiagramOptions(prefix_schema=True))
    assert prefixed == {"dbo_Customer": "Customer", "dbo_Order": "Order"}


def test_dynamics_solution_layering_columns_are_not_primary_keys():
    rows = [
        _row("bookableresource", 1, "bookableresourceid", pk=True),
        _row("bookableresource", 2, "overwritetime", pk=True),
        _row("bookableresource", 3, "componentstate", pk=True),
        _row("booking", 1, "bookingid", pk=True),
        _row("booking", 2, "resource", fk="dbo.bookableresource.bookableresourceid"),
        # A table whose only key is a layering column keeps it.
        _row("odd", 1, "componentstate", pk=True),
    ]
    schema = build_schema(rows)
    br = next(t for t in schema.tables if t.name == "bookableresource")
    assert [c.name for c in br.columns if c.is_primary_key] == ["bookableresourceid"]
    odd = next(t for t in schema.tables if t.name == "odd")
    assert odd.columns[0].is_primary_key
    (rel,) = schema.relationships
    assert rel.parent_columns == ("bookableresourceid",)
    assert "int overwritetime PK" not in generate_mermaid(schema)


def test_keys_only_can_drop_hidden_audit_columns():
    rows = [
        _row("account", 1, "accountid", pk=True),
        _row("account", 2, "createdby", fk="dbo.systemuser.systemuserid"),
        _row("account", 3, "primarycontact", fk="dbo.contact.contactid"),
        _row("account", 4, "name"),
    ]
    schema = build_schema(rows)
    plain = generate_mermaid(schema, DiagramOptions(keys_only=True))
    assert "createdby" in plain and "primarycontact" in plain and " name" not in plain
    hidden = generate_mermaid(
        schema, DiagramOptions(keys_only=True, hide_columns=frozenset({"createdby"}))
    )
    # The column row goes; hiding the link itself is the window's job.
    assert "int createdby FK" not in hidden
    assert "int primarycontact FK" in hidden and "accountid PK" in hidden


def test_keys_only_ends_each_reduced_table_with_a_hidden_count():
    schema = build_schema(SAMPLE_ROWS)
    text = generate_mermaid(schema, DiagramOptions(keys_only=True))
    customer = text[text.index("Customer {"):]
    customer = customer[:customer.index("}")]
    hidden = sum(1 for c in schema.tables[0].columns if not c.is_primary_key)
    assert f'more columns "+{hidden} hidden by Keys only"' in customer
    assert "more columns" not in generate_mermaid(schema)  # nothing hidden
    off = generate_mermaid(schema, DiagramOptions(keys_only=True, note_hidden=False))
    assert "more columns" not in off


def test_keys_only_collapses_long_fk_lists_to_drawn_tables():
    from xsltomermaid.excel_to_mermaid import KEYS_ONLY_ROW_LIMIT, drawn_columns, linked_fk_columns

    rows = [_row("hub", 1, "hubid", pk=True)]
    for i in range(KEYS_ONLY_ROW_LIMIT + 5):
        rows.append(_row("hub", i + 2, f"ref{i}", fk=f"dbo.t{i}.t{i}id"))
    rows += [_row("t0", 1, "t0id", pk=True), _row("t1", 1, "t1id", pk=True)]
    schema = filter_schema(build_schema(rows), ["hub", "t0", "t1"])
    hub = next(t for t in schema.tables if t.name == "hub")
    opts = DiagramOptions(keys_only=True)
    result = drawn_columns(hub, opts, linked_fk_columns(schema)["hub"])
    assert [c.name for c in result.columns] == ["hubid", "ref0", "ref1"]
    assert result.collapsed_fk == KEYS_ONLY_ROW_LIMIT + 3
    assert result.hidden == result.collapsed_fk
    text = generate_mermaid(schema, opts)
    assert f"+{result.hidden} hidden by Keys only, {result.collapsed_fk} of them FK" in text
    assert "ref5" not in text
    # A short table keeps every key, drawn or not.
    short = build_schema(rows[:4])
    assert len(drawn_columns(short.tables[0], opts, set()).columns) == 4


def _schema_row(schema, table, order, column, pk=False, fk=""):
    return {"SchemaName": schema, "TableName": table, "ColumnOrder": order,
            "ColumnName": column, "DataType": "int", "IsPrimaryKey": int(pk),
            "IsNullable": int(not pk), "ForeignKeyReference": fk}


def test_tables_sharing_a_name_across_schemas_stay_separate():
    from xsltomermaid.excel_to_mermaid import mermaid_entity_ids, unresolved_foreign_keys

    rows = [
        _schema_row("dbo", "Customer", 1, "Id", pk=True),
        _schema_row("dbo", "Customer", 2, "Name"),
        _schema_row("sales", "Customer", 1, "Id", pk=True),
        _schema_row("sales", "Customer", 2, "Region"),
        _schema_row("sales", "Order", 1, "Id", pk=True),
        # Schema named: sales' Customer. No schema: the order's own schema first.
        _schema_row("sales", "Order", 2, "BuyerId", fk="sales.Customer.Id"),
        _schema_row("sales", "Order", 3, "OwnerId", fk="Customer.Id"),
        _schema_row("dbo", "Invoice", 1, "CustomerId", fk="dbo.Customer(Id)"),
    ]
    schema = build_schema(rows)
    names = [t.name for t in schema.tables]
    assert "dbo.Customer" in names and "sales.Customer" in names  # not merged
    assert "Order" in names and "Invoice" in names  # unique names stay plain
    customers = {t.name: [c.name for c in t.columns] for t in schema.tables if "Customer" in t.name}
    assert customers == {"dbo.Customer": ["Id", "Name"], "sales.Customer": ["Id", "Region"]}
    links = {(r.child_table, r.label): r.parent_table for r in schema.relationships}
    assert links[("Order", "BuyerId")] == "sales.Customer"
    assert links[("Order", "OwnerId")] == "sales.Customer"
    assert links[("Invoice", "CustomerId")] == "dbo.Customer"
    assert unresolved_foreign_keys(schema) == []
    # Two entities in the diagram, each with its own columns.
    text = generate_mermaid(schema)
    assert "dbo_Customer {" in text and "sales_Customer {" in text
    assert len(set(mermaid_entity_ids(schema))) == len(schema.tables)
    sales = next(t for t in schema.tables if t.name == "sales.Customer")
    assert sales.full_name == "sales.Customer"  # not qualified twice
    from xsltomermaid.sql_query import generate_select
    order = next(t for t in schema.tables if t.name == "Order")
    sql = generate_select(Schema([order, sales], [r for r in schema.relationships
                                                  if r.child_table == "Order"]))
    assert "sales.Customer AS" in sql and "sales.[sales.Customer]" not in sql


def test_names_that_clean_to_the_same_diagram_id_get_their_own():
    from xsltomermaid.excel_to_mermaid import mermaid_entity_ids

    rows = [_row("Order Line", 1, "Id", pk=True), _row("Order_Line", 1, "Id", pk=True),
            _row("hsl_day_rule", 1, "Id", pk=True), _row("hsldayrule", 1, "Id", pk=True)]
    schema = build_schema(rows)
    ids = mermaid_entity_ids(schema)
    assert len(ids) == 4 and set(ids.values()) == {t.name for t in schema.tables}
    # Unique even as Mermaid draws them (underscores dropped from group ids).
    assert len({i.replace("_", "").lower() for i in ids}) == 4
    text = generate_mermaid(schema)
    assert sum(1 for line in text.splitlines() if line.rstrip().endswith("{")) == 4
