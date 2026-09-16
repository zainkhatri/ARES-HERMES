# GPU swap + loan-flag reconciliation (host infra)

These files live on the **Proxmox host**, not in the running dashboard. Copies
are kept here for version control + backup. Deploy paths:

| repo copy | host path |
|---|---|
| `gpu-swap.sh` | `/var/lib/vz/snippets/gpu-swap.sh` (VM 200 `hookscript:`) |
| `gpu-loan-reconcile` | `/usr/local/sbin/gpu-loan-reconcile` (0755) |
| `gpu-loan-reconcile.service` | `/etc/systemd/system/` |
| `gpu-loan-reconcile.timer` | `/etc/systemd/system/` (enable: `systemctl enable --now`) |
| `nvidia-uvm-init.service` | `/etc/systemd/system/` (enable: `systemctl enable`) — creates /dev/nvidia-uvm before CT 101 starts |

## How it works
- `gpu-swap.sh` hands the RTX 3080 between ARES (CT 101) and VM 200 on VM
  start/stop, writing/removing `.gpu-on-loan` so the dashboard drops to CPU
  while a VM holds the card.
- `.gpu-on-loan` is a *cache* of a fact; a reboot or abrupt VM stop can orphan
  it (flag present, GPU actually free) → dashboard stuck on CPU (2026-09-15).
- `gpu-loan-reconcile` enforces **flag == reality** at boot (`OnBootSec=90s`)
  and every 3 min. Ground-truth authority = the **PCI driver binding**
  (`/sys/.../driver` == `nvidia` + no borrower VM), NOT nvidia-smi.
- Vetoes: `.gpu-force-cpu` (manual override, never touched) and `.gpu-wedged`
  (dead card, needs host reboot). flock `/run/gpu-swap.lock` serializes the
  reconciler against an in-flight swap.
