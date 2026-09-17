#!/bin/bash
# kg-sync-eros.sh — runs on the ARES host. Ships the stdlib atlas package to
# EROS, indexes EROS's filesystem there (box=EROS, EROS-local Ollama, vault-guarded,
# bounded), pulls the db back, merges into the central graph, and links the boxes.
set -u
export HOME=/root
MNEMO="/mnt/nvme/PROMETHEUS/PROJECTS/more projects/atlas"
CENTRAL="$MNEMO/data/homelab_kg.db"
ERO=root@10.0.1.69
REMOTE=/opt/atlas
EROS_ROOTS="/root /srv /mnt"     # EROS's meaningful areas
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new"

ping -c1 -W2 10.0.1.69 >/dev/null 2>&1 || { echo "kg-sync-eros: EROS unreachable"; exit 1; }

# 1. ship the package (stdlib only, small)
rsync -a --delete -e "$SSH" "$MNEMO/atlas" "$ERO:$REMOTE/" || { echo "kg-sync-eros: rsync push failed"; exit 1; }

# 2. index each EROS root into a local eros_kg.db on EROS (fresh db each run)
$SSH "$ERO" "export HOME=/root; rm -f $REMOTE/eros_kg.db; \
  for r in $EROS_ROOTS; do [ -d \"\$r\" ] || continue; \
    KG_DB=$REMOTE/eros_kg.db KG_MAX_DEPTH=4 OLLAMA_HOST=http://127.0.0.1:11434 \
    PYTHONPATH=$REMOTE python3 -m atlas.cli reindex \"\$r\" --box EROS; done" \
  || { echo "kg-sync-eros: remote reindex failed"; exit 1; }

# 3. pull the EROS db back
rsync -a -e "$SSH" "$ERO:$REMOTE/eros_kg.db" /tmp/eros_kg.db || { echo "kg-sync-eros: rsync pull failed"; exit 1; }

# 4. merge into the central graph + link the boxes
KG_DB="$CENTRAL" PYTHONPATH="$MNEMO" python3 -m atlas.cli merge /tmp/eros_kg.db --box EROS || { echo "kg-sync-eros: merge failed"; exit 1; }
KG_DB="$CENTRAL" PYTHONPATH="$MNEMO" python3 -m atlas.cli link-boxes || { echo "kg-sync-eros: link-boxes failed"; exit 1; }
echo "kg-sync-eros: done"
