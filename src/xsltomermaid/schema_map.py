"""Lay out a whole schema as a map: clusters of tables, sized by connections.

Pure Python (no Qt), like :mod:`excel_to_mermaid`. A big schema (a Dynamics
export has ~1,800 tables) is too large for an ER diagram, so the map shows its
shape instead: tables are points, grouped into communities of tightly linked
tables, each community a disc with its most-connected table (its hub) at the
centre. Tables with no foreign keys at all are listed separately as
"unconnected" rather than scattered across the map.

Audit, ownership and system links (``createdby``, ``owninguser``,
``organizationid``, …) touch almost every Dynamics table, so by default they
are left out: with them, everything is one undifferentiated block.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from .excel_to_mermaid import AUDIT_FK_COLUMNS, Relationship, Schema

# System-wide references that, like audit links, connect nearly everything.
SYSTEM_FK_COLUMNS = frozenset({"organizationid", "transactioncurrencyid"})
HIDDEN_BY_DEFAULT = AUDIT_FK_COLUMNS | SYSTEM_FK_COLUMNS


@dataclass
class MapNode:
    name: str
    degree: int  # links shown on the map (both directions)
    community: int  # index into SchemaMap.communities
    x: float = 0.0
    y: float = 0.0


@dataclass
class Community:
    hub: str  # the most-connected table, which names the cluster
    members: list[str]
    x: float = 0.0
    y: float = 0.0
    radius: float = 0.0


@dataclass
class SchemaMap:
    nodes: dict[str, MapNode] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)
    communities: list[Community] = field(default_factory=list)
    unconnected: list[str] = field(default_factory=list)
    total_links: int = 0  # every relationship in the schema
    shown_links: int = 0  # relationships the map draws


def _is_hidden(rel: Relationship, hidden: frozenset[str]) -> bool:
    columns = rel.child_columns or tuple(c.strip() for c in rel.label.split(","))
    return bool(columns) and all(c.lower() in hidden for c in columns)


def link_counts(
    schema: Schema, hidden_columns: frozenset[str] = HIDDEN_BY_DEFAULT
) -> dict[str, tuple[int, int]]:
    """``{table: (references out, references in)}``, leaving out hidden links.

    Self-references count once, as "out". Used by the table list so its
    numbers match what the map shows.
    """
    counts = {t.name: [0, 0] for t in schema.tables}
    for rel in schema.relationships:
        if _is_hidden(rel, hidden_columns):
            continue
        if rel.child_table in counts:
            counts[rel.child_table][0] += 1
        if rel.parent_table in counts and rel.parent_table != rel.child_table:
            counts[rel.parent_table][1] += 1
    return {name: (out, in_) for name, (out, in_) in counts.items()}


def name_prefix(name: str) -> str:
    """A Dynamics-style publisher prefix: ``msdyn_project`` → ``msdyn``; "" if none."""
    head, sep, _rest = name.partition("_")
    return head.lower() if sep and head else ""


# ---------------------------------------------------------------------------
# Communities: Louvain modularity optimisation (two levels is plenty here)
# ---------------------------------------------------------------------------
def _louvain_pass(adj: dict[int, dict[int, float]], order: list[int]) -> dict[int, int]:
    """One local-moving phase: each node joins the neighbouring community
    that most increases modularity. Returns node → community."""
    m2 = sum(w for nbrs in adj.values() for w in nbrs.values()) or 1.0  # 2m
    degree = {n: sum(adj[n].values()) for n in adj}
    community = {n: n for n in adj}
    tot = dict(degree)  # total degree per community
    moved = True
    while moved:
        moved = False
        for node in order:
            current = community[node]
            k = degree[node]
            links: dict[int, float] = defaultdict(float)
            for nbr, w in adj[node].items():
                if nbr != node:
                    links[community[nbr]] += w
            tot[current] -= k
            best, best_gain = current, links.get(current, 0.0) - tot[current] * k / m2
            for comm, w in sorted(links.items()):
                gain = w - tot[comm] * k / m2
                if gain > best_gain + 1e-12:
                    best, best_gain = comm, gain
            tot[best] += k
            if best != current:
                community[node] = best
                moved = True
    return community


def _communities(names: list[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    index = {n: i for i, n in enumerate(names)}
    adj: dict[int, dict[int, float]] = {i: {} for i in range(len(names))}
    for a, b in edges:
        ia, ib = index[a], index[b]
        if ia == ib:
            continue
        adj[ia][ib] = adj[ia].get(ib, 0.0) + 1.0
        adj[ib][ia] = adj[ib].get(ia, 0.0) + 1.0
    # Visit low-degree tables first: hubs then join whichever cluster their
    # neighbours formed, instead of absorbing everything around them.
    order = sorted(adj, key=lambda n: (len(adj[n]), names[n]))
    level1 = _louvain_pass(adj, order)

    # Aggregate and run once more, so small clusters merge where it helps.
    groups: dict[int, list[int]] = defaultdict(list)
    for node, comm in level1.items():
        groups[comm].append(node)
    keys = sorted(groups)
    super_of = {comm: i for i, comm in enumerate(keys)}
    sadj: dict[int, dict[int, float]] = {i: {} for i in range(len(keys))}
    for node, nbrs in adj.items():
        for nbr, w in nbrs.items():
            a, b = super_of[level1[node]], super_of[level1[nbr]]
            sadj[a][b] = sadj[a].get(b, 0.0) + w
    level2 = _louvain_pass(sadj, sorted(sadj, key=lambda n: (len(groups[keys[n]]), n)))

    merged: dict[int, list[str]] = defaultdict(list)
    for node, comm in level1.items():
        merged[level2[super_of[comm]]].append(names[node])
    return sorted(
        (sorted(members) for members in merged.values()),
        key=lambda members: (-len(members), members[0]),
    )


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
_GOLDEN_ANGLE = math.pi * (3 - math.sqrt(5))
_SPACING = 14.0  # distance scale between neighbouring points


def _place_members(comm: Community, nodes: dict[str, MapNode]):
    """Sunflower spiral, most connected first: the hub sits in the middle."""
    ranked = sorted(comm.members, key=lambda n: (-nodes[n].degree, n))
    for i, name in enumerate(ranked):
        r = _SPACING * math.sqrt(i)
        nodes[name].x = comm.x + r * math.cos(i * _GOLDEN_ANGLE)
        nodes[name].y = comm.y + r * math.sin(i * _GOLDEN_ANGLE)


def _place_communities(comms: list[Community], weights: dict[tuple[int, int], float]):
    """Pack the discs without overlaps, linked clusters next to each other.

    Biggest first. Each disc goes beside the already-placed cluster it shares
    the most links with (or the centre), at the first free spot on a spiral
    around it. Deterministic, never overlaps, and compact; a force layout
    collapsed big, mutually linked clusters into one unreadable blob.
    """
    margin = 22.0
    linked: dict[int, dict[int, float]] = {}
    for (i, j), w in weights.items():
        linked.setdefault(i, {})[j] = w
        linked.setdefault(j, {})[i] = w
    order = sorted(range(len(comms)), key=lambda i: (-len(comms[i].members), comms[i].hub))
    placed: list[int] = []
    for i in order:
        comm = comms[i]
        if not placed:
            comm.x = comm.y = 0.0
            placed.append(i)
            continue
        ties = linked.get(i, {})
        anchor_index = max(
            (p for p in placed if p in ties), key=lambda p: (ties[p], -p), default=None
        )
        ax, ay = (comms[anchor_index].x, comms[anchor_index].y) if anchor_index is not None else (0.0, 0.0)
        base = (comms[anchor_index].radius if anchor_index is not None else 0.0) + comm.radius + margin
        for k in range(200000):
            r = base + 10.0 * math.sqrt(k)
            x = ax + r * math.cos(k * _GOLDEN_ANGLE)
            y = ay + r * math.sin(k * _GOLDEN_ANGLE)
            if all(
                math.hypot(x - comms[p].x, y - comms[p].y) >= comm.radius + comms[p].radius + margin
                for p in placed
            ):
                comm.x, comm.y = x, y
                break
        placed.append(i)


def build_map(schema: Schema, hidden_columns: frozenset[str] = HIDDEN_BY_DEFAULT) -> SchemaMap:
    """Compute the map for ``schema``; ``hidden_columns`` = links to leave out."""
    names = [t.name for t in schema.tables]
    known = set(names)
    result = SchemaMap(total_links=len(schema.relationships))

    edges = [
        (r.child_table, r.parent_table)
        for r in schema.relationships
        if r.child_table in known and r.parent_table in known
        and r.child_table != r.parent_table
        and not _is_hidden(r, hidden_columns)
    ]
    result.shown_links = len(edges)
    result.edges = edges

    degree: dict[str, int] = defaultdict(int)
    for a, b in edges:
        degree[a] += 1
        degree[b] += 1
    connected = [n for n in names if degree[n] > 0]
    result.unconnected = [n for n in names if degree[n] == 0]

    for index, members in enumerate(_communities(connected, edges)):
        # Same ordering as _place_members, so the named hub is the one drawn
        # at the centre even when two tables tie on links.
        hub = min(members, key=lambda n: (-degree[n], n))
        comm = Community(hub=hub, members=members)
        comm.radius = _SPACING * math.sqrt(len(members)) + 6.0
        result.communities.append(comm)
        for name in members:
            result.nodes[name] = MapNode(name=name, degree=degree[name], community=index)

    weights: dict[tuple[int, int], float] = defaultdict(float)
    for a, b in edges:
        ca, cb = result.nodes[a].community, result.nodes[b].community
        if ca != cb:
            weights[(min(ca, cb), max(ca, cb))] += 1.0
    _place_communities(result.communities, weights)
    for comm in result.communities:
        _place_members(comm, result.nodes)
    return result
