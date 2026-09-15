#!/bin/bash
# ╔══════════════════════════════════════════════════════════════════╗
# ║  GPU SWAP HOOKSCRIPT                                             ║
# ║                                                                  ║
# ║  Default owner of the RTX 3080 → ARES LXC container (CT 101)    ║
# ║  for photo search / CLIP / future ML.                            ║
# ║                                                                  ║
# ║  When VM 200 (win11-gaming) or VM 300 (ollama-llm) starts, this ║
# ║  detaches the GPU from ARES, hands it to vfio-pci, then lets    ║
# ║  the VM consume it. On VM stop, GPU is returned to ARES.        ║
# ║                                                                  ║
# ║  One borrower at a time. If both try to start, the second fails.║
# ╚══════════════════════════════════════════════════════════════════╝

set -u

VMID="${1:-}"
PHASE="${2:-}"

ARES_CTID=101
GPU_VENDOR=10de
GPU_DEVICE=2206
AUDIO_VENDOR=10de
AUDIO_DEVICE=1aef
LOG=/var/log/gpu-swap.log

log() {
    echo "[$(date '+%F %T')] [vm:$VMID phase:$PHASE] $*" | tee -a "$LOG"
}

# 2026-09-15 incident: GPU_PCI/GPU_AUDIO_PCI used to be hardcoded bus
# addresses (0000:07:00.0/.1). A reboot re-enumerated the PCI bus, the RTX
# 3080 moved to 04:00.0, and 07:00.0 became the onboard RTL8125 2.5GbE NIC.
# The hookscript unbound the live NIC from r8169, handed it to vfio-pci, and
# qm start fed it to QEMU as hostpci0/x-vga=1 (a non-GPU device forced into
# the primary-VGA passthrough role) — that corrupted the PCIe/IOMMU state
# badly enough that the whole host hard-froze ~3.5 minutes later. Resolve by
# vendor:device ID every run instead, and abort rather than guess if the
# expected hardware isn't found — better a failed VM start than reassigning
# the wrong device.
resolve_pci_bdf() {
    local vendor="$1" device="$2" d v id
    for d in /sys/bus/pci/devices/*/; do
        v=$(cat "$d/vendor" 2>/dev/null) || continue
        id=$(cat "$d/device" 2>/dev/null) || continue
        if [ "$v" = "0x$vendor" ] && [ "$id" = "0x$device" ]; then
            basename "$d"
            return 0
        fi
    done
    return 1
}

GPU_PCI=$(resolve_pci_bdf "$GPU_VENDOR" "$GPU_DEVICE") || {
    log "ABORT: no PCI device matches GPU $GPU_VENDOR:$GPU_DEVICE — refusing to guess, not touching any hardware"
    exit 1
}
GPU_AUDIO_PCI=$(resolve_pci_bdf "$AUDIO_VENDOR" "$AUDIO_DEVICE") || {
    log "ABORT: no PCI device matches GPU audio function $AUDIO_VENDOR:$AUDIO_DEVICE — refusing to guess, not touching any hardware"
    exit 1
}

current_driver() {
    local pci="$1"
    if [ -e "/sys/bus/pci/devices/$pci/driver" ]; then
        basename "$(readlink /sys/bus/pci/devices/$pci/driver)"
    else
        echo "none"
    fi
}

unbind() {
    local pci="$1"
    local drv
    drv=$(current_driver "$pci")
    if [ -n "$drv" ] && [ "$drv" != "none" ]; then
        log "unbinding $pci from $drv"
        # The unbind write blocks while the device has users — this is what
        # hung the 2026-06-04 14:22 launch. Bound at 15s; a timeout leaves
        # the driver in place and the pre-start verification aborts cleanly.
        timeout 15 sh -c "echo '$pci' > '/sys/bus/pci/drivers/$drv/unbind'" 2>/dev/null \
            || log "WARN: unbind of $pci from $drv timed out/failed"
        # Wait briefly for the kernel to settle
        sleep 0.3
    fi
}

bind_to() {
    local pci="$1"
    local target="$2"
    local cur
    cur=$(current_driver "$pci")
    if [ "$cur" = "$target" ]; then
        log "$pci already bound to $target"
        return 0
    fi
    unbind "$pci"
    # Use driver_override + drivers_probe — most reliable across kernel versions.
    echo "$target" > "/sys/bus/pci/devices/$pci/driver_override" 2>/dev/null || true
    echo "$pci" > /sys/bus/pci/drivers_probe 2>/dev/null || true
    # Fall back to direct bind if probe didn't take.
    if [ "$(current_driver "$pci")" != "$target" ]; then
        echo "$pci" > "/sys/bus/pci/drivers/$target/bind" 2>/dev/null || true
    fi
    sleep 0.3
    log "$pci now bound to $(current_driver "$pci")"
}

ensure_vfio_ready() {
    modprobe vfio-pci 2>>"$LOG" || true
    # Register IDs in case vfio-pci doesn't auto-recognize them at bind time.
    echo "$GPU_VENDOR $GPU_DEVICE" > /sys/bus/pci/drivers/vfio-pci/new_id 2>/dev/null || true
    echo "$AUDIO_VENDOR $AUDIO_DEVICE" > /sys/bus/pci/drivers/vfio-pci/new_id 2>/dev/null || true
}

ensure_nvidia_loaded() {
    modprobe nvidia 2>>"$LOG" || true
    modprobe nvidia_uvm 2>>"$LOG" || true
    modprobe nvidia_modeset 2>>"$LOG" || true
    modprobe nvidia_drm 2>>"$LOG" || true
}

# True only when the nvidia driver can actually TALK to the GPU — not merely
# when the device node exists or the driver is bound. A card left dirty by a
# vfio guest binds fine but fails RmInitAdapter, so nvidia-smi lists nothing.
gpu_healthy() {
    timeout 10 nvidia-smi -L 2>/dev/null | grep -q "$GPU_MODEL"
}

# Escalating recovery for a card that came back wedged after a gaming session.
# A plain FLR (issued inline in post-stop) is often not enough on Ampere; try
# progressively harder re-POSTs, re-checking health after each. Power-of-Ten:
# fixed, small attempt set (no unbounded loop); every device op wrapped in a
# timeout so a wedged GPU can never hang the hook (and thus the host). Resets
# are done DRIVER-LESS (unbind first) — you can't reset a card nvidia holds.
recover_gpu_health() {
    local m
    for m in bus remove_rescan; do
        log "recovery: GPU still wedged — escalating to '$m'"
        unbind "$GPU_PCI"
        case "$m" in
            bus)
                echo bus > "/sys/bus/pci/devices/$GPU_PCI/reset_method" 2>/dev/null || true
                timeout 10 sh -c "echo 1 > /sys/bus/pci/devices/$GPU_PCI/reset" 2>/dev/null \
                    || log "WARN: bus reset write failed"
                ;;
            remove_rescan)
                # Last resort: drop the device off the bus and re-enumerate it,
                # forcing a full re-POST the way a cold boot would.
                timeout 10 sh -c "echo 1 > /sys/bus/pci/devices/$GPU_PCI/remove" 2>/dev/null \
                    || log "WARN: PCI remove failed"
                sleep 1
                timeout 15 sh -c "echo 1 > /sys/bus/pci/rescan" 2>/dev/null \
                    || log "WARN: PCI rescan failed"
                ;;
        esac
        sleep 2
        ensure_nvidia_loaded
        bind_to "$GPU_PCI"       nvidia
        bind_to "$GPU_AUDIO_PCI" snd_hda_intel
        sleep 2
        if gpu_healthy; then
            log "recovery: '$m' revived the GPU"
            return 0
        fi
    done
    return 1
}

# Flag file (host path; CT 101 sees it via the /mnt/nvme bind mount as
# /mnt/data/PROJECTS/ARES-DASHBOARD/.gpu-on-loan). While present, app.py
# sets CUDA_VISIBLE_DEVICES="" at startup so the restarted service runs
# CPU-only and never re-opens /dev/nvidia*.
LOAN_FLAG=/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/.gpu-on-loan
# Advisory marker written when the post-stop recovery ladder can't revive the
# card (persistent Ampere passthrough wedge). NOT a functional gate — the app
# already CPU-falls-back via torch.cuda.is_available() on a dead GPU — it just
# records "GPU needs a host reboot" for the log and (future) the vitals panel.
WEDGED_FLAG=/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/.gpu-wedged
GPU_MODEL="RTX 3080"

stop_ares_if_running() {
    # The old kill-based release raced systemd: Restart=on-failure respawned
    # the app within 5s, and since the CLIP startup preload the fresh process
    # grabs the GPU immediately — landing between our lsof check and the
    # driver unbind, which then hung the whole VM start (see truncated
    # pre-start entries in this log). New protocol:
    #   1. write the loan flag
    #   2. cleanly RESTART ares — it comes back CPU-only because of the flag
    #   3. sweep any non-service stragglers (manual GPU scripts)
    #   4. verify /dev/nvidia* is free, else abort the VM start
    touch "$LOAN_FLAG" 2>>"$LOG" || { log "ABORT: cannot write $LOAN_FLAG"; return 1; }
    if pct status "$ARES_CTID" 2>/dev/null | grep -q "status: running"; then
        log "loan flag set — restarting ares in CT $ARES_CTID (comes back CPU-only)"
        timeout 90 pct exec "$ARES_CTID" -- systemctl restart ares >>"$LOG" 2>&1 \
            || log "WARN: ares restart slow/failed; continuing to straggler sweep"
    else
        log "ARES container not running — only sweeping host-visible stragglers"
    fi
    # Straggler sweep: anything else still holding the device (manual
    # clip-gpu-venv runs, convert_thumbs, etc.). Bounded: TERM, wait, KILL.
    local pids
    pids=$(lsof -t /dev/nvidia* /dev/nvidiactl /dev/nvidia-uvm 2>/dev/null | sort -u | tr '\n' ' ')
    if [ -n "$pids" ]; then
        log "  straggler GPU PIDs: $pids — TERM"
        kill -TERM $pids 2>>"$LOG" || true
        sleep 2
        local remaining
        remaining=$(lsof -t /dev/nvidia* /dev/nvidiactl /dev/nvidia-uvm 2>/dev/null | sort -u | tr '\n' ' ')
        if [ -n "$remaining" ]; then
            log "  force-killing survivors: $remaining"
            kill -KILL $remaining 2>>"$LOG" || true
            sleep 1
        fi
    fi
    # Final verification — refuse to unbind while the device is held.
    if lsof /dev/nvidia* /dev/nvidiactl /dev/nvidia-uvm >/dev/null 2>&1; then
        log "ABORT: /dev/nvidia* still held after release protocol"
        return 1
    fi
    log "GPU fully released — safe to unbind"
}

start_ares_if_stopped() {
    # Patched: CT 101 stays running across swaps; only act if somehow stopped.
    if pct status "$ARES_CTID" 2>/dev/null | grep -q "status: stopped"; then
        log "starting ARES container $ARES_CTID"
        pct start "$ARES_CTID" >>"$LOG" 2>&1 || log "WARN: failed to start ARES"
    else
        log "ARES container kept alive across swap — nothing to start"
    fi
}

# Only act on the two borrower VMs.
case "$VMID" in
    200|300) ;;
    *) exit 0 ;;
esac

# Serialize the GPU-mutating phases against the gpu-loan-reconcile timer: it
# takes this same lock non-blocking and skips a tick while a swap is mid-flight,
# so it can never yank the loan flag or restart ares during a handoff. The lock
# is held for the whole phase (pre-start / post-stop); between phases the VM is
# running and the reconciler correctly reads driver=vfio-pci as "loaned".
case "$PHASE" in
    pre-start|post-stop)
        exec 8>/run/gpu-swap.lock 2>/dev/null || true
        flock -w 30 8 2>/dev/null || log "WARN: could not take gpu-swap.lock within 30s — proceeding"
        ;;
esac

case "$PHASE" in
    pre-start)
        log "vm $VMID wants the GPU — detaching from ARES"
        # Hard abort on failed release: a non-zero pre-start exit makes
        # `qm start` fail fast with a clear error instead of hanging on a
        # driver unbind that can never succeed.
        if ! stop_ares_if_running; then
            rm -f "$LOAN_FLAG"
            pct exec "$ARES_CTID" -- systemctl restart ares >>"$LOG" 2>&1 || true
            log "pre-start FAILED — loan flag rolled back, ares restored to GPU mode"
            exit 1
        fi
        # Clear driver_override so unbind/rebind work cleanly
        echo "" > "/sys/bus/pci/devices/$GPU_PCI/driver_override" 2>/dev/null || true
        echo "" > "/sys/bus/pci/devices/$GPU_AUDIO_PCI/driver_override" 2>/dev/null || true
        ensure_vfio_ready
        bind_to "$GPU_PCI"       vfio-pci
        bind_to "$GPU_AUDIO_PCI" vfio-pci
        # Verify the handoff actually happened — abort cleanly if not.
        if [ "$(current_driver "$GPU_PCI")" != "vfio-pci" ]; then
            log "ABORT: $GPU_PCI is on '$(current_driver "$GPU_PCI")', not vfio-pci"
            rm -f "$LOAN_FLAG"
            ensure_nvidia_loaded
            bind_to "$GPU_PCI"       nvidia
            bind_to "$GPU_AUDIO_PCI" snd_hda_intel
            pct exec "$ARES_CTID" -- systemctl restart ares >>"$LOG" 2>&1 || true
            log "pre-start FAILED — GPU re-bound to nvidia, ares restored"
            exit 1
        fi
        log "GPU ready for vm $VMID"
        ;;
    post-stop)
        log "vm $VMID released GPU — returning to ARES"
        # vfio-pci should release on VM exit, but force it just in case.
        unbind "$GPU_PCI"
        unbind "$GPU_AUDIO_PCI"
        echo "" > "/sys/bus/pci/devices/$GPU_PCI/driver_override" 2>/dev/null || true
        echo "" > "/sys/bus/pci/devices/$GPU_AUDIO_PCI/driver_override" 2>/dev/null || true
        # Reset the card while it is driver-less (vfio unbound, nvidia not yet
        # bound). Ampere's device_specific reset isn't kernel-supported; issue a
        # plain FLR — the cheap re-POST that works ONLY because GSP firmware is
        # disabled (NVreg_EnableGpuFirmware=0 in nvidia.conf). A single FLR is
        # NOT always enough (2026-06-16 Code-43, 2026-08-13 RmInitAdapter), so
        # the health gate below escalates (bus reset → PCI remove/rescan) when
        # the driver still can't talk to the card.
        echo flr > "/sys/bus/pci/devices/$GPU_PCI/reset_method" 2>/dev/null || true
        log "reset_method=$(cat /sys/bus/pci/devices/$GPU_PCI/reset_method 2>/dev/null) — issuing FLR"
        timeout 10 sh -c "echo 1 > /sys/bus/pci/devices/$GPU_PCI/reset" 2>/dev/null \
            || log "WARN: FLR on $GPU_PCI failed"
        sleep 1
        ensure_nvidia_loaded
        bind_to "$GPU_PCI"       nvidia
        bind_to "$GPU_AUDIO_PCI" snd_hda_intel
        sleep 2
        # Health gate: confirm the driver can actually talk to the card. If the
        # FLR+rebind wasn't enough, escalate before handing the GPU back.
        if gpu_healthy || recover_gpu_health; then
            # Healthy: end the loan + clear any stale wedge marker BEFORE
            # restarting ares so the fresh process sees CUDA (CLIP on GPU, NVENC).
            rm -f "$LOAN_FLAG" "$WEDGED_FLAG"
            start_ares_if_stopped
            timeout 90 pct exec "$ARES_CTID" -- systemctl restart ares >> "$LOG" 2>&1 \
                || log "WARN: ares restart after GPU return failed"
            log "GPU returned to ARES healthy — RTX 3080 NVENC ready"
        else
            # Persistent wedge: every reset failed. Mark it and bring ares back
            # CPU-only (it auto-falls-back via torch.cuda when the GPU is dead).
            # Recovery from here needs a host reboot / power cycle.
            log "ALERT: 3080 STILL WEDGED after FLR+bus+remove/rescan — dashboard staying on CPU"
            : > "$WEDGED_FLAG" 2>>"$LOG" || true
            rm -f "$LOAN_FLAG"
            start_ares_if_stopped
            timeout 90 pct exec "$ARES_CTID" -- systemctl restart ares >> "$LOG" 2>&1 \
                || log "WARN: ares restart (CPU mode) failed"
            log "ALERT: reboot the ARES host to recover the GPU ($WEDGED_FLAG set)"
        fi
        # ── Blank the host console after a gaming session ────────────────
        # When VM 200 exits, the GPU is back on nvidia and the host VT redraws
        # the login terminal onto the monitor. We don't want that glowing in
        # the room. Force the console into DPMS powerdown (monitor drops to
        # standby — no signal, not just a black image) and DISABLE the idle
        # blank timer (--blank 0) so that once a keypress wakes the VT it stays
        # awake while you actually use the terminal. The VT unblanks on local
        # keyboard input natively, so "off until a key is hit" needs no extra
        # wiring. Scoped to 200 (the gaming VM); 300 is headless.
        if [ "$VMID" = "200" ]; then
            CONTTY="/dev/$(cat /sys/class/tty/tty0/active 2>/dev/null)"
            if [ -w "$CONTTY" ]; then
                setterm --blank 0 --powersave powerdown --powerdown 0 > "$CONTTY" 2>&1 || true
                setterm --blank force --powersave powerdown      > "$CONTTY" 2>&1 || true
                log "host console blanked on $CONTTY (DPMS powerdown) — wakes on keypress"
            else
                log "WARN: active console $CONTTY not writable — display left on"
            fi
        fi
        ;;
    post-start)
        # ── Gaming-VM latency tuning (frametime spikes) ──────────────────
        # Scoped to win11-gaming (200). The 6 vCPUs are pinned to host cores
        # 2-7 via `affinity` in 200.conf; here we shove every OTHER qemu
        # thread (emulator, iothreads, vhost-net, vnc, rcu) onto cores 10-15
        # (the idle SMT siblings of the gaming cores) so VM-driven NVMe/net
        # interrupts fire off the gaming cores AND off cores 0-1,8-9, which the
        # host userland cpuset (Claude/MCP swarm + ARES-DASHBOARD app.py ~20%)
        # now saturates — that contention was starving vhost-net and causing
        # the 2026-06-26 jitter/lag spikes. We also flip host
        # THP defrag to defer+madvise so memory compaction runs in kcompactd
        # instead of stalling the kvm process synchronously mid-frame — the
        # 2026-06-18 "random drop to 60fps" cause (compact_stall was climbing).
        if [ "$VMID" = "200" ]; then
            echo defer+madvise > /sys/kernel/mm/transparent_hugepage/defrag 2>/dev/null || true
            QPID=$(cat "/run/qemu-server/${VMID}.pid" 2>/dev/null || true)
            if [ -n "${QPID:-}" ] && [ -d "/proc/$QPID" ]; then
                # 2026-07-12: was 10-15, but those are the SMT SIBLINGS of the
                # gaming cores 2-7 (core i = cpus i,i+8) — emulator/vhost work
                # there steals pipeline from the vCPUs on the same silicon
                # (confirmed in the 07-11 incident). 0,1,8,9 shares with host
                # userland instead; if a heavy Claude swarm ever re-starves
                # vhost-net (2026-06-26 jitter), dedicate core 1 (cpus 1,9) to
                # qemu and shrink userland to core 0 (cpus 0,8).
                for t in /proc/$QPID/task/*; do
                    tid=$(basename "$t")
                    comm=$(cat "$t/comm" 2>/dev/null || echo "")
                    case "$comm" in
                        CPU*KVM) : ;;                       # vCPUs: leave on 2-7
                        *) taskset -pc 0,1,8,9 "$tid" >/dev/null 2>&1 || true ;;
                    esac
                done
                log "latency tuning applied: non-vCPU threads -> cpu 0,1,8,9 (off vCPU SMT siblings), THP defrag=defer+madvise"
            else
                log "WARN: could not find qemu pid for latency tuning"
            fi
            # PCIe link breadcrumb: the 3080 has come back Gen2-wedged after
            # vfio resets (07-11, quarter bandwidth, fps collapse). Log the
            # trained link ~90s in (after the guest driver loads). "(downgraded)"
            # at 2.5GT/s is normal ASPM idle; 5GT/s stuck is the wedge.
            (
                sleep 90
                LNK=$(lspci -vv -s 07:00.0 2>/dev/null | grep -o 'LnkSta:.*' | head -1)
                log "pcie link check (t+90s): ${LNK:-unreadable}"
                case "$LNK" in *"5GT/s (downgraded)"*) log "WARN: 3080 looks Gen2-WEDGED — cold qm stop/start 200 retrains it (see gpu-swap-protocol memory)";; esac
            ) &
        fi
        ;;
    pre-stop)
        # nothing
        ;;
esac

exit 0
