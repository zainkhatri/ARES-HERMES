#!/usr/bin/env python3
"""Port HERMES face_clusters.json directly into ARES.

Photo thumb_hashes are md5(rel_path) which is identical on both machines, so
HERMES's cluster structure works almost 1:1. We just:
  1. Copy the HERMES clusters verbatim
  2. Filter photo_hashes to ones that exist in ARES's photo_index
  3. Rebuild emb_indices from ARES's face_index (map each photo_hash to its faces)
  4. Pick sample_face from an existing ARES photo
"""
import json
import os

LEGACY_CLUSTERS = "/mnt/data/ares-app/ai_data/legacy_hermes/face_clusters.json"
ARES_CLUSTERS = "/mnt/data/ares-app/ai_data/face_clusters.json"
ARES_FACE_INDEX = "/mnt/data/ares-app/ai_data/face_index.json"
ARES_PHOTO_INDEX = "/mnt/data/ares-app/photo_index.json"


def main():
    print("Loading...")
    with open(LEGACY_CLUSTERS) as f:
        legacy = json.load(f)
    with open(ARES_FACE_INDEX) as f:
        face_index = json.load(f)
    with open(ARES_PHOTO_INDEX) as f:
        photos = json.load(f)

    ares_hashes = set()
    for p in photos:
        t = p.get("thumb", "")
        if t:
            ares_hashes.add(t.rsplit("/", 1)[-1].rsplit(".", 1)[0])

    print(f"  {len(legacy)} HERMES clusters, {len(ares_hashes)} ARES photos, {len(face_index)} ARES face-indexed photos")

    new_clusters = {}
    total_photos = 0
    total_faces = 0
    for cid, c in legacy.items():
        photo_hashes = c.get("photo_hashes") or []
        # Filter: only keep photos that exist on ARES
        kept = [h for h in photo_hashes if h in ares_hashes]
        if not kept:
            continue

        # Rebuild emb_indices from ARES face_index
        emb_indices = []
        for h in kept:
            for face in face_index.get(h, []):
                ei = face.get("emb_idx")
                if isinstance(ei, int):
                    emb_indices.append(ei)

        # Sample face: first kept photo_hash with actual faces detected on ARES,
        # falling back to any kept hash
        sample = next((h for h in kept if face_index.get(h)), kept[0])

        # Preserve HERMES name + key metadata, rebuild derived fields for ARES
        new_clusters[cid] = {
            "name": c.get("name", ""),
            "photo_count": len(kept),
            "face_count": len(emb_indices),
            "sample_face": sample,
            "photo_hashes": kept,
            "emb_indices": emb_indices,
            "exemplars": emb_indices[:10],
        }
        # Preserve avatar override if set on HERMES
        if c.get("avatar_hash") and c["avatar_hash"] in ares_hashes:
            new_clusters[cid]["avatar_hash"] = c["avatar_hash"]
            if c.get("avatar_bbox"):
                new_clusters[cid]["avatar_bbox"] = c["avatar_bbox"]

        total_photos += len(kept)
        total_faces += len(emb_indices)

    # Re-number clusters sequentially, sorted by photo_count desc for stability
    sorted_items = sorted(new_clusters.items(), key=lambda x: -x[1]["photo_count"])
    final = {str(i): v for i, (_, v) in enumerate(sorted_items)}

    # Backup existing + write
    if os.path.exists(ARES_CLUSTERS):
        backup = ARES_CLUSTERS + ".preport-" + str(int(os.path.getmtime(ARES_CLUSTERS)))
        if not os.path.exists(backup):
            os.rename(ARES_CLUSTERS, backup)
            print(f"  Backup: {backup}")

    tmp = ARES_CLUSTERS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(final, f)
    os.replace(tmp, ARES_CLUSTERS)

    named = [(v["name"], v["photo_count"]) for v in final.values() if v["name"]]
    named.sort(key=lambda x: -x[1])
    print(f"\nPorted {len(final)} clusters ({len(named)} named, {len(final)-len(named)} unnamed)")
    print(f"Total: {total_photos} photos, {total_faces} faces\n")
    print("Named clusters (top 20):")
    for name, ct in named[:20]:
        print(f"  {name:<15} {ct:>5} photos")
    if len(named) > 20:
        print(f"  ... and {len(named)-20} more")


if __name__ == "__main__":
    main()
