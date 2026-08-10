#!/usr/bin/env python3
"""
embed_video_frames.py — ARES LXC CPU CLIP embedder for video frames.

Reads frames from ai_data/vidframes/, embeds them via open_clip ViT-B-32,
appends to clip_embeddings.npy and clip_hashes.json atomically.

Run on ARES LXC (has open_clip + torch installed):
    /mnt/data/ares-app/.venv/bin/python3 /mnt/data/ares-app/scripts/embed_video_frames.py

Or from Proxmox host using pct exec:
    pct exec 101 -- /mnt/data/ares-app/.venv/bin/python3 /mnt/data/ares-app/scripts/embed_video_frames.py
"""
import json, os, sys, time
import numpy as np

# Paths from ARES LXC perspective (/mnt/data is the NVMe mount)
# When running from Proxmox host directly, paths need adjustment.
# Auto-detect:
if os.path.exists("/mnt/data/ares-app"):
    APP_DIR = "/mnt/data/ares-app"
elif os.path.exists("/mnt/nvme/ares-app"):
    APP_DIR = "/mnt/nvme/ares-app"
else:
    raise RuntimeError("Cannot find ares-app directory")

AI_DIR = os.path.join(APP_DIR, "ai_data")
FRAMES_DIR = os.path.join(AI_DIR, "vidframes")
CLIP_HASHES_PATH = os.path.join(AI_DIR, "clip_hashes.json")
CLIP_EMB_PATH = os.path.join(AI_DIR, "clip_embeddings.npy")
BATCH_SIZE = 32

def main():
    if not os.path.exists(FRAMES_DIR):
        print(f"No vidframes dir at {FRAMES_DIR} — run extract_video_frames.py on VM 300 first")
        sys.exit(1)

    frames = [f for f in os.listdir(FRAMES_DIR) if f.endswith(".jpg")]
    if not frames:
        print("No frames to embed.")
        sys.exit(0)

    # Load existing index
    with open(CLIP_HASHES_PATH) as f:
        clip_hashes = json.load(f)
    existing = set(clip_hashes)

    frame_ids = [os.path.splitext(f)[0] for f in frames]
    todo_ids = [fid for fid in frame_ids if fid not in existing]
    print(f"Frames on disk: {len(frame_ids)}, already indexed: {len(frame_ids)-len(todo_ids)}, to embed: {len(todo_ids)}")
    if not todo_ids:
        print("Nothing to do.")
        return

    # Load CLIP model
    print("Loading CLIP ViT-B-32...")
    import open_clip, torch
    from PIL import Image

    model, _, preprocess = open_clip.create_model_and_transforms(
        "ViT-B-32", pretrained="laion2b_s34b_b79k"
    )
    model.eval()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = model.to(device)
    print(f"CLIP loaded on {device}")

    # Load existing embeddings
    emb_matrix = np.load(CLIP_EMB_PATH)  # shape (N, 512)
    print(f"Existing embeddings: {emb_matrix.shape}")

    new_embs = []
    new_hashes = []
    errors = 0
    t0 = time.time()

    for batch_start in range(0, len(todo_ids), BATCH_SIZE):
        batch_ids = todo_ids[batch_start:batch_start + BATCH_SIZE]
        imgs = []
        valid_ids = []

        for fid in batch_ids:
            path = os.path.join(FRAMES_DIR, fid + ".jpg")
            try:
                img = Image.open(path).convert("RGB")
                imgs.append(preprocess(img).unsqueeze(0))
                valid_ids.append(fid)
            except Exception as e:
                errors += 1
                if errors <= 5:
                    print(f"  IMG ERROR {fid}: {e}")

        if not imgs:
            continue

        batch_tensor = torch.cat(imgs, dim=0).to(device)
        with torch.no_grad():
            features = model.encode_image(batch_tensor)
            features = features / features.norm(dim=-1, keepdim=True)

        feats_np = features.cpu().float().numpy()
        for i, fid in enumerate(valid_ids):
            new_embs.append(feats_np[i])
            new_hashes.append(fid)

        done = batch_start + len(batch_ids)
        if done % 500 == 0 or done >= len(todo_ids):
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (len(todo_ids) - done) / rate if rate > 0 else 0
            print(f"  [{done}/{len(todo_ids)}] err={errors} rate={rate:.1f}/s eta={eta/60:.1f}min")
            sys.stdout.flush()

        # Checkpoint every 1000 to avoid losing progress
        if len(new_hashes) >= 1000:
            _checkpoint(clip_hashes, emb_matrix, new_hashes, new_embs)
            existing.update(new_hashes)
            clip_hashes = clip_hashes + new_hashes
            emb_matrix = np.vstack([emb_matrix] + [np.array(new_embs)])
            new_hashes = []
            new_embs = []

    if new_hashes:
        _checkpoint(clip_hashes, emb_matrix, new_hashes, new_embs)
        clip_hashes = clip_hashes + new_hashes
        emb_matrix = np.vstack([emb_matrix] + [np.array(new_embs)])

    elapsed = time.time() - t0
    print(f"\nDone. New embeddings: {len(todo_ids)-errors}, errors: {errors}, time: {elapsed/60:.1f}min")
    print(f"Final index size: {len(clip_hashes)} embeddings")
    print("Restart ARES Flask to reload the index.")


def _checkpoint(existing_hashes, existing_emb, new_hashes, new_embs):
    """Atomically append new embeddings to the index files."""
    all_hashes = existing_hashes + new_hashes
    all_emb = np.vstack([existing_emb, np.array(new_embs)])

    # Atomic write: tmp + replace
    tmp_hashes = CLIP_HASHES_PATH + ".tmp"
    with open(tmp_hashes, "w") as f:
        json.dump(all_hashes, f)
    os.replace(tmp_hashes, CLIP_HASHES_PATH)

    tmp_emb = CLIP_EMB_PATH[:-4] + ".tmp.npy"
    np.save(tmp_emb, all_emb)
    os.replace(tmp_emb, CLIP_EMB_PATH)

    print(f"  Checkpoint: {len(all_hashes)} total hashes saved")


if __name__ == "__main__":
    main()
