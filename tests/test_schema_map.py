"""Tests for the schema map's clustering and layout (no Qt needed)."""

import math

from xsltomermaid.excel_to_mermaid import Relationship, Schema, Table
from xsltomermaid.schema_map import HIDDEN_BY_DEFAULT, build_map


def _schema(tables, links):
    return Schema(
        tables=[Table("dbo", n) for n in tables],
        relationships=[Relationship(parent, child, col, (col,), ("id",)) for child, parent, col in links],
    )


def _two_modules():
    """Two tight groups (a*, b*) joined by one link, a lonely table, and audit
    links from everything to systemuser."""
    a = [f"a{i}" for i in range(6)]
    b = [f"b{i}" for i in range(6)]
    links = [(x, "a0", "a_ref") for x in a[1:]] + [(x, "b0", "b_ref") for x in b[1:]]
    links += [("a1", "a2", "peer"), ("b1", "b2", "peer"), ("a1", "b1", "bridge")]
    links += [(x, "systemuser", "createdby") for x in a + b]
    return _schema(a + b + ["systemuser", "lonely"], links)


def test_audit_links_are_hidden_by_default_and_can_be_shown():
    m = build_map(_two_modules())
    assert m.total_links == 25 and m.shown_links == 13
    assert "systemuser" in m.unconnected and "lonely" in m.unconnected
    shown_all = build_map(_two_modules(), frozenset())
    assert shown_all.shown_links == 25 and "systemuser" in shown_all.nodes


def test_tight_groups_become_separate_clusters_named_after_their_hub():
    m = build_map(_two_modules())
    clusters = {c.hub: set(c.members) for c in m.communities}
    assert clusters["a0"] == {f"a{i}" for i in range(6)}
    assert clusters["b0"] == {f"b{i}" for i in range(6)}


def test_layout_puts_the_hub_at_the_centre_and_keeps_discs_apart():
    m = build_map(_two_modules())
    for comm in m.communities:
        hub = m.nodes[comm.hub]
        assert (hub.x, hub.y) == (comm.x, comm.y)
    a, b = m.communities[0], m.communities[1]
    assert math.hypot(a.x - b.x, a.y - b.y) >= a.radius + b.radius


def test_map_is_deterministic():
    one, two = build_map(_two_modules()), build_map(_two_modules())
    assert [(n.name, n.x, n.y) for n in one.nodes.values()] == [
        (n.name, n.x, n.y) for n in two.nodes.values()
    ]


def test_default_hidden_set_covers_audit_and_system_columns():
    assert {"createdby", "owninguser", "organizationid", "transactioncurrencyid"} <= HIDDEN_BY_DEFAULT


def test_link_counts_skip_audit_links_and_name_prefix():
    from xsltomermaid.schema_map import link_counts, name_prefix

    counts = link_counts(_two_modules())
    assert counts["a0"] == (0, 5)  # five a* tables reference it
    assert counts["a1"] == (3, 0)  # a1 references a0, a2 (peer) and b1 (bridge)
    assert counts["systemuser"] == (0, 0)  # only audit links touch it
    assert name_prefix("msdyn_project") == "msdyn"
    assert name_prefix("hsl_projectactivity") == "hsl"
    assert name_prefix("account") == ""
