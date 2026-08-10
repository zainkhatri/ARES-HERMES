#!/usr/bin/env python3
"""Strict face re-id using ARES's OWN antelopev2 embeddings as references.

After the antelopev2 rescan, we no longer need HERMES's buffalo_l embeddings —
each ARES named cluster has its own trusted embeddings (verified-same-person
via HERMES's photo_hash labels, then cleaned). We use those as the reference
pool for classifying unmatched ARES faces into existing named people.

Usage: python self_expand.py [--apply]
"""
import json
import os
import sys
import numpy as np

ARES_CLUSTERS = "/mnt/data/ares-app/ai_data/face_clusters.json"
ARES_EMBS = "/mnt/data/ares-app/ai_data/face_embeddings.npy"
ARES_FACE_INDEX = "/mnt/data/ares-app/ai_data/face_index.json"

TOP_K = 10
SIM_THRESHOLD = 0.60
MARGIN = 0.12


def main():
    apply_changes = "--apply" in sys.argv
    print(f"{'APPLY' if apply_changes else 'DRY RUN'} mode\n")

    with open(ARES_CLUSTERS) as f:
        ares = json.load(f)
    embs = np.load(ARES_EMBS)
    with open(ARES_FACE_INDEX) as f:
        face_index = json.load(f)
    print(f"  {len(ares)} clusters, {len(embs)} embeddings, {len(face_index)} face-indexed photos")

    # Build references from named clusters' emb_indices (each cluster's own trusted embeddings)
    refs = {}          # name -> (N_ref, 512) embedding pool
    existing = {}      # name -> set of photo_hashes
    cid_by_name = {}
    casing = {}
    for cid, c in ares.items():
        name = (c.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        casing[key] = name
        cid_by_name[key] = cid
        existing[key] = set(c.get("photo_hashes", []))
        emb_idx_list = [i for i in (c.get("emb_indices") or []) if 0 <= i < len(embs)]
        if emb_idx_list:
            refs[key] = embs[emb_idx_list]

    names = [k for k in refs if len(refs[k]) > 0]
    names.sort(key=lambda k: -len(refs[k]))
    print(f"  {len(names)} named identities with reference embeddings")

    # emb_idx -> photo_hash
    emb_to_hash = {}
    for phash, faces in face_index.items():
        for face in faces:
            ei = face.get("emb_idx")
            if isinstance(ei, int) and 0 <= ei < len(embs):
                emb_to_hash[ei] = phash

    # Score matrix (names × ares_faces) using top-K mean similarity
    print(f"\nScoring {len(embs)} ARES faces vs {len(names)} identities (top-{TOP_K} mean)...")
    scores_matrix = np.full((len(names), len(embs)), -1.0, dtype=np.float32)
    for i, name in enumerate(names):
        ref = refs[name]
        sims = embs @ ref.T
        k = min(TOP_K, ref.shape[0])
        if k < ref.shape[0]:
            topk = np.partition(sims, -k, axis=1)[:, -k:]
        else:
            topk = sims
        scores_matrix[i] = topk.mean(axis=1)

    best_idx = np.argmax(scores_matrix, axis=0)
    ar = np.arange(len(embs))
    best_score = scores_matrix[best_idx, ar]
    masked = scores_matrix.copy()
    masked[best_idx, ar] = -np.inf
    second_score = np.max(masked, axis=0)
    confident = (best_score > SIM_THRESHOLD) & ((best_score - second_score) > MARGIN)

    additions = {n: set() for n in names}
    ambig = 0
    low = 0
    noop = 0
    already_other = 0
    for j in range(len(embs)):
        if not confident[j]:
            if best_score[j] <= SIM_THRESHOLD:
                low += 1
            else:
                ambig += 1
            continue
        name = names[best_idx[j]]
        phash = emb_to_hash.get(j)
        if not phash:
            continue
        if phash in existing.get(name, set()):
            noop += 1
            continue
        # Respect existing curation — if photo belongs to a different named cluster, skip
        other_claim = any(phash in existing.get(other, set()) for other in existing if other != name)
        if other_claim:
            already_other += 1
            continue
        additions[name].add(phash)

    print(f"\nResults:")
    print(f"  Confident matches:             {int(confident.sum())}")
    print(f"  Rejected low similarity:       {low}")
    print(f"  Rejected ambiguous:            {ambig}")
    print(f"  Already in same cluster:       {noop}")
    print(f"  Claimed by other named person: {already_other}")
    total_new = sum(len(v) for v in additions.values())
    print(f"  NEW photos to add:             {total_new}\n")

    if total_new > 0:
        print(f"Per-person additions (top 25):")
        print(f"  {'NAME':<15} {'CURRENT':>8} {'+NEW':>6} {'→TOTAL':>8}")
        srt = sorted(additions.items(), key=lambda x: -len(x[1]))
        for name, new_set in srt[:25]:
            if not new_set: continue
            cur = len(existing.get(name, set()))
            print(f"  {casing[name]:<15} {cur:>8} {len(new_set):>6} {cur + len(new_set):>8}")
        remaining = sum(1 for _, v in srt[25:] if v)
        if remaining:
            print(f"  ... and {remaining} more")

    if not apply_changes:
        print(f"\nDRY RUN — no changes written. Re-run with --apply to commit.")
        return

    applied = 0
    for name, new_set in additions.items():
        if not new_set: continue
        cid = cid_by_name[name]
        c = ares[cid]
        merged = set(c.get("photo_hashes", [])) | new_set
        c["photo_hashes"] = list(merged)
        c["photo_count"] = len(merged)
        applied += len(new_set)

    tmp = ARES_CLUSTERS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ares, f)
    os.replace(tmp, ARES_CLUSTERS)
    print(f"\nApplied +{applied} photos across {sum(1 for v in additions.values() if v)} profiles")
    print(f"Wrote {ARES_CLUSTERS}")


if __name__ == "__main__":
    main()
