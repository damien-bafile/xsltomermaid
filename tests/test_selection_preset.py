"""Tests for table-selection preset serialization."""

from xsltomermaid.selection_preset import dump_selection_toml, load_selection_toml


def test_selection_preset_round_trip():
    text = dump_selection_toml("sample_schema.xlsx", ["Customer", "Order", "customer"])
    filename, selected = load_selection_toml(text)
    assert filename == "sample_schema.xlsx"
    assert selected == ["Customer", "Order"]


def test_selection_preset_trims_and_ignores_empty_names():
    text = dump_selection_toml("x.xlsx", [" Customer ", "", "  ", "Order"])
    _filename, selected = load_selection_toml(text)
    assert selected == ["Customer", "Order"]


def test_selection_preset_rejects_invalid_types():
    bad = 'filename = ["not-a-string"]\nselected_tables = ["Customer"]\n'
    try:
        load_selection_toml(bad)
    except ValueError as exc:
        assert "filename" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected ValueError for invalid filename type")


def test_selection_preset_load_dedupes_case_insensitive():
    text = 'filename = "sample_schema.xlsx"\nselected_tables = ["Customer", "customer", "Order"]\n'
    _filename, selected = load_selection_toml(text)
    assert selected == ["Customer", "Order"]


def test_selection_preset_dump_rejects_non_string_entries():
    try:
        dump_selection_toml("sample_schema.xlsx", ["Customer", 123])  # type: ignore[list-item]
    except ValueError as exc:
        assert "selected_tables" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected ValueError for non-string selected table")


def test_selection_preset_rejects_malformed_content():
    bad = 'filename = "x.xlsx"\nthis is not valid toml\nselected_tables = ["A"]\n'
    try:
        load_selection_toml(bad)
    except ValueError as exc:
        assert str(exc) == "Invalid TOML preset content."
    else:  # pragma: no cover
        raise AssertionError("Expected ValueError for malformed TOML")


def test_selection_preset_allows_extra_keys():
    text = (
        'filename = "sample_schema.xlsx"\n'
        'selected_tables = ["Customer"]\n'
        'note = "extra metadata"\n'
    )
    filename, selected = load_selection_toml(text)
    assert filename == "sample_schema.xlsx"
    assert selected == ["Customer"]


def test_preset_keeps_column_choices_and_label_layout():
    from xsltomermaid import selection_preset
    from xsltomermaid.selection_preset import Preset, dump_preset, load_preset

    preset = Preset(
        "dynamics.xlsx", ["account", "contact"],
        hidden_columns={("account", "traversedpath"), ("contact", "fax")},
        column_order={"account": ["name", "accountid"]},
        column_sort="name", keys_first=True,
        label_layout={"contact|account|0": [90.0, 28.0, -19.5]},
    )
    text = dump_preset(preset)
    assert load_preset(text) == preset
    # The Python 3.10 fallback reads it too: one line of JSON per key.
    real = selection_preset.tomllib
    try:
        selection_preset.tomllib = None
        assert load_preset(text) == preset
    finally:
        selection_preset.tomllib = real


def test_an_older_preset_leaves_column_choices_alone():
    from xsltomermaid.selection_preset import load_preset

    preset = load_preset('filename = "x.xlsx"\nselected_tables = ["A"]\n')
    assert preset.selected_tables == ["A"]
    assert preset.hidden_columns is None and preset.column_order is None
    assert preset.column_sort is None and preset.keys_first is None
    assert preset.label_layout is None


def test_preset_rejects_bad_column_values():
    import pytest

    from xsltomermaid.selection_preset import load_preset

    head = 'filename = "x.xlsx"\nselected_tables = ["A"]\n'
    for line, key in (
        ('hidden_columns = [["a"]]', "hidden_columns"),
        ('column_order = [["a", "b"]]', "column_order"),
        ('column_sort = "random"', "column_sort"),
        ('keys_first = "yes"', "keys_first"),
        ('label_layout = [["k", 1, 2]]', "label_layout"),
    ):
        with pytest.raises(ValueError, match=key):
            load_preset(head + line + "\n")
