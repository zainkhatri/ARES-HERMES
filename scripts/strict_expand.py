#!/usr/bin/env python3
"""Strict face re-identification using HERMES labeled embeddings.

Uses industry-standard thresholds (cos sim > 0.60, margin > 0.12) and top-K
mean similarity instead of single centroid — robust to outlier exemplars.

Dedup: never removes anything. Only ADDS new photo_hashes to existing named
clusters where a face in that photo confidently matches the person.

Run WITHOUT --apply to see a preview. Add --apply to actually write.
"""
import json
import os
import sys
import numpy as np

LEGACY_CLUSTERS = "/mnt/data/ares-app/ai_data/legacy_hermes/face_clusters.json"
LEGACY_EMBS = "/mnt/data/ares-app/ai_data/legacy_hermes/face_embeddings.npy"
ARES_CLUSTERS = "/mnt/data/ares-app/ai_data/face_clusters.json"
ARES_EMBS = "/mnt/data/ares-app/ai_data/face_embeddings.npy"
ARES_FACE_INDEX = "/mnt/data/ares-app/ai_data/face_index.json"

# Strict thresholds — same-person recognition standard (ArcFace paper range)
TOP_K = 10              # top-K mean, robust to outlier exemplars
SIM_THRESHOLD = 0.60    # cosine similarity floor for any match
MARGIN = 0.12           # best must beat second-best by at least this much


def main():
    apply_changes = "--apply" in sys.argv

    print(f"{'APPLY' if apply_changes else 'DRY RUN'} mode\n")
    print("Loading legacy HERMES data...")
    with open(LEGACY_CLUSTERS) as f:
        legacy = json.load(f)
    legacy_embs = np.load(LEGACY_EMBS)

    print("Loading ARES data...")
    with open(ARES_CLUSTERS) as f:
        ares = json.load(f)
    ares_embs = np.load(ARES_EMBS)
    with open(ARES_FACE_INDEX) as f:
        face_index = json.load(f)

    print(f"  {len(legacy)} legacy clusters, {len(legacy_embs)} legacy embeddings")
    print(f"  {len(ares)} ARES clusters, {len(ares_embs)} ARES embeddings")

    # Build reference pool per named identity from HERMES labeled data
    refs = {}  # lowercase name -> (N_ref, 512) ndarray of embedding vectors
    casing = {}  # lowercase -> display name
    for cid, c in legacy.items():
        name = (c.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        casing[key] = name
        idxs = [i for i in (c.get("emb_indices") or []) if 0 <= i < len(legacy_embs)]
        if not idxs:
            continue
        pool = legacy_embs[idxs]
        if key in refs:
            refs[key] = np.concatenate([refs[key], pool])
        else:
            refs[key] = pool
    names = sorted(refs.keys(), key=lambda k: -len(refs[k]))
    print(f"  {len(names)} reference identities")

    # Map ARES emb_idx -> photo_hash (needed to assign to clusters)
    emb_to_hash = {}
    for phash, faces in face_index.items():
        for face in faces:
            ei = face.get("emb_idx")
            if isinstance(ei, int) and 0 <= ei < len(ares_embs):
                emb_to_hash[ei] = phash

    # Existing photo_hashes per named ARES cluster (for dedup)
    existing_hashes = {}    # lowercase name -> set of photo_hashes
    cid_by_name = {}        # lowercase name -> cluster id
    for cid, c in ares.items():
        n = (c.get("name") or "").strip().lower()
        if n in refs:
            existing_hashes[n] = set(c.get("photo_hashes", []))
            cid_by_name[n] = cid

    # Score all ARES embeddings against each reference pool
    # scores_matrix[i, j] = top-K mean similarity of ARES face j to person i
    print(f"\nScoring {len(ares_embs)} ARES faces vs {len(names)} identities (top-{TOP_K} mean)...")
    scores_matrix = np.full((len(names), len(ares_embs)), -1.0, dtype=np.float32)
    for i, name in enumerate(names):
        ref_embs = refs[name]
        sims = ares_embs @ ref_embs.T     # (n_ares, N_ref)  — embeddings are unit-normalized
        k = min(TOP_K, ref_embs.shape[0])
        if k < ref_embs.shape[0]:
            # np.partition puts the k largest at the end
            partitioned = np.partition(sims, -k, axis=1)
            topk = partitioned[:, -k:]
        else:
            topk = sims
        scores_matrix[i] = topk.mean(axis=1)

    # Find best + second-best per ARES face
    best_idx = np.argmax(scores_matrix, axis=0)
    ar = np.arange(len(ares_embs))
    best_score = scores_matrix[best_idx, ar]
    # Mask out best to find second-best
    masked = scores_matrix.copy()
    masked[best_idx, ar] = -np.inf
    second_score = np.max(masked, axis=0)

    confident_mask = (best_score > SIM_THRESHOLD) & ((best_score - second_score) > MARGIN)

    # Aggregate assignments per person, only counting photos not already in that cluster
    additions = {n: set() for n in names}     # name -> set of photo_hashes to add
    ambiguous = 0
    below_thresh = 0
    already_assigned = 0
    for j in range(len(ares_embs)):
        if not confident_mask[j]:
            if best_score[j] <= SIM_THRESHOLD:
                below_thresh += 1
            else:
                ambiguous += 1
            continue
        name = names[best_idx[j]]
        phash = emb_to_hash.get(j)
        if not phash:
            continue
        if phash in existing_hashes.get(name, set()):
            already_assigned += 1
            continue
        # Also guard: don't add if some other named cluster already has this photo
        # as the winning identity (keep manual curation intact — prefer existing)
        if any(phash in existing_hashes.get(other, set()) for other in existing_hashes):
            # Already labeled as someone else on HERMES — respect that curation
            continue
        additions[name].add(phash)

    # Report
    print(f"\nResults with thresholds SIM>{SIM_THRESHOLD}, MARGIN>{MARGIN}, TOP_K={TOP_K}:\n")
    print(f"  Confident same-person matches:  {int(confident_mask.sum())}")
    print(f"  Rejected — low similarity:      {below_thresh}")
    print(f"  Rejected — ambiguous (tight):   {ambiguous}")
    print(f"  Already in the right cluster:   {already_assigned}")
    total_new = sum(len(v) for v in additions.values())
    print(f"  NEW photos to add to profiles:  {total_new}\n")

    if total_new > 0:
        print(f"Per-person additions (top 25):")
        print(f"  {'NAME':<15} {'CURRENT':>8} {'+NEW':>6} {'→TOTAL':>8}")
        sorted_persons = sorted(additions.items(), key=lambda x: -len(x[1]))
        for name, new_set in sorted_persons[:25]:
            if not new_set:
                continue
            current = len(existing_hashes.get(name, set()))
            print(f"  {casing[name]:<15} {current:>8} {len(new_set):>6} {current + len(new_set):>8}")
        shown = min(25, sum(1 for _, v in sorted_persons if v))
        remaining = sum(1 for _, v in sorted_persons[shown:] if v)
        if remaining:
            print(f"  ... and {remaining} more people with additions")

    if not apply_changes:
        print(f"\nDRY RUN — no changes written. Re-run with --apply to commit.")
        return

    # APPLY: merge into face_clusters.json
    applied = 0
    for name, new_set in additions.items():
        if not new_set:
            continue
        cid = cid_by_name.get(name)
        if not cid:
            continue
        c = ares[cid]
        merged = set(c.get("photo_hashes", [])) | new_set
        c["photo_hashes"] = list(merged)
        c["photo_count"] = len(merged)
        applied += len(new_set)

    tmp = ARES_CLUSTERS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ares, f)
    os.replace(tmp, ARES_CLUSTERS)
    print(f"\nApplied: +{applied} photos across {sum(1 for v in additions.values() if v)} profiles.")
    print(f"Wrote {ARES_CLUSTERS}")


if __name__ == "__main__":
    main()
