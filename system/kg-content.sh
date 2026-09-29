#!/bin/bash
# kg-content.sh — OCR photos and extract text from documents into the homelab
# knowledge graph (see docs/superpowers/specs/2026-09-25-content-indexing-design.md
# in the atlas repo). Runs on its own timer, separate from kg-nightly.sh, so a
# slow OCR backlog across the photo library never delays the folder-summary
# reindex. Budgeted: only new/changed files are processed per run, so the
# first full pass over the library completes over many nights, not one.
# Vault-excluded paths (My Eyes Only) are never opened — see atlas/walker.py's
# default_vault_pred, reused as-is by atlas/content.py.
set -u
export HOME=/root
MNEMO="/mnt/nvme/PROMETHEUS/PROJECTS/more projects/atlas"
CENTRAL="$MNEMO/data/homelab_kg.db"
LOG=/var/log/kg-content.log
exec >>"$LOG" 2>&1

echo "=== kg-content start $(date -Is) ==="

# Priority roots first (small backlog, already mostly done — finish it off),
# then the photo library (the bulk of the real backlog), then the rest of the
# pool with whatever budget remains. Total stays ~2000/run, same sizing as
# before; the per-file fingerprint means re-walking an already-covered root
# just costs a cheap skip, never re-processing.
env KG_DB="$CENTRAL" OLLAMA_HOST=http://127.0.0.1:11434 PYTHONPATH="$MNEMO" \
  python3 -m atlas.cli index-content /mnt/nvme/PROMETHEUS/PERSONAL/id --box ARES --budget 200 \
  && echo "kg-content: PERSONAL/id ok" || echo "kg-content: PERSONAL/id FAILED"

# Documents (md/txt/pdf/docx, no images) in the work areas next. The pool-wide
# pass below spends its budget on image backlog first, which left PROJECTS and
# WORK with zero indexed docs until 2026-09-28. Text extraction is cheap.
for area in PROJECTS WORK RESUME docs; do
  env KG_DB="$CENTRAL" OLLAMA_HOST=http://127.0.0.1:11434 PYTHONPATH="$MNEMO" \
    python3 -m atlas.cli index-content "/mnt/nvme/PROMETHEUS/$area" --box ARES --budget 2000 --docs-only \
    && echo "kg-content: $area docs ok" || echo "kg-content: $area docs FAILED"
done

env KG_DB="$CENTRAL" OLLAMA_HOST=http://127.0.0.1:11434 PYTHONPATH="$MNEMO" \
  python3 -m atlas.cli index-content /mnt/nvme/PROMETHEUS/PHOTOS --box ARES --budget 5000 \
  && echo "kg-content: PHOTOS ok" || echo "kg-content: PHOTOS FAILED"

env KG_DB="$CENTRAL" OLLAMA_HOST=http://127.0.0.1:11434 PYTHONPATH="$MNEMO" \
  python3 -m atlas.cli index-content /mnt/nvme/PROMETHEUS --box ARES --budget 300 \
  && echo "kg-content: pool-wide ok" || echo "kg-content: pool-wide FAILED"

sqlite3 "$CENTRAL" "SELECT 'kg-content: file-content nodes = '||count(*) FROM nodes WHERE kind='file-content';"

echo "=== kg-content done $(date -Is) ==="
