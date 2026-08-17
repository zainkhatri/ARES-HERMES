# Spec: Recover the wedged 3080 + harden GPU swap for opportunistic dashboard use

**Date:** 2026-08-13
**Status:** Approved for design; execution (Part A) needs a scheduled reboot window.
**Repo:** ARES-DASHBOARD (host) + Proxmox host config (`/var/lib/vz/snippets/gpu-swap.sh`).

## Goal

Make the RTX 3080 actually available to the ARES dashboard (LXC 101) for opportunistic
GPU work — faces on new photos, CLIP embedding of bulk imports, video face detection —
while **gaming (VM 200) keeps priority**. The passthrough plumbing already exists; the
real problem is that the GPU is currently **wedged** and the swap-back recovery is
unreliable.

## Decisions (from brainstorming 2026-08-13)

- Workload that justifies GPU: **faces-on-new-photos + CLIP/video bulk**. (Full rescans
  not a priority; nightly incremental is small and CPU already handles it.)
- Coexistence: **gaming wins, dashboard opportunistic** — dashboard uses the GPU only
  when healthy and free, and falls back to CPU cleanly otherwise.
- Recover the current wedge via a **scheduled host reboot** (cleanest, ~reliable).

## Current state (verified)

- 3080 (`0000:07:00.0`) is on the `nvidia` host driver; VM 200 **stopped**; loan flag
  **off** — yet `nvidia-smi` finds nothing on host and in LXC 101.
- dmesg: repeating `NVRM: GPU 0000:07:00.0: RmInitAdapter failed! (0x31:0x40:2936)` —
  the GPU is wedged in a failed-init state. The swap script's `post-stop` soft reset
  (FLR via `echo 1 > .../reset`) ran but did **not** recover it this cycle.
- GSP firmware already disabled (`/etc/modprobe.d/nvidia.conf`: `NVreg_EnableGpuFirmware=0`;
  live "GPU Firmware: N/A") — **no change needed**.
- `reset_method` exposes `flr bus` (FLR + secondary-bus reset both available).
- VM 200 `onboot: 0` → stays stopped after a host reboot → GPU stays with host/LXC.
- App already falls back to CPU at runtime: `.gpu-on-loan` clears `CUDA_VISIBLE_DEVICES`
  at startup (`app.py:97-100`), and runtime paths use `torch.cuda.is_available()`
  (`app.py:7270`) / onnxruntime CUDA→CPU. **So "opportunistic + CPU fallback" already
  works** — the only gap is that the GPU is never healthy after gaming, and a
  present-but-wedged GPU isn't surfaced anywhere.

## What already exists (do NOT rebuild)

- `gpu-swap.sh` hookscript (VM 200/300 pre-start/post-stop): unbind nvidia → vfio → VM;
  on stop, reset + rebind nvidia + restart ares. Default GPU owner = LXC 101.
- LXC 101 config: full `/dev/nvidia*` binds + cgroup allows.
- App CPU fallback (startup flag + runtime device detection).

The swap script has a documented wedge history in its own comments (Code-43 6/16,
Gen2-link 7/11) — this is a recurring class of problem, which is why Part B hardens
recovery rather than assuming one reset always works.

---

## Part A — One-time recovery via scheduled host reboot

**Files:** none (ops procedure). **Needs:** a reboot window from the user.

Steps:
1. Pre-flight (host): confirm VM 200 stopped (`qm status 200`), loan flag absent, no
   straggler GPU procs (`lsof /dev/nvidia*`). Announce/land in the agreed window.
2. `reboot` the host.
3. Post-reboot verify, in order — each gates the next:
   - Host: `nvidia-smi -L` lists "GeForce RTX 3080".
   - dmesg clean: no `RmInitAdapter failed` since boot.
   - LXC 101: `pct exec 101 -- nvidia-smi -L` lists the 3080.
   - CUDA usable: `pct exec 101 -- /mnt/data/PROJECTS/ARES-DASHBOARD/.venv/bin/python3 -c
     "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"`.
   - End-to-end: trigger a small GPU face pass (`ops/run-facescan.sh`) and confirm the
     `--faces` phase uses `CUDAExecutionProvider` (no CUDA→CPU warning) in the log.
4. If the reboot does NOT recover it (unexpected, given GSP off): escalate to a full
   power-off/on (not warm reboot) — Ampere dirty state occasionally needs a cold cycle.

## Part B — Harden `gpu-swap.sh` post-stop (self-heal + honest state)

**Files:** Modify `/var/lib/vz/snippets/gpu-swap.sh` (`post-stop` handler only).

Today `post-stop` rebinds nvidia and clears the loan flag unconditionally — so a wedged
GPU is handed back to the app as if healthy. Change to verify-then-decide:

1. After `bind_to "$GPU_PCI" nvidia` + `ensure_nvidia_loaded`, add a health gate:
   `if timeout 10 nvidia-smi -L | grep -q "RTX 3080"; then HEALTHY=1; else HEALTHY=0; fi`
2. `HEALTHY=1`: proceed exactly as today — `rm -f "$LOAN_FLAG"`, restart ares (GPU mode),
   remove any `.gpu-wedged` marker.
3. `HEALTHY=0`: **one** bounded escalation — force a secondary-bus reset
   (`echo 1 > /sys/bus/pci/devices/$GPU_PCI/reset` after ensuring `reset_method` prefers
   `bus`; the device exposes `flr bus`), `sleep 2`, re-probe nvidia, re-check
   `nvidia-smi -L`.
   - Recovered → treat as `HEALTHY=1`.
   - Still wedged → **keep the app on CPU**: leave `.gpu-on-loan` in place (or write a
     dedicated `.gpu-force-cpu` marker the app also honors — see note), write
     `.gpu-wedged` (host path, bind-visible to LXC 101), `log` LOUDLY, restart ares so it
     comes back CPU-only. Do **not** leave the device half-bound in a way that breaks the
     next VM 200 start (ensure it's on nvidia or cleanly resettable).
4. Keep every step bounded (`timeout`) — the script's history includes unbind hangs; no
   new unbounded waits.

Marker note: the app's startup gate keys on `.gpu-on-loan`. Reusing it to mean "wedged"
is the laziest correct option (app already CPU-falls-back when it's present) — but it
conflates "lent to VM" with "broken." If that distinction matters for Part C's status,
add a separate `.gpu-force-cpu` marker and OR it into the `app.py:98` check
(one-line change). Decide during planning; default to reusing `.gpu-on-loan` unless
Part C needs the distinction.

## Part C — GPU status in dashboard vitals (small)

**Files:** Modify `app.py` (vitals/system-info route) + the vitals UI it feeds.

Surface one of: `healthy` (nvidia-smi lists the 3080, no markers), `on-loan` (VM 200
running / `.gpu-on-loan`), `wedged` (`.gpu-wedged` present). Source of truth:
`nvidia-smi -L` success + marker files. Purpose: never be silently on CPU without
knowing. Keep it a read-only status derivation — do **not** have the app manage the
loan flag (that stays owned by `gpu-swap.sh` to avoid races).

---

## Testing / verification

- Part A: the ordered post-reboot checks above; end-to-end GPU face pass with no
  CUDA→CPU fallback warning in `/var/log/ares-facescan.log`.
- Part B: simulate a wedge is hard to force safely; instead unit-test the *logic* by
  running the `post-stop` health-gate block with `nvidia-smi` stubbed to fail, and
  assert it writes `.gpu-wedged`, keeps CPU mode, and returns nonzero-free (no hang).
  Real validation: after the next actual gaming session, confirm `post-stop` either
  returns the GPU healthy or leaves a clear `.gpu-wedged` + CPU mode (check
  `/var/log/gpu-swap.log`).
- Part C: with GPU healthy → vitals shows `healthy`; `touch .gpu-wedged` → shows
  `wedged`; start VM 200 → shows `on-loan`.

## Out of scope

- Changing gaming priority or VM 200 passthrough (gaming wins — untouched).
- Dedicating the GPU to the dashboard / removing the swap.
- Faster-than-nightly incremental face scheduling (that's the separate goal #3 —
  this spec only makes the GPU *available*; scheduling is its own spec).
- Multi-GPU / eGPU / hardware changes.

## Risks

- Reboot is the reliable recovery but takes the whole box down briefly — needs a window.
- Secondary-bus reset in `post-stop` is more forceful than FLR; bounded and only on the
  already-failed path, but validate it doesn't disturb the audio function
  (`07:00.1`) or the parent bridge's other devices.
- The wedge is recurring by nature (vfio passthrough reset bug on Ampere). Part B makes
  it *survivable and visible*, not impossible — a persistent wedge still ends in "CPU +
  clear status," and the ultimate recovery remains a reboot/power-cycle.
