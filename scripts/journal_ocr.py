#!/usr/bin/env python3
"""
Journal OCR — Extract handwriting from journal pages using Claude Haiku Vision.

Reads journal PDFs, renders each page to JPEG, sends to Claude Haiku
for handwriting recognition, and stores extracted text in a searchable index.

Usage: python scripts/journal_ocr.py
"""

import base64
import json
import os
import sys
import time
import fitz  # PyMuPDF
import anthropic

# Load .env
_env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

API_KEY = os.environ.get("JOURNAL_OCR_KEY", "")

if sys.platform == "darwin":
    JOURNALS_DIR = "/Volumes/PROMETHEUS/PERSONAL/journals"
else:
    JOURNALS_DIR = "/srv/mergerfs/PROMETHEUS/PERSONAL/journals"

INDEX_PATH = os.path.join(JOURNALS_DIR, "journal_text_index.json")

_client = None


def get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=API_KEY)
    return _client


def ocr_image_claude(image_bytes):
    """Use Claude Haiku vision to extract handwritten text."""
    client = get_client()
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Extract ALL handwritten text from this journal page. "
                                "Transcribe exactly what is written, preserving line breaks. "
                                "If there are drawings or non-text elements, ignore them. "
                                "If the page is blank or has no readable text, respond with just: [blank page]",
                    },
                ],
            }
        ],
    )

    text = msg.content[0].text.strip()
    if text == "[blank page]":
        return ""
    return text


def render_page_jpeg(pdf_path, page_num, scale=2.0):
    """Render PDF page to JPEG, auto-downscaling for Claude's limits (5MB, 8000px max)."""
    doc = fitz.open(pdf_path)
    if page_num >= doc.page_count:
        doc.close()
        return None
    page = doc[page_num]
    rect = page.rect
    max_dim = max(rect.width, rect.height)
    # Cap scale so dimensions stay under 7500px
    scale = min(scale, 7500 / max_dim)
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
    img_bytes = pix.tobytes("jpeg", 80)
    # Reduce until base64 fits under 5MB (raw ~3.7MB)
    while len(img_bytes) > 3_700_000 and scale > 0.3:
        scale *= 0.75
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
        img_bytes = pix.tobytes("jpeg", 65)
    doc.close()
    return img_bytes


def load_index():
    if os.path.exists(INDEX_PATH):
        with open(INDEX_PATH) as f:
            return json.load(f)
    return {"version": 2, "engine": "claude-haiku-vision", "pages": {}}


def save_index(index):
    os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
    with open(INDEX_PATH, "w") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)


def main():
    if not API_KEY:
        print("ERROR: JOURNAL_OCR_KEY not set in .env")
        sys.exit(1)

    print(f"[{time.strftime('%H:%M:%S')}] Journal OCR starting (Claude Haiku Vision)...", flush=True)

    index = load_index()

    # Re-index if engine changed
    if index.get("engine") != "claude-haiku-vision":
        print("  Switching engine — re-indexing all pages with Claude Haiku", flush=True)
        index = {"version": 2, "engine": "claude-haiku-vision", "pages": {}}

    total_new = 0

    for fname in sorted(os.listdir(JOURNALS_DIR)):
        if not fname.lower().endswith(".pdf") or fname.startswith("."):
            continue

        pdf_path = os.path.join(JOURNALS_DIR, fname)
        pdf_mtime = int(os.path.getmtime(pdf_path))

        doc = fitz.open(pdf_path)
        page_count = doc.page_count
        doc.close()

        print(f"  {fname}: {page_count} pages", flush=True)

        new_pages = 0
        errors = 0
        for pg in range(page_count):
            key = f"{fname}:{pg}"
            existing = index["pages"].get(key)

            if existing and existing.get("mtime") == pdf_mtime:
                continue

            img_bytes = render_page_jpeg(pdf_path, pg, scale=2.0)
            if not img_bytes:
                continue

            try:
                text = ocr_image_claude(img_bytes)
            except anthropic.RateLimitError:
                print(f"    Rate limited at p{pg} — waiting 30s...", flush=True)
                time.sleep(30)
                try:
                    text = ocr_image_claude(img_bytes)
                except Exception as e2:
                    print(f"    Retry failed: {e2}", flush=True)
                    errors += 1
                    continue
            except Exception as e:
                errors += 1
                print(f"    Error p{pg}: {e}", flush=True)
                if errors > 10:
                    print(f"    Too many errors, skipping {fname}", flush=True)
                    break
                continue

            index["pages"][key] = {
                "pdf": fname,
                "page": pg,
                "mtime": pdf_mtime,
                "text": text,
            }
            new_pages += 1
            total_new += 1

            if new_pages % 20 == 0:
                print(f"    {new_pages} pages done...", flush=True)
                save_index(index)

        if new_pages > 0:
            print(f"    {fname}: {new_pages} new pages OCR'd", flush=True)
            save_index(index)
        else:
            print(f"    {fname}: all pages already indexed", flush=True)

    save_index(index)
    print(f"[{time.strftime('%H:%M:%S')}] Done — {total_new} new, {len(index['pages'])} total", flush=True)


if __name__ == "__main__":
    main()
