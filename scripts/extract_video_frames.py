#!/usr/bin/env python3
"""
extract_video_frames.py — VM 300 GPU frame extractor for CLIP indexing.

Reads photo_index.json, finds all unindexed videos, extracts one
representative frame per video via ffmpeg NVDEC, saves as JPEG to
/mnt/nvme/ares-app/ai_data/vidframes/<thumb_id>.jpg

Run from VM 300 where /mnt/nvme is NFS-mounted from the Proxmox host:
    /opt/faceenv/bin/python3 /mnt/nvme/ares-app/scripts/extract_video_frames.py

After completion, run embed_video_frames.py on ARES to embed and index.
"""
import json, os, subprocess, sys, time

AI_DIR = "/mnt/nvme/ares-app/ai_data"
FRAMES_DIR = os.path.join(AI_DIR, "vidframes")
PHOTO_INDEX = "/mnt/nvme/ares-app/photo_index.json"
CLIP_HASHES = os.path.join(AI_DIR, "clip_hashes.json")

# Path translation: LXC uses /mnt/data/, host/VM300 uses /mnt/nvme/
def translate_path(lxc_path):
    if lxc_path.startswith("/mnt/data/"):
        return "/mnt/nvme/" + lxc_path[len("/mnt/data/"):]
    return lxc_path

def thumb_id(thumb):
    return os.path.splitext(os.path.basename(thumb))[0] if thumb else None

def main():
    os.makedirs(FRAMES_DIR, exist_ok=True)

    with open(PHOTO_INDEX) as f:
        photos = json.load(f)

    with open(CLIP_HASHES) as f:
        indexed = set(json.load(f))

    videos = [p for p in photos if p.get("type") == "video"]
    todo = [v for v in videos if thumb_id(v.get("thumb","")) not in indexed]

    print(f"Videos total: {len(videos)}, already indexed: {len(videos)-len(todo)}, to extract: {len(todo)}")

    done = 0
    skipped = 0
    errors = 0
    t0 = time.time()

    for v in todo:
        tid = thumb_id(v.get("thumb",""))
        if not tid:
            skipped += 1
            continue

        out_path = os.path.join(FRAMES_DIR, tid + ".jpg")
        if os.path.exists(out_path):
            done += 1
            continue  # already extracted in a previous run

        src = translate_path(v["path"])
        if not os.path.exists(src):
            skipped += 1
            continue

        # GPU NVDEC decode, extract frame at 1s (or first keyframe if <1s)
        cmd = [
            "ffmpeg", "-y",
            "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
            "-ss", "1",
            "-i", src,
            "-vframes", "1",
            "-vf", "scale_cuda=224:224:force_original_aspect_ratio=increase,hwdownload,format=nv12",
            "-f", "image2",
            out_path,
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30)
            if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                done += 1
            else:
                # Fallback 1: CPU, -ss 1
                cmd2 = [
                    "ffmpeg", "-y",
                    "-ss", "1",
                    "-i", src,
                    "-vframes", "1",
                    "-vf", "scale=224:224:force_original_aspect_ratio=increase",
                    out_path,
                ]
                r2 = subprocess.run(cmd2, capture_output=True, timeout=30)
                if r2.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    done += 1
                else:
                    # Fallback 2: CPU, first frame (for very short clips)
                    cmd3 = [
                        "ffmpeg", "-y",
                        "-i", src,
                        "-vframes", "1",
                        "-vf", "scale=224:224:force_original_aspect_ratio=increase",
                        out_path,
                    ]
                    r3 = subprocess.run(cmd3, capture_output=True, timeout=30)
                    if r3.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                        done += 1
                    else:
                        errors += 1
                        if errors <= 5:
                            print(f"  ERROR: {src}")
                            print(f"    {r3.stderr[-200:].decode(errors='replace')}")
        except subprocess.TimeoutExpired:
            errors += 1
            print(f"  TIMEOUT: {src}")
        except Exception as e:
            errors += 1
            print(f"  EXCEPTION {src}: {e}")

        total_done = done + skipped + errors
        if total_done % 100 == 0:
            elapsed = time.time() - t0
            rate = total_done / elapsed if elapsed > 0 else 0
            remaining = (len(todo) - total_done) / rate if rate > 0 else 0
            print(f"  [{total_done}/{len(todo)}] done={done} skip={skipped} err={errors} "
                  f"rate={rate:.1f}/s eta={remaining/60:.1f}min")
            sys.stdout.flush()

    elapsed = time.time() - t0
    print(f"\nDone. extracted={done} skipped={skipped} errors={errors} in {elapsed/60:.1f}min")
    print(f"Frame files in: {FRAMES_DIR}")
    print(f"Next step: run embed_video_frames.py on ARES")

if __name__ == "__main__":
    main()
