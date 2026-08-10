#!/usr/bin/env python3
"""Reclassify every ARES face against old HERMES labeled centroids.

The old HERMES face_clusters.json has 50 manually named people with thousands of
labeled face embeddings each. Use those as a supervised classifier: for each
ARES face embedding, find its best-matching named identity (cosine sim) and
assign above a confidence threshold.

Then rebuild ARES face_clusters.json with one big cluster per named person
(containing ALL ARES faces classified as them) plus the existing unnamed
cluster structure for everyone else.
"""
import json
import os
import numpy as np
from collections import defaultdict

LEGACY_CLUSTERS = "/mnt/data/ares-app/ai_data/legacy_hermes/face_clusters.json"
LEGACY_EMBS = "/mnt/data/ares-app/ai_data/legacy_hermes/face_embeddings.npy"
ARES_CLUSTERS = "/mnt/data/ares-app/ai_data/face_clusters.json"
ARES_EMBS = "/mnt/data/ares-app/ai_data/face_embeddings.npy"
ARES_FACE_INDEX = "/mnt/data/ares-app/ai_data/face_index.json"

# Cosine similarity thresholds (embeddings are L2-normalized by InsightFace).
# Use top-K exemplar mean instead of single centroid — more robust to pose
# variation. An ARES face is assigned to a named person if:
#   * mean similarity to its top-5 closest labeled exemplars > CONFIDENT
#   * OR similarity to centroid > CENTROID_CONFIDENT
TOP_K = 5
CONFIDENT = 0.45
CENTROID_CONFIDENT = 0.38  # stricter-feeling because centroid averages out


def main():
    print("Loading legacy HERMES data...")
    with open(LEGACY_CLUSTERS) as f:
        legacy = json.load(f)
    legacy_embs = np.load(LEGACY_EMBS)
    print(f"  {len(legacy)} clusters, {len(legacy_embs)} embeddings")

    # Build reference pool per named person
    refs = {}  # name -> (exemplar_embs, centroid)
    for cid, c in legacy.items():
        name = (c.get("name") or "").strip().lower()
        if not name:
            continue
        indices = [i for i in (c.get("emb_indices") or []) if 0 <= i < len(legacy_embs)]
        if not indices:
            continue
        pool = legacy_embs[indices]
        cent = pool.mean(axis=0)
        cent /= np.linalg.norm(cent) or 1.0
        # Collapse duplicate names (e.g. "zahir shah" appears twice) by merging
        if name in refs:
            old_pool, _ = refs[name]
            pool = np.concatenate([old_pool, pool])
            cent = pool.mean(axis=0)
            cent /= np.linalg.norm(cent) or 1.0
        refs[name] = (pool, cent)
    print(f"  {len(refs)} named identities with exemplar pools")

    print("\nLoading ARES data...")
    with open(ARES_CLUSTERS) as f:
        ares = json.load(f)
    ares_embs = np.load(ARES_EMBS)
    with open(ARES_FACE_INDEX) as f:
        face_index = json.load(f)
    print(f"  {len(ares)} clusters, {len(ares_embs)} embeddings, {len(face_index)} face-indexed photos")

    # Build emb_idx -> photo_hash map
    emb_to_hash = {}
    for phash, faces in face_index.items():
        for face in faces:
            idx = face.get("emb_idx")
            if isinstance(idx, int) and 0 <= idx < len(ares_embs):
                emb_to_hash[idx] = phash

    # Classify every ARES embedding
    names = list(refs.keys())
    centroids = np.stack([refs[n][1] for n in names])  # (N_names, 512)

    # Pass 1: fast centroid scoring to filter obvious cases
    centroid_sims = ares_embs @ centroids.T  # (N_ares, N_names)

    # Pass 2: for each ARES face, top-K exemplar similarity (per-name) — only
    # for the top 5 centroid candidates to save time
    print(f"\nClassifying {len(ares_embs)} ARES faces against {len(names)} identities...")

    assignments = {}  # ares_emb_idx -> (name, score)
    for i in range(len(ares_embs)):
        emb = ares_embs[i]
        top_cand_idx = np.argpartition(-centroid_sims[i], 5)[:5]
        best = (None, 0.0)
        for ni in top_cand_idx:
            name = names[ni]
            pool, cent = refs[name]
            cent_sim = float(centroid_sims[i, ni])
            # Fast top-K exemplar mean
            sims = pool @ emb
            topk = np.partition(-sims, TOP_K - 1)[:TOP_K] if len(sims) >= TOP_K else sims
            topk_mean = float(-topk.mean()) if len(sims) >= TOP_K else float(sims.mean())
            # Accept if either signal is confident
            score = max(cent_sim, topk_mean)
            if (topk_mean > CONFIDENT or cent_sim > CENTROID_CONFIDENT) and score > best[1]:
                best = (name, score)
        if best[0]:
            assignments[i] = best

    print(f"\nClassified {len(assignments)}/{len(ares_embs)} faces ({100*len(assignments)//len(ares_embs)}%)")

    # Aggregate per identity
    by_name = defaultdict(list)       # name -> [ares_emb_idx...]
    name_hashes = defaultdict(set)    # name -> {photo_hash...}
    for emb_idx, (name, score) in assignments.items():
        by_name[name].append(emb_idx)
        phash = emb_to_hash.get(emb_idx)
        if phash:
            name_hashes[name].add(phash)

    # Restore original casing using legacy data
    name_casing = {}
    for cid, c in legacy.items():
        n = (c.get("name") or "").strip()
        if n:
            name_casing[n.lower()] = n

    print(f"\nPer-identity photo counts:")
    for name in sorted(by_name.keys(), key=lambda n: -len(name_hashes[n])):
        display = name_casing.get(name, name)
        print(f"  {display:<15} {len(name_hashes[name]):>5} photos  ({len(by_name[name])} faces)")

    # Rebuild ARES clusters:
    #   - For each named person, one new cluster with their aggregated faces
    #   - Unnamed ARES clusters: keep them, but DROP faces that got reassigned
    assigned_embs = set(assignments.keys())
    new_clusters = {}

    # Named clusters get new IDs starting at 0
    next_id = 0
    for name in sorted(by_name.keys(), key=lambda n: -len(name_hashes[n])):
        emb_idxs = by_name[name]
        hashes = sorted(name_hashes[name])
        # Pick exemplar = the face with highest centroid similarity
        pool, cent = refs[name]
        best_exemplar = None
        best_sim = -1.0
        for ei in emb_idxs:
            s = float(ares_embs[ei] @ cent)
            if s > best_sim:
                best_sim = s
                best_exemplar = ei
        # Sample face = photo hash of that exemplar
        sample = emb_to_hash.get(best_exemplar, hashes[0] if hashes else "")
        new_clusters[str(next_id)] = {
            "name": name_casing.get(name, name),
            "photo_count": len(hashes),
            "face_count": len(emb_idxs),
            "sample_face": sample,
            "photo_hashes": hashes,
            "emb_indices": emb_idxs,
            "exemplars": [best_exemplar] if best_exemplar is not None else [],
        }
        next_id += 1

    # Carry over unnamed ARES clusters, minus faces that got reassigned to named
    for cid, c in ares.items():
        if c.get("name"):
            continue  # skip any previously named — we just rebuilt those properly
        old_embs_list = c.get("emb_indices") or []
        remaining = [i for i in old_embs_list if i not in assigned_embs]
        if not remaining:
            continue
        remaining_hashes = sorted({emb_to_hash[i] for i in remaining if i in emb_to_hash})
        if not remaining_hashes:
            continue
        new_clusters[str(next_id)] = {
            "name": "",
            "photo_count": len(remaining_hashes),
            "face_count": len(remaining),
            "sample_face": remaining_hashes[0],
            "photo_hashes": remaining_hashes,
            "emb_indices": remaining,
            "exemplars": remaining[:5],
        }
        next_id += 1

    # Sort by photo_count desc for consistent ordering
    sorted_items = sorted(new_clusters.items(), key=lambda x: -x[1]["photo_count"])
    final = {str(i): v for i, (_, v) in enumerate(sorted_items)}

    # Backup + write
    backup = ARES_CLUSTERS + ".prereclass-" + str(int(os.path.getmtime(ARES_CLUSTERS)))
    if not os.path.exists(backup):
        os.rename(ARES_CLUSTERS, backup)
        print(f"\nBackup saved: {backup}")
    tmp = ARES_CLUSTERS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(final, f)
    os.replace(tmp, ARES_CLUSTERS)
    print(f"Wrote {len(final)} clusters to {ARES_CLUSTERS}")
    named_count = sum(1 for v in final.values() if v["name"])
    print(f"Named: {named_count}  Unnamed: {len(final) - named_count}")


if __name__ == "__main__":
    main()
