# EROS GPU wake + Ollama offload (Phase 0+1)

Date: 2026-09-08
Status: Approved. Build follows in verified stages.

## Goal
Wake EROS's idle GTX 1070 (currently on nouveau) and move the LLM workload onto it, so ARES's RTX 3080
stops sharing itself between photos and Ollama. Dashboard LLM → local 1070; business drafting stays on
Claude (paying-client quality). Use the 8GB well — run a genuinely capable model, not a 3B.

## Fixed facts
- EROS: Proxmox VE 9 host, GTX 1070 (8GB, Pascal), on nouveau, no CUDA. 15GB RAM (~11 free). Headless on WiFi.
- **EROS is LIVE**: runs the business (FAI/FCSF/Ibtakar) + is an auto-failover target (ARES `watch-eros` fences +
  fails it over to ZEUS on a 5-min outage).
- Current LLM layout: dashboard → Ollama on ARES host (3080), models `qwen2.5:7b-instruct`, `llama3.2:3b`,
  `nomic-embed-text`. Business → cc-proxy → Claude subscription (NOT the 3080 Ollama).

## Phase 0 — GPU foundation
Driver only — **Ollama bundles its own CUDA**, so no full CUDA toolkit needed.
1. Install `pve-headers-$(uname -r)` (kernel headers for the running PVE kernel).
2. Blacklist `nouveau`; install the **NVIDIA proprietary driver** (Debian `nvidia-driver` non-free, or the
   NVIDIA .run — pick whichever builds cleanly against the PVE kernel; DKMS so it survives kernel updates).
3. Reboot; confirm `nvidia-smi` sees the 1070 and nouveau is gone.

## ⚠️ Operational safety (EROS is live) — the install is BRACKETED
The reboot drops the business ~2 min AND could trip the auto-failover. Procedure:
1. **Pause auto-failover:** `systemctl stop watch-eros.timer` on ARES.
2. Install driver + reboot EROS (business daemons auto-restart via `restart: unless-stopped`).
3. **Verify:** EROS back on WiFi, business containers up + permit fresh + healthy, `nvidia-smi` green.
4. **Re-arm:** `systemctl start watch-eros.timer` on ARES.
Do it in a quiet window (business is Claude-based; a 2-min blip is low-risk, but confirm no FCSF send is mid-flight).

## Phase 1 — Ollama on the 1070
1. Install Ollama on the EROS host (official installer); it auto-detects the NVIDIA GPU.
2. **Model — use the 8GB well.** Default to a strong 7–9B at higher quant that fits with context headroom:
   primary = `qwen2.5:7b-instruct` at **q5_K_M/q6_K** (~6–7GB) rather than q4. Also pull `nomic-embed-text`
   (embeddings) and keep a small fast model (`llama3.2:3b`) for trivial tasks. Optionally evaluate a 14B-q4
   (`qwen2.5:14b-instruct-q4` ≈ 9GB — spills a little, slower) and keep it only if tokens/sec is acceptable;
   otherwise the 7B-q5 is the daily driver. Decide by measured speed after install.
3. **Bind + secure:** Ollama listens on the LAN + tailnet (`OLLAMA_HOST=0.0.0.0:11434`), but it has NO auth —
   add a firewall rule so `:11434` is never reachable from the internet (EROS has no public IP anyway). Enable
   as a systemd service (survives reboot).
4. **Repoint the dashboard:** set `OLLAMA_HOST=http://<eros-reachable-ip>:11434` in LXC 101's env; restart the
   dashboard. **Verify LXC 101 → EROS reachability first** (LAN via ARES routing vs tailnet) — this is the one
   unknown to confirm at build time.
5. **Free the 3080:** stop + disable Ollama on ARES; confirm the 3080 no longer hosts an Ollama process.
6. **Business untouched:** cc-proxy → Claude stays exactly as-is.

## Testing (acceptance)
- `nvidia-smi` on EROS shows the 1070, nouveau unloaded, driver persists across reboot.
- `curl eros:11434/api/tags` lists the models; a generate call runs on the GPU (`nvidia-smi` shows the ollama
  process using VRAM) at acceptable tokens/sec.
- A dashboard AI feature (chat/summarize) works pointed at EROS Ollama.
- ARES: no Ollama process; 3080 shows only the photo pipeline.
- Business: containers healthy post-reboot, permit fresh, sends unaffected; auto-failover re-armed.

## Out of scope (later phases)
CLIP/face on EROS (Phase 2), Whisper/captioning/chat-over-data (Phase 3), NVENC media (Phase 4). Each gets its
own spec. VRAM budget note: with a 7B pinned (~6GB) the GPU is near-full, so Phase 2/3 will need on-demand
model loading or a smaller LLM — designed then, not now.
