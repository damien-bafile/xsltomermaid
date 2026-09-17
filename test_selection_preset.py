from selection_preset import dump_selection_toml, load_selection_toml


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
