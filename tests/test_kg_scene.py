"""The knowledge-graph scene is computed once on ARES and drawn as-is by the web dashboard and
the iPhone app, so both show the same nodes in the same shape and colours."""
from system import kg_scene


def _graph(extra=False):
    nodes = [{"id": f"PROMETHEUS/PROJECTS/n{i}", "kind": "folder", "name": f"n{i}"} for i in range(40)]
    edges = [{"src": f"PROMETHEUS/PROJECTS/n{i // 3}", "dst": f"PROMETHEUS/PROJECTS/n{i}"} for i in range(1, 40)]
    nodes.append({"id": "PROMETHEUS/PROJECTS/lonely", "kind": "folder", "name": "lonely"})
    if extra:
        nodes.append({"id": "PROMETHEUS/PROJECTS/new", "kind": "folder", "name": "new"})
        edges.append({"src": "PROMETHEUS/PROJECTS/n5", "dst": "PROMETHEUS/PROJECTS/new"})
    return {"nodes": nodes, "edges": edges, "total_nodes": 9001}


def test_scene_drops_unlinked_nodes_and_fits_the_unit_sphere():
    s = kg_scene.build(_graph())
    ids = [n["id"] for n in s["nodes"]]
    assert "PROMETHEUS/PROJECTS/lonely" not in ids and len(ids) == 40
    assert len(s["points"]) == len(ids) == len(s["radius"])
    assert all(sum(c * c for c in p) <= 1.0000001 for p in s["points"])
    assert all(0 <= a < len(ids) and 0 <= b < len(ids) for a, b in s["edges"])
    assert s["total_nodes"] == 9001


def test_scene_is_deterministic():
    assert kg_scene.build(_graph())["points"] == kg_scene.build(_graph())["points"]


def test_groups_and_colours_match_the_client_palettes():
    g = {"nodes": [{"id": "a", "kind": "chat", "name": "x"}, {"id": "PROMETHEUS/WORK/b", "kind": "folder", "name": "b"},
                   {"id": "EROS:c", "kind": "folder", "name": "c"}],
         "edges": [{"src": "a", "dst": "PROMETHEUS/WORK/b"}, {"src": "a", "dst": "EROS:c"}]}
    s = kg_scene.build(g)
    by = {n["id"]: n["group"] for n in s["nodes"]}
    assert by == {"a": "Claude Code", "PROMETHEUS/WORK/b": "WORK", "EROS:c": "EROS"}
    assert s["colors"]["Claude Code"] == "249,115,22" and s["colors"]["EROS"] == "250,204,21"
    assert s["colors"]["WORK"] == "239,68,68"          # first folder group, alphabetical
    assert s["buckets"] == ["Claude", "Files", "EROS"]


def test_rebuild_from_previous_keeps_nodes_in_place():
    before = kg_scene.build(_graph())
    after = kg_scene.build(_graph(extra=True), previous=before)
    was = {n["id"]: p for n, p in zip(before["nodes"], before["points"])}
    worst = max(sum((a - b) ** 2 for a, b in zip(p, was[n["id"]])) ** 0.5
                for n, p in zip(after["nodes"], after["points"]) if n["id"] in was)
    assert worst < 0.2


def test_source_fingerprint_tracks_the_graph():
    assert kg_scene.fingerprint(_graph()) == kg_scene.fingerprint(_graph())
    assert kg_scene.fingerprint(_graph()) != kg_scene.fingerprint(_graph(extra=True))
