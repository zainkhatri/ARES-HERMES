import sqlite3
from system import kg_query

def _mkdb(tmp_path):
    p = str(tmp_path / "kg.db")
    c = sqlite3.connect(p)
    c.executescript("""
      CREATE TABLE nodes(id TEXT PRIMARY KEY, box TEXT, kind TEXT, path TEXT, name TEXT,
        understanding TEXT, fingerprint TEXT, size INTEGER, mtime INTEGER, status TEXT, meta TEXT);
      CREATE TABLE edges(src TEXT, dst TEXT, type TEXT, meta TEXT, PRIMARY KEY(src,dst,type));
      CREATE VIRTUAL TABLE nodes_fts USING fts5(id UNINDEXED, name, understanding);
    """)
    rows = [
        ("ARES:/p", "ARES", "folder", "/p", "p", "root p", "", 0, 1, "live", "{}"),
        ("ARES:/p/proj", "ARES", "project", "/p/proj", "proj", "a cool project about photos", "", 0, 1, "live", "{}"),
        ("ARES:/p/proj/src", "ARES", "folder", "/p/proj/src", "src", "source code", "", 0, 1, "live", "{}"),
    ]
    c.executemany("INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    for r in rows:
        c.execute("INSERT INTO nodes_fts(id,name,understanding) VALUES(?,?,?)", (r[0], r[4], r[5]))
    c.executemany("INSERT INTO edges VALUES(?,?,?,?)", [
        ("ARES:/p", "ARES:/p/proj", "contains", "{}"),
        ("ARES:/p/proj", "ARES:/p/proj/src", "contains", "{}"),
    ])
    c.commit(); c.close(); return p

def test_overview_returns_nodes_and_internal_edges(tmp_path):
    db = _mkdb(tmp_path)
    d = kg_query.overview(db, limit=2)
    assert d["shown"] == 2 and d["total_nodes"] == 3 and d["total_edges"] == 2
    # shallow-first: /p (depth-2) and /p/proj (depth-3) included; their edge kept
    ids = {n["id"] for n in d["nodes"]}
    assert "ARES:/p" in ids
    assert all(set(n.keys()) == {"id","box","kind","name","understanding","depth"} for n in d["nodes"])
    assert all(e["src"] in ids and e["dst"] in ids for e in d["edges"])

def test_search_matches_understanding(tmp_path):
    db = _mkdb(tmp_path)
    r = kg_query.search(db, "photos")
    assert any(x["id"] == "ARES:/p/proj" for x in r["results"])
    assert kg_query.search(db, "")["results"] == []

def test_node_with_neighbors(tmp_path):
    db = _mkdb(tmp_path)
    n = kg_query.node(db, "ARES:/p/proj")
    assert n["node"]["name"] == "proj" and n["node"]["path"] == "/p/proj"
    assert any(o["id"] == "ARES:/p/proj/src" for o in n["out"])   # child
    assert any(i["id"] == "ARES:/p" for i in n["in"])             # parent
    assert kg_query.node(db, "nope") is None

def test_children(tmp_path):
    db = _mkdb(tmp_path)
    c = kg_query.children(db, "ARES:/p")
    assert [x["id"] for x in c["nodes"]] == ["ARES:/p/proj"]
    assert c["edges"] == [{"src": "ARES:/p", "dst": "ARES:/p/proj", "type": "contains"}]
