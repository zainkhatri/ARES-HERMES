#!/usr/bin/env python3
"""Clean up ARES face cluster emb_indices.

After porting HERMES clusters, each cluster's emb_indices contains EVERY face
from EVERY photo in the cluster — including faces of other people in
multi-person shots. That pollutes the centroid and breaks avatar picking.

Fix: for each named cluster, compute a CLEAN centroid using only faces from
single-face photos exclusive to this cluster. Then for every multi-face photo,
pick the ONE face closest to the clean centroid.
"""
import json
import os
import numpy as np
from collections import defaultdict

ARES_CLUSTERS = "/mnt/data/ares-app/ai_data/face_clusters.json"
ARES_FACE_INDEX = "/mnt/data/ares-app/ai_data/face_index.json"
ARES_EMBS = "/mnt/data/ares-app/ai_data/face_embeddings.npy"


def main():
    print("Loading...")
    with open(ARES_CLUSTERS) as f:
        clusters = json.load(f)
    with open(ARES_FACE_INDEX) as f:
        face_index = json.load(f)
    embs = np.load(ARES_EMBS)
    print(f"  {len(clusters)} clusters, {len(face_index)} face-indexed photos, {len(embs)} embeddings")

    # For each photo_hash, which named clusters claim it?
    claims = defaultdict(set)  # photo_hash -> {cluster_id}
    for cid, c in clusters.items():
        if not c.get("name"):
            continue
        for h in c.get("photo_hashes", []):
            claims[h].add(cid)

    # Pass 1: compute clean centroids
    print("\nPass 1: computing clean centroids from single-face exclusive photos...")
    centroids = {}
    for cid, c in clusters.items():
        if not c.get("name"):
            continue
        clean_embs = []
        for h in c.get("photo_hashes", []):
            if len(claims[h]) > 1:
                continue  # photo claimed by multiple people — ambiguous
            faces = face_index.get(h, [])
            if len(faces) != 1:
                continue  # zero or multi-face — ambiguous or empty
            ei = faces[0].get("emb_idx")
            if isinstance(ei, int) and 0 <= ei < len(embs):
                clean_embs.append(embs[ei])
        if not clean_embs:
            # Fallback: just single-face photos (even if claimed by others)
            for h in c.get("photo_hashes", []):
                faces = face_index.get(h, [])
                if len(faces) == 1:
                    ei = faces[0].get("emb_idx")
                    if isinstance(ei, int) and 0 <= ei < len(embs):
                        clean_embs.append(embs[ei])
        if not clean_embs:
            print(f"  [skip] {c.get('name'):<15} — no clean sample faces")
            continue
        cent = np.mean(clean_embs, axis=0)
        cent /= np.linalg.norm(cent) or 1.0
        centroids[cid] = (cent, len(clean_embs))

    # Pass 2: pick ONE face per photo based on distance to clean centroid
    print("\nPass 2: assigning best face per photo using clean centroid...")
    for cid, c in clusters.items():
        name = c.get("name", "")
        if not name or cid not in centroids:
            continue
        cent, clean_count = centroids[cid]
        chosen_indices = []
        scored_faces = []  # (sim, emb_idx, photo_hash) for picking avatar

        for h in c.get("photo_hashes", []):
            faces = face_index.get(h, [])
            if not faces:
                continue
            if len(faces) == 1:
                ei = faces[0].get("emb_idx")
                if isinstance(ei, int) and 0 <= ei < len(embs):
                    chosen_indices.append(ei)
                    sim = float(embs[ei] @ cent)
                    scored_faces.append((sim, ei, h))
                continue
            # Multi-face: pick the one closest to clean centroid
            best = (None, -1.0, None)
            for face in faces:
                ei = face.get("emb_idx")
                if not isinstance(ei, int) or ei < 0 or ei >= len(embs):
                    continue
                sim = float(embs[ei] @ cent)
                if sim > best[1]:
                    best = (ei, sim, h)
            if best[0] is not None and best[1] > 0.3:  # basic sanity floor
                chosen_indices.append(best[0])
                scored_faces.append((best[1], best[0], best[2]))

        # Sort by similarity to pick avatar = face closest to centroid
        scored_faces.sort(key=lambda x: -x[0])
        avatar_emb_idx = scored_faces[0][1] if scored_faces else None
        avatar_hash = scored_faces[0][2] if scored_faces else None

        c["emb_indices"] = chosen_indices
        c["face_count"] = len(chosen_indices)
        c["exemplars"] = [x[1] for x in scored_faces[:10]]
        # Only set avatar if the cluster doesn't already have one pinned
        # (e.g. carried over from HERMES's hand-curated avatar_hash). Otherwise
        # we'd overwrite manually chosen profile pics.
        if avatar_emb_idx is not None and not c.get("avatar_hash"):
            for face in face_index.get(avatar_hash, []):
                if face.get("emb_idx") == avatar_emb_idx:
                    c["avatar_hash"] = avatar_hash
                    c["avatar_bbox"] = face.get("bbox")
                    break
            c["sample_face"] = avatar_hash

        print(f"  {name:<15} clean_count={clean_count:>4}  final_faces={len(chosen_indices):>4}  avatar_sim={scored_faces[0][0]:.3f}" if scored_faces else f"  {name:<15} (no faces)")

    # Write
    tmp = ARES_CLUSTERS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(clusters, f)
    os.replace(tmp, ARES_CLUSTERS)
    print(f"\nUpdated {ARES_CLUSTERS}")

    # Also clear avatar cache so new ones get regenerated
    avatar_dir = "/mnt/data/ares-app/static/face_avatars"
    if os.path.isdir(avatar_dir):
        cleared = 0
        for fn in os.listdir(avatar_dir):
            try:
                os.remove(os.path.join(avatar_dir, fn))
                cleared += 1
            except Exception:
                pass
        print(f"Cleared {cleared} cached avatars")


if __name__ == "__main__":
    main()
