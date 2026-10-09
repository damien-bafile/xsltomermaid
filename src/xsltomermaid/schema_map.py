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

from .excel_to_mermaid import AUDIT_FK_COLUMNS, Relationship, Schema, relationship_columns

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


def is_hidden_link(rel: Relationship, hidden: frozenset[str] = HIDDEN_BY_DEFAULT) -> bool:
    """Whether every column of ``rel`` is in ``hidden`` (an audit/system link)."""
    return _is_hidden(rel, hidden)


def cluster_index(schema: Schema) -> dict[str, tuple[int, str]]:
    """``{table: (cluster number, hub)}`` with the map's default settings.

    Unconnected tables are left out. Used to sort the table list by cluster.
    """
    return clusters_of(build_map(schema))


def clusters_of(m: SchemaMap) -> dict[str, tuple[int, str]]:
    """``{table: (cluster number, hub)}`` for a map already built."""
    return {
        name: (node.community, m.communities[node.community].hub)
        for name, node in m.nodes.items()
    }


def _is_hidden(rel: Relationship, hidden: frozenset[str]) -> bool:
    columns = relationship_columns(rel)
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
_GAP = 4.0  # clear space between two tables' dots
_DISC_PAD = 8.0  # between the outermost dot and the cluster's disc edge


def node_radius(degree: int) -> float:
    """A table's dot size on the map: bigger for more links."""
    return 3.0 + 1.6 * math.sqrt(degree)


def _layout_members(
    comm: Community, nodes: dict[str, MapNode], adjacency: dict[str, set[str]]
) -> dict[str, tuple[float, float]]:
    """Positions for a cluster's tables around (0, 0), grown from the hub.

    The hub goes in the middle. Then, repeatedly, the table with the most
    links to tables already placed goes as close as it fits to where those
    neighbours are (their average position), on a spiral of candidate spots
    around it, never overlapping a placed dot. Linked tables end up next to
    each other, so the cluster's sub-groups show, and its links stay short.
    Deterministic. (A sunflower spiral sorted by link count ignored who links
    to whom: links crossed the whole disc, and big dots overlapped.)
    """
    members = comm.members
    inside = {n: adjacency.get(n, set()) & set(members) for n in members}
    radius = {n: node_radius(nodes[n].degree) for n in members}
    cell = 2 * max(radius.values()) + _GAP  # grid for the overlap checks
    grid: dict[tuple[int, int], list[str]] = defaultdict(list)
    pos: dict[str, tuple[float, float]] = {}
    links_to_placed = {n: 0 for n in members}
    # Ties go to the alphabetically first name: ranked once, not per pick.
    name_rank = {n: -i for i, n in enumerate(sorted(members))}
    degree = {n: nodes[n].degree for n in members}
    unplaced = set(members)

    def fits(name: str, x: float, y: float) -> bool:
        cx, cy = int(math.floor(x / cell)), int(math.floor(y / cell))
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for other in grid.get((gx, gy), ()):
                    ox, oy = pos[other]
                    if math.hypot(x - ox, y - oy) < radius[name] + radius[other] + _GAP:
                        return False
        return True

    def place(name: str, x: float, y: float):
        pos[name] = (x, y)
        grid[(int(math.floor(x / cell)), int(math.floor(y / cell)))].append(name)
        unplaced.discard(name)
        for nbr in inside[name]:
            if nbr not in pos:
                links_to_placed[nbr] += 1

    place(comm.hub, 0.0, 0.0)
    while unplaced:
        name = max(unplaced, key=lambda n: (links_to_placed[n], degree[n], name_rank[n]))
        placed = [pos[p] for p in inside[name] if p in pos] or [(0.0, 0.0)]
        tx = sum(x for x, _ in placed) / len(placed)
        ty = sum(y for _, y in placed) / len(placed)
        step = radius[name] + 2.0  # candidate spots about one dot apart
        for k in range(100000):
            dist = step * math.sqrt(k)
            x = tx + dist * math.cos(k * _GOLDEN_ANGLE)
            y = ty + dist * math.sin(k * _GOLDEN_ANGLE)
            if fits(name, x, y):
                place(name, x, y)
                break
    return pos


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
        # Spiral steps scale with the disc: fixed steps crawled for big discs.
        step = max(10.0, comm.radius * 0.15)
        def clashes(p: int, x: float, y: float) -> bool:
            return math.hypot(x - comms[p].x, y - comms[p].y) < comm.radius + comms[p].radius + margin

        blocker = None  # the disc that blocked the last spot usually blocks the next
        for k in range(200000):
            r = base + step * math.sqrt(k)
            x = ax + r * math.cos(k * _GOLDEN_ANGLE)
            y = ay + r * math.sin(k * _GOLDEN_ANGLE)
            if blocker is not None and clashes(blocker, x, y):
                continue
            blocker = next((p for p in placed if clashes(p, x, y)), None)
            if blocker is None:
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

    adjacency: dict[str, set[str]] = defaultdict(set)
    for a, b in edges:
        adjacency[a].add(b)
        adjacency[b].add(a)

    relative: list[dict[str, tuple[float, float]]] = []
    for index, members in enumerate(_communities(connected, edges)):
        # The most-connected table names the cluster and sits at its centre.
        hub = min(members, key=lambda n: (-degree[n], n))
        comm = Community(hub=hub, members=members)
        result.communities.append(comm)
        for name in members:
            result.nodes[name] = MapNode(name=name, degree=degree[name], community=index)
        # Lay the cluster out first: its disc is sized to what it holds.
        layout = _layout_members(comm, result.nodes, adjacency)
        relative.append(layout)
        comm.radius = max(
            math.hypot(x, y) + node_radius(degree[n]) for n, (x, y) in layout.items()
        ) + _DISC_PAD

    weights: dict[tuple[int, int], float] = defaultdict(float)
    for a, b in edges:
        ca, cb = result.nodes[a].community, result.nodes[b].community
        if ca != cb:
            weights[(min(ca, cb), max(ca, cb))] += 1.0
    _place_communities(result.communities, weights)
    for comm, layout in zip(result.communities, relative):
        for name, (x, y) in layout.items():
            result.nodes[name].x = comm.x + x
            result.nodes[name].y = comm.y + y
    return result
