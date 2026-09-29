"""Knowledge-graph scene, computed once on ARES and drawn as-is by the web dashboard
(static/zeus-graph.js) and the iPhone app (Vitals/KGSpinView.swift), so both show the same
nodes, positions, sizes and colours.

Port of the app's KGSpinScene.build: keep the MAX_NODES best-linked nodes, dedupe and cap the
edges among them, drop nodes left without a link, lay them out in 3D (seeded Fruchterman-
Reingold) and scale so the dense core fills the unit sphere. A rebuild warm-starts from the
previous scene, so fresh data moves existing nodes very little.
"""
import hashlib
import math

import numpy as np

MAX_NODES = 400
MAX_EDGES = 600
COLD_ITERATIONS = 160
WARM_ITERATIONS = 60
SEED = 11

# Same palette as zeus-graph.js CAT_COL / FOLDER_RED / BUCKET_* and the app's KGPalette.
CAT_COL = {"Claude Code": "249,115,22", "Skills": "251,146,60", "MCP": "234,88,12",
           "Agents": "253,186,116", "Claude.ai": "194,65,12", "GPT": "240,240,240",
           "ZEUS": "56,189,248", "EROS": "250,204,21"}
FOLDER_RED = ["239,68,68", "124,20,20", "185,28,28", "252,165,165", "159,18,57", "220,38,38", "127,29,29"]
BUCKET_ORDER = ["Claude", "Files", "GPT", "ZEUS", "EROS"]
_KIND_GROUP = {"chat": "Claude Code", "gpt-chat": "GPT", "claude-chat": "Claude.ai",
               "skill": "Skills", "mcp": "MCP", "agent": "Agents"}
_NAME_GROUP = [("chatgpt", "GPT"), ("claude code", "Claude Code"), ("claude.ai", "Claude.ai"),
               ("skill", "Skills"), ("mcp server", "MCP"), ("sub-agent", "Agents")]


def group_of(n):
    """Top-level group of a node (zeus-graph.js groupOf / KGPalette.group)."""
    nid, kind, name = n.get("id") or "", n.get("kind"), (n.get("name") or "").lower()
    if nid.startswith("EROS:"):
        return "EROS"
    if kind in _KIND_GROUP:
        return _KIND_GROUP[kind]
    if kind in ("folder", "file-cluster", "project"):
        for needle, g in _NAME_GROUP:
            if needle in name:
                return g
    if "PROMETHEUS/" in nid:
        seg = nid.split("PROMETHEUS/", 1)[1].split("/", 1)[0]
        if seg:
            return seg.lstrip(".") or seg
    return n.get("name") or "root"


def bucket_of(g):
    if g in ("GPT", "ZEUS", "EROS"):
        return g
    return "Claude" if g in CAT_COL else "Files"


def group_colors(groups):
    """Fixed colours for known categories; folder groups get red shades, alphabetically."""
    out = {g: CAT_COL[g] for g in groups if g in CAT_COL}
    for i, g in enumerate(sorted(g for g in groups if g not in CAT_COL)):
        out[g] = FOLDER_RED[i % len(FOLDER_RED)]
    return out


def fingerprint(graph):
    """Stable id of the graph's content (node ids + edges)."""
    h = hashlib.sha1()
    for n in (graph.get("nodes") or [])[:100000]:
        h.update(str(n.get("id")).encode()); h.update(b"\0")
    for e in (graph.get("edges") or [])[:500000]:
        h.update(f"{e.get('src')}>{e.get('dst')}".encode()); h.update(b"\0")
    return h.hexdigest()[:16]


def _select(graph):
    """Best-linked nodes, deduped capped edges among them, and only nodes that keep a link."""
    seen, nodes = set(), []
    for n in (graph.get("nodes") or [])[:100000]:
        if n.get("id") and n["id"] not in seen:
            seen.add(n["id"]); nodes.append(n)
    edges_in = [e for e in (graph.get("edges") or [])[:500000] if e.get("src") != e.get("dst")]
    deg = {}
    for e in edges_in:
        deg[e["src"]] = deg.get(e["src"], 0) + 1
        deg[e["dst"]] = deg.get(e["dst"], 0) + 1
    order = sorted(range(len(nodes)), key=lambda i: (-deg.get(nodes[i]["id"], 0), i))
    kept = [nodes[i] for i in order[:MAX_NODES]]
    index = {n["id"]: i for i, n in enumerate(kept)}
    raw, pairs_seen = [], set()
    for e in edges_in:
        if len(raw) >= MAX_EDGES:
            break
        a, b = index.get(e["src"]), index.get(e["dst"])
        if a is None or b is None or a == b or (min(a, b), max(a, b)) in pairs_seen:
            continue
        pairs_seen.add((min(a, b), max(a, b))); raw.append((a, b))
    linked = sorted({i for p in raw for i in p})
    remap = {old: new for new, old in enumerate(linked)}
    out_nodes = [kept[i] for i in linked]
    return out_nodes, [(remap[a], remap[b]) for a, b in raw], deg


def _layout(n, pairs, start):
    """Seeded 3D Fruchterman-Reingold (KGSpin.layout), vectorised. `start` warm-starts it."""
    assert n >= 0 and all(0 <= a < n and 0 <= b < n for a, b in pairs)
    if n == 0:
        return np.zeros((0, 3))
    rng = np.random.default_rng(SEED)
    pos = rng.uniform(-1, 1, size=(n, 3))
    warm = start is not None
    if warm:
        for i, p in enumerate(start[:n]):
            if p is not None:
                pos[i] = p
    iters = WARM_ITERATIONS if warm else COLD_ITERATIONS
    k = 2.0 / math.sqrt(n); k2 = k * k
    temp = 0.03 if warm else 0.25
    cool = temp / (iters + 1)
    ea = np.array([p[0] for p in pairs], dtype=int); eb = np.array([p[1] for p in pairs], dtype=int)
    for _ in range(iters):
        d = pos[:, None, :] - pos[None, :, :]
        d2 = np.maximum((d * d).sum(-1), 1e-6)
        np.fill_diagonal(d2, np.inf)
        f = -pos * 0.6 + (d * (k2 / d2)[..., None]).sum(1)
        if len(pairs):
            ed = pos[ea] - pos[eb]
            ln = np.maximum(np.sqrt((ed * ed).sum(-1)), 1e-3)
            v = ed * (ln / k)[:, None]
            np.add.at(f, ea, -v); np.add.at(f, eb, v)
        fl = np.maximum(np.sqrt((f * f).sum(-1)), 1e-3)
        pos = pos + f * (np.minimum(fl, temp) / fl)[:, None]
        temp = max(0.002, temp - cool)
    return pos


def _normalize(pos):
    """Centre; the 85th-percentile radius sits at 0.92; outliers clamp onto the unit sphere."""
    if len(pos) == 0:
        return pos
    q = pos - pos.mean(0)
    radii = np.sort(np.sqrt((q * q).sum(-1)))
    r85 = radii[min(len(radii) - 1, int(len(radii) * 0.85))]
    ref = r85 / 0.92 if r85 > 1e-9 else radii[-1]
    q = q * (1.0 / ref if ref > 1e-9 else 0.0)
    ln = np.sqrt((q * q).sum(-1))
    over = ln > 1
    q[over] = q[over] / ln[over][:, None]
    return np.nan_to_num(q)


def _warm_start(nodes, pairs, previous):
    if not previous:
        return None
    was = {n["id"]: p for n, p in zip(previous.get("nodes", []), previous.get("points", []))}
    out = [was.get(n["id"]) for n in nodes]
    for a, b in pairs:                       # a new node starts beside a linked known one
        if out[a] is None and out[b] is not None:
            out[a] = [c * 0.97 + o for c, o in zip(out[b], (0.02, -0.015, 0.01))]
        if out[b] is None and out[a] is not None:
            out[b] = [c * 0.97 + o for c, o in zip(out[a], (-0.02, 0.015, -0.01))]
    return out


def build(graph, previous=None):
    """The scene both clients draw: nodes (with group), 3D points, radii, edges, colours."""
    nodes, pairs, deg = _select(graph)
    pts = _normalize(_layout(len(nodes), pairs, _warm_start(nodes, pairs, previous)))
    groups = [group_of(n) for n in nodes]
    colors = group_colors(set(groups))
    present = {bucket_of(g) for g in groups}
    return {
        "source": fingerprint(graph),
        "total_nodes": graph.get("total_nodes"),
        "nodes": [{"id": n["id"], "name": n.get("name"), "kind": n.get("kind"), "depth": n.get("depth"),
                   "understanding": n.get("understanding") or "", "path": n.get("path"), "group": g}
                  for n, g in zip(nodes, groups)],
        "points": [[round(float(c), 5) for c in p] for p in pts],
        "radius": [round(1.3 + min(3.2, math.sqrt(deg.get(n["id"], 0)) * 0.45), 3) for n in nodes],
        "edges": [list(p) for p in pairs],
        "colors": colors,
        "buckets": [b for b in BUCKET_ORDER if b in present],
    }
