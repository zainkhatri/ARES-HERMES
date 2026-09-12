"""Read-only queries over the MNEMOSYNE homelab_kg.db for the dashboard KG views."""
import os, sqlite3, urllib.parse

DEFAULT_CANDS = ["/mnt/data/PROJECTS/mnemosyne/data/homelab_kg.db",
                 "/mnt/nvme/PROMETHEUS/PROJECTS/mnemosyne/data/homelab_kg.db"]

def db_path():
    cands = ([os.environ["KG_DB"]] if os.environ.get("KG_DB") else []) + DEFAULT_CANDS
    return next((p for p in cands if p and os.path.exists(p)), None)

def _conn(db):
    c = sqlite3.connect(f"file:{urllib.parse.quote(db)}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c

def _node(r):
    return {"id": r["id"], "box": r["box"], "kind": r["kind"], "name": r["name"],
            "understanding": r["understanding"] or "", "depth": r["path"].count("/")}

def overview(db, limit=150, box=None, root=None):
    c = _conn(db)
    try:
        where, args = [], []
        if box:
            boxes = [b.strip() for b in str(box).split(",") if b.strip()]
            if len(boxes) == 1:
                where.append("box=?"); args.append(boxes[0])
            elif boxes:
                where.append("box IN (%s)" % ",".join("?" * len(boxes))); args += boxes
        if root:
            where.append("(id=? OR path LIKE ?)"); args += [root, root.split(":", 1)[-1] + "/%"]
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        de = "(length(path)-length(replace(path,'/','')))"
        rows = c.execute(f"SELECT id,box,kind,name,understanding,path,{de} d FROM nodes{wsql}"
                         f" ORDER BY d ASC,name LIMIT ?", args + [limit]).fetchall()
        ids = {r["id"] for r in rows}
        nodes = [_node(r) for r in rows]
        edges = [{"src": e["src"], "dst": e["dst"], "type": e["type"]}
                 for e in c.execute("SELECT src,dst,type FROM edges").fetchall()
                 if e["src"] in ids and e["dst"] in ids]
        tn = c.execute("SELECT count(*) x FROM nodes").fetchone()["x"]
        te = c.execute("SELECT count(*) x FROM edges").fetchone()["x"]
        return {"nodes": nodes, "edges": edges, "shown": len(nodes), "total_nodes": tn, "total_edges": te}
    finally:
        c.close()

def search(db, q, limit=15):
    q = (q or "").strip()
    if not q:
        return {"results": []}
    c = _conn(db)
    try:
        try:
            rows = c.execute("SELECT n.id,n.kind,n.name,n.understanding FROM nodes_fts f"
                             " JOIN nodes n ON n.id=f.id WHERE nodes_fts MATCH ? ORDER BY rank LIMIT ?",
                             ('"' + q.replace('"', '') + '"*', limit)).fetchall()
        except sqlite3.OperationalError:
            rows = c.execute("SELECT id,kind,name,understanding FROM nodes WHERE name LIKE ? LIMIT ?",
                             ("%" + q + "%", limit)).fetchall()
        res = [{"id": r["id"], "kind": r["kind"], "name": r["name"],
                "understanding": (r["understanding"] or "")[:160]} for r in rows]
        return {"results": res}
    finally:
        c.close()

def node(db, node_id):
    c = _conn(db)
    try:
        r = c.execute("SELECT id,box,kind,name,understanding,path FROM nodes WHERE id=?", (node_id,)).fetchone()
        if not r:
            return None
        n = _node(r); n["path"] = r["path"]
        out = [{"id": e["id"], "name": e["name"], "kind": e["kind"], "type": e["type"]}
               for e in c.execute("SELECT e.type,e.dst id,m.name,m.kind FROM edges e"
                                  " JOIN nodes m ON m.id=e.dst WHERE e.src=?", (node_id,)).fetchall()]
        inn = [{"id": e["id"], "name": e["name"], "kind": e["kind"], "type": e["type"]}
               for e in c.execute("SELECT e.type,e.src id,m.name,m.kind FROM edges e"
                                 " JOIN nodes m ON m.id=e.src WHERE e.dst=?", (node_id,)).fetchall()]
        return {"node": n, "out": out, "in": inn}
    finally:
        c.close()

def children(db, node_id, limit=200):
    c = _conn(db)
    try:
        rows = c.execute("SELECT n.id,n.box,n.kind,n.name,n.understanding,n.path FROM edges e"
                         " JOIN nodes n ON n.id=e.dst WHERE e.src=? AND e.type='contains'"
                         " ORDER BY n.name LIMIT ?", (node_id, limit)).fetchall()
        nodes = [_node(r) for r in rows]
        edges = [{"src": node_id, "dst": r["id"], "type": "contains"} for r in rows]
        return {"nodes": nodes, "edges": edges}
    finally:
        c.close()
