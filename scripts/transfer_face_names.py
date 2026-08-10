#!/usr/bin/env python3
"""Transfer named person labels from old HERMES clusters to new ARES clusters.

Matches each old named cluster to the closest new cluster by centroid cosine
similarity. InsightFace buffalo_l embeddings are already L2-normalized so dot
product == cosine similarity.
"""
import json
import os
import numpy as np

OLD_CLUSTERS = "/mnt/data/ares-app/ai_data/legacy_hermes/face_clusters.json"
OLD_EMBS = "/mnt/data/ares-app/ai_data/legacy_hermes/face_embeddings.npy"
NEW_CLUSTERS = "/mnt/data/ares-app/ai_data/face_clusters.json"
NEW_EMBS = "/mnt/data/ares-app/ai_data/face_embeddings.npy"

# Minimum cosine similarity to accept a match (1.0 = identical, 0 = orthogonal)
# Same-person centroids across separate clusterings typically score 0.55-0.85
MIN_SIMILARITY = 0.55


def centroid(embeddings, indices):
    idxs = [i for i in indices if 0 <= i < len(embeddings)]
    if not idxs:
        return None
    v = embeddings[idxs].mean(axis=0)
    n = np.linalg.norm(v)
    return v / n if n > 0 else None


def main():
    print("Loading old data...")
    with open(OLD_CLUSTERS) as f:
        old = json.load(f)
    old_embs = np.load(OLD_EMBS)
    print(f"  {len(old)} old clusters, {len(old_embs)} old embeddings")

    print("Loading new data...")
    with open(NEW_CLUSTERS) as f:
        new = json.load(f)
    new_embs = np.load(NEW_EMBS)
    print(f"  {len(new)} new clusters, {len(new_embs)} new embeddings")

    # Compute centroids for old NAMED clusters
    old_named = {}
    for cid, c in old.items():
        if not c.get("name"):
            continue
        indices = c.get("emb_indices") or c.get("exemplars") or []
        if not indices:
            continue
        cent = centroid(old_embs, indices)
        if cent is None:
            continue
        old_named[cid] = {"name": c["name"], "centroid": cent, "count": c.get("photo_count", 0)}
    print(f"\nOld named clusters with valid centroids: {len(old_named)}")

    # Compute centroids for new clusters
    new_cents = {}
    for cid, c in new.items():
        indices = c.get("emb_indices") or c.get("exemplars") or []
        cent = centroid(new_embs, indices)
        if cent is not None:
            new_cents[cid] = cent
    print(f"New clusters with valid centroids: {len(new_cents)}")

    # For each old named, find best new match
    new_ids = list(new_cents.keys())
    new_mat = np.stack([new_cents[cid] for cid in new_ids])  # (N, 512)

    assignments = {}  # new_cid -> (old_name, similarity, old_count)
    print(f"\nMatching (min similarity: {MIN_SIMILARITY}):")
    print(f"{'OLD NAME':<15} {'OLD CT':>7}  →  {'NEW CID':<6} {'NEW CT':>7}  SIM")
    skipped = []
    # Sort by old photo count so important people match first; if the best
    # match is already taken by a stronger assignment, the next closest wins.
    for old_cid, info in sorted(old_named.items(), key=lambda x: -x[1]["count"]):
        sims = new_mat @ info["centroid"]  # dot = cosine (unit vectors)
        # Skip already-assigned new clusters
        ranked = np.argsort(-sims)
        chosen = None
        for idx in ranked:
            new_cid = new_ids[idx]
            if new_cid in assignments:
                continue
            if sims[idx] < MIN_SIMILARITY:
                break
            chosen = (new_cid, float(sims[idx]))
            break
        if chosen is None:
            skipped.append((info["name"], info["count"], float(sims[ranked[0]])))
            print(f"{info['name']:<15} {info['count']:>7}  →  (no match, best sim {float(sims[ranked[0]]):.3f})")
            continue
        new_cid, sim = chosen
        new_count = new[new_cid].get("photo_count", 0)
        assignments[new_cid] = (info["name"], sim, info["count"])
        print(f"{info['name']:<15} {info['count']:>7}  →  {new_cid:<6} {new_count:>7}  {sim:.3f}")

    # Write updated clusters
    print(f"\nAssigning {len(assignments)} names...")
    for new_cid, (name, sim, _) in assignments.items():
        new[new_cid]["name"] = name
    tmp = NEW_CLUSTERS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(new, f)
    os.replace(tmp, NEW_CLUSTERS)
    print(f"Wrote {NEW_CLUSTERS}")

    if skipped:
        print(f"\nSkipped {len(skipped)} old clusters (no confident match):")
        for name, ct, sim in skipped:
            print(f"  {name:<15} {ct:>5} (best sim {sim:.3f})")


if __name__ == "__main__":
    main()
