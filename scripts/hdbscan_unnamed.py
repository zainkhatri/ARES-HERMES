#!/usr/bin/env python3
"""Re-cluster the unnamed ARES face embeddings with HDBSCAN.

After self-expand assigns all confident matches to existing named clusters,
the rest are faces that don't match any known person. These are currently
bucketed into ~17 coarse "unnamed" clusters. HDBSCAN with tight params
discovers many more distinct identities for the user to label.

Output: rewrites face_clusters.json replacing unnamed clusters with finer
HDBSCAN output. Named clusters are untouched.
"""
import json
import os
import sys
import numpy as np
from collections import defaultdict

ARES_CLUSTERS = "/mnt/data/ares-app/ai_data/face_clusters.json"
ARES_EMBS = "/mnt/data/ares-app/ai_data/face_embeddings.npy"
ARES_FACE_INDEX = "/mnt/data/ares-app/ai_data/face_index.json"

MIN_CLUSTER_SIZE = 5     # minimum photos to form a new cluster
MIN_SAMPLES = 3          # core-point density
CLUSTER_SELECTION_EPSILON = 0.15  # 1 - cos sim threshold; smaller = stricter


def main():
    apply_changes = "--apply" in sys.argv
    print(f"{'APPLY' if apply_changes else 'DRY RUN'} mode\n")

    with open(ARES_CLUSTERS) as f:
        ares = json.load(f)
    embs = np.load(ARES_EMBS)
    with open(ARES_FACE_INDEX) as f:
        face_index = json.load(f)

    # All emb_idx already in a NAMED cluster
    named_embs = set()
    for cid, c in ares.items():
        if c.get("name"):
            for i in (c.get("emb_indices") or []):
                named_embs.add(i)

    # emb_idx -> photo_hash for the rest
    unnamed_indices = []
    unnamed_hashes = []
    for phash, faces in face_index.items():
        for face in faces:
            ei = face.get("emb_idx")
            if not isinstance(ei, int) or ei < 0 or ei >= len(embs):
                continue
            if ei in named_embs:
                continue
            unnamed_indices.append(ei)
            unnamed_hashes.append(phash)

    print(f"Named embeddings: {len(named_embs)}")
    print(f"Unnamed embeddings to re-cluster: {len(unnamed_indices)}")

    if not unnamed_indices:
        print("Nothing to cluster.")
        return

    X = embs[unnamed_indices]  # shape (N, 512), already L2-normalized

    try:
        import hdbscan
    except ImportError:
        print("Missing: pip install hdbscan")
        sys.exit(1)

    print(f"\nRunning HDBSCAN (min_cluster_size={MIN_CLUSTER_SIZE}, min_samples={MIN_SAMPLES})...")
    # Cosine distance ≈ 1 - dot product for unit vectors
    # HDBSCAN wants a precomputed distance or a euclidean-like metric.
    # For unit vectors, 1 - cos_sim works as a valid metric.
    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=MIN_CLUSTER_SIZE,
        min_samples=MIN_SAMPLES,
        metric="euclidean",
        cluster_selection_epsilon=CLUSTER_SELECTION_EPSILON,
        cluster_selection_method="eom",
        core_dist_n_jobs=-1,
    )
    labels = clusterer.fit_predict(X)
    # -1 = noise (outliers, not assigned to any cluster)
    unique = set(labels) - {-1}
    print(f"Discovered {len(unique)} new clusters, {int((labels == -1).sum())} noise points")

    # Aggregate: label -> (photo_hashes, emb_indices)
    groups = defaultdict(lambda: {"emb": [], "hash": set()})
    for i, lbl in enumerate(labels):
        if lbl == -1:
            continue
        groups[int(lbl)]["emb"].append(unnamed_indices[i])
        groups[int(lbl)]["hash"].add(unnamed_hashes[i])

    # Sort clusters by photo_count desc
    sorted_groups = sorted(groups.items(), key=lambda kv: -len(kv[1]["hash"]))
    print(f"\nTop 20 new clusters by size:")
    for lbl, g in sorted_groups[:20]:
        print(f"  hdb-{lbl:>3}  {len(g['hash']):>4} photos  {len(g['emb']):>4} faces")
    if len(sorted_groups) > 20:
        print(f"  ...and {len(sorted_groups)-20} more")

    if not apply_changes:
        print(f"\nDRY RUN — no changes written. Re-run with --apply to commit.")
        return

    # Rebuild face_clusters.json: keep NAMED clusters as-is, replace all
    # unnamed with HDBSCAN output
    new_clusters = {}
    # 1) Carry over named
    for cid, c in ares.items():
        if c.get("name"):
            new_clusters[cid] = c

    # 2) Append new unnamed clusters with fresh IDs
    next_id = max((int(k) for k in new_clusters.keys() if k.isdigit()), default=-1) + 1
    for lbl, g in sorted_groups:
        hashes = sorted(g["hash"])
        emb_idx_list = g["emb"]
        # Pick representative emb as exemplar: one closest to group centroid
        pool = embs[emb_idx_list]
        cent = pool.mean(axis=0)
        cent /= np.linalg.norm(cent) or 1.0
        sims = pool @ cent
        best = int(np.argmax(sims))
        sample_emb_idx = emb_idx_list[best]
        # Find that face's photo_hash
        sample_hash = None
        for phash, faces in face_index.items():
            for face in faces:
                if face.get("emb_idx") == sample_emb_idx:
                    sample_hash = phash
                    break
            if sample_hash:
                break

        new_clusters[str(next_id)] = {
            "name": "",
            "photo_count": len(hashes),
            "face_count": len(emb_idx_list),
            "sample_face": sample_hash or hashes[0],
            "photo_hashes": hashes,
            "emb_indices": emb_idx_list,
            "exemplars": emb_idx_list[:10],
        }
        next_id += 1

    tmp = ARES_CLUSTERS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(new_clusters, f)
    os.replace(tmp, ARES_CLUSTERS)
    named_count = sum(1 for v in new_clusters.values() if v.get("name"))
    print(f"\nWrote {len(new_clusters)} clusters ({named_count} named, {len(new_clusters) - named_count} unnamed)")


if __name__ == "__main__":
    main()
