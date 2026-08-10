#!/usr/bin/env python3
"""Merge duplicate-named face clusters in face_clusters.json.

Safety: pairwise exemplar-centroid cosine must be >= MIN_SIM for a group
to be merged. Any group that fails is skipped and reported.
"""
import json, os, sys, time
from collections import defaultdict
import numpy as np

FC_PATH = "/mnt/data/ares-app/ai_data/face_clusters.json"
MIN_SIM = 0.25  # insightface antelopev2 same-person floor; below this = different people
EXEMPLAR_KEEP = 10


def cos(a, b):
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def centroid(cluster):
    ex = cluster.get("exemplars") or []
    if not ex:
        return None
    arr = np.array(ex, dtype=np.float32)
    if arr.ndim != 2 or arr.shape[1] != 512:
        return None
    return arr.mean(axis=0)


def merge_group(clusters_by_id, ids, keeper_id):
    keeper = clusters_by_id[keeper_id]
    photo_hashes = set(keeper.get("photo_hashes") or [])
    emb_indices = list(keeper.get("emb_indices") or [])
    emb_set = set(emb_indices)
    all_exemplars = list(keeper.get("exemplars") or [])

    for cid in ids:
        if cid == keeper_id:
            continue
        c = clusters_by_id[cid]
        for ph in (c.get("photo_hashes") or []):
            photo_hashes.add(ph)
        for ei in (c.get("emb_indices") or []):
            if ei not in emb_set:
                emb_indices.append(ei)
                emb_set.add(ei)
        for e in (c.get("exemplars") or []):
            all_exemplars.append(e)

    # Pick top EXEMPLAR_KEEP exemplars closest to the merged centroid
    if all_exemplars:
        arr = np.array(all_exemplars, dtype=np.float32)
        cent = arr.mean(axis=0)
        cent_n = cent / max(np.linalg.norm(cent), 1e-9)
        sims = arr @ cent_n / (np.linalg.norm(arr, axis=1) + 1e-9)
        keep_idx = np.argsort(-sims)[:EXEMPLAR_KEEP]
        kept = arr[keep_idx].tolist()
    else:
        kept = []

    keeper["photo_hashes"] = sorted(photo_hashes)
    keeper["emb_indices"] = emb_indices
    keeper["exemplars"] = kept
    keeper["photo_count"] = len(photo_hashes)
    keeper["face_count"] = len(emb_indices)
    # sample_face, avatar_hash, avatar_bbox, name stay as keeper's


def main():
    ts = time.strftime("%Y%m%d-%H%M%S")
    with open(FC_PATH) as f:
        fc = json.load(f)
    snap = FC_PATH + f".pre-merge-{ts}"
    with open(snap, "w") as f:
        json.dump(fc, f)
    print(f"snapshot: {snap}")

    name_groups = defaultdict(list)
    for cid, c in fc.items():
        nm = (c.get("name") or "").strip().lower()
        if nm:
            name_groups[nm].append(cid)

    dupes = {n: ids for n, ids in name_groups.items() if len(ids) > 1}
    print(f"duplicate-name groups: {len(dupes)}")

    merged_groups = 0
    dropped_clusters = 0
    skipped = []

    for name, ids in sorted(dupes.items(), key=lambda kv: -sum(fc[i].get("photo_count", 0) for i in kv[1])):
        centroids = {cid: centroid(fc[cid]) for cid in ids}
        missing = [cid for cid, c in centroids.items() if c is None]
        if missing:
            skipped.append((name, ids, f"cluster(s) missing exemplars: {missing}"))
            continue
        # pairwise cosine
        min_sim = 1.0
        worst_pair = None
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                s = cos(centroids[ids[i]], centroids[ids[j]])
                if s < min_sim:
                    min_sim = s
                    worst_pair = (ids[i], ids[j])
        if min_sim < MIN_SIM:
            skipped.append((name, ids, f"cos {min_sim:.3f} < {MIN_SIM} between {worst_pair}"))
            continue

        keeper_id = max(ids, key=lambda cid: fc[cid].get("photo_count", 0))
        losers = [cid for cid in ids if cid != keeper_id]
        loser_counts = [(cid, fc[cid].get("photo_count", 0)) for cid in losers]
        print(f"[merge] {name}: keeper={keeper_id}({fc[keeper_id].get('photo_count',0)}) ← {loser_counts}  min_sim={min_sim:.3f}")
        merge_group(fc, ids, keeper_id)
        for cid in losers:
            del fc[cid]
            dropped_clusters += 1
        merged_groups += 1

    tmp = FC_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(fc, f)
    os.replace(tmp, FC_PATH)

    print()
    print(f"merged {merged_groups} groups, dropped {dropped_clusters} clusters")
    print(f"new cluster count: {len(fc)}")
    if skipped:
        print(f"skipped {len(skipped)} suspicious groups:")
        for name, ids, reason in skipped:
            counts = [(cid, fc[cid].get('photo_count', 0)) for cid in ids if cid in fc]
            print(f"  {name:25s} {counts}  — {reason}")


if __name__ == "__main__":
    main()
