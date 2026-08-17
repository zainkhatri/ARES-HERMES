#!/usr/bin/env python3
"""Host collector: RTX 3080 vitals from the Windows gaming VM (200), read through
the QEMU guest agent. Used when the GPU is passed through to Windows so the host
(and VM 300) can't see it. Writes .host_gpu.json for the dashboard in LXC 101 to
read — same pattern as drive-vitals / cron-status. No-op when the VM is off or the
GPU isn't present in the guest."""
import json
import os
import subprocess
import time

VMID = "200"
OUT = "/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/.host_gpu.json"
QUERY = ("nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,"
         "memory.used,memory.total,power.draw --format=csv,noheader,nounits").split()


def _running():
    try:
        r = subprocess.run(["qm", "status", VMID], capture_output=True, text=True, timeout=6)
        return r.returncode == 0 and "running" in r.stdout
    except Exception:
        return False


def main():
    if not _running():
        return                                   # GPU is home on the host; nothing to do
    try:
        r = subprocess.run(["qm", "guest", "exec", VMID, "--timeout", "10", "--"] + QUERY,
                           capture_output=True, text=True, timeout=20)
        d = json.loads(r.stdout or "{}")
    except Exception:
        return
    if d.get("exitcode") != 0:
        return
    line = (d.get("out-data") or "").strip().splitlines()
    if not line:
        return
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"ts": int(time.time()), "csv": line[0]}, f)
    os.replace(tmp, OUT)


if __name__ == "__main__":
    main()
