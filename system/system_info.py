"""System info gathering for ARES — runs locally on the NAS or Mac."""

import json
import os
import platform
import subprocess
import sys
import threading
import time
import psutil
from datetime import timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
FOLDER_CACHE_FILE = os.path.join(PROJECT_ROOT, ".folder_sizes.json")
DISK_CACHE_FILE = os.path.join(PROJECT_ROOT, ".disk_stats.json")
NAS_DRIVES_FILE = os.path.join(PROJECT_ROOT, ".nas_drives.json")
DISK_CACHE_TTL = 60  # seconds

IS_MAC = sys.platform == "darwin"

# ─── Pool root ───
# ARES (Linux LXC): /mnt/data/PROMETHEUS (bind-mounted from /mnt/nvme on Proxmox host)
# Mac (dev/SMB):    /Volumes/PROMETHEUS
if IS_MAC:
    POOL_ROOT = "/Volumes/PROMETHEUS"
else:
    POOL_ROOT = os.getenv("POOL_ROOT", "/mnt/data/PROMETHEUS")

# ─── Drive detection ───
# ARES: single NVMe. (The LXC sees the NVMe via bind mount; device-level stats are on the Proxmox host.)
LINUX_DRIVE_MAP = {
    "nvme0n1": "EVO-970",
}

# macOS: match by volume name
MAC_VOLUME_MAP = {
    "PROMETHEUS": "PROMETHEUS",
    "T5": "T5",
    "T7": "T7",
    "T9": "T9",
    "Samsung_T5": "T5",
    "Samsung_T7": "T7",
    "Samsung_T9": "T9",
}


def _format_bytes(b: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PiB"


HOST_DRIVES_FILE = os.path.join(PROJECT_ROOT, ".host_drives.json")
HOST_CRONS_FILE = os.path.join(PROJECT_ROOT, ".host_crons.json")


def _read_host_crons():
    """Scheduled-job status from the host collector (ops/cron-status.py). The LXC
    can't see host systemd timers, so the host writes them here. [] if missing."""
    try:
        with open(HOST_CRONS_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data.get("jobs", []) if isinstance(data, dict) else []


# --- capability profiles: which feature panels this box should show ----------
_CAPS_DEFAULTS = {
    "ARES":   {"gpu": 1, "proxmox": 1, "windows_vm": 1, "mordor": 1, "photos": 1, "journals": 1, "finance": 1, "terminal": 1, "docker": 0},
    "ZEUS": {"gpu": 0, "proxmox": 0, "windows_vm": 0, "mordor": 0, "photos": 0, "journals": 0, "finance": 1, "terminal": 1, "docker": 1},
}
_CAPS_CONSERVATIVE = {"gpu": 0, "proxmox": 0, "windows_vm": 0, "mordor": 0, "photos": 0, "journals": 0, "finance": 0, "terminal": 1, "docker": 0}


def _capabilities():
    """Feature panels this box shows. Brand-keyed defaults + optional
    CAPS="gpu=0,docker=1" env override. Fail-safe: an unset/unknown HOST_BRAND
    yields the CONSERVATIVE profile (terminal only), never full ARES access — a
    misconfigured box locks down rather than exposing GPU/photos/PVE probes.
    Both boxes set HOST_BRAND explicitly (ARES via .env, ZEUS via the unit)."""
    brand = os.getenv("HOST_BRAND", "").upper()
    caps = dict(_CAPS_DEFAULTS.get(brand, _CAPS_CONSERVATIVE))
    for pair in os.getenv("CAPS", "").split(","):          # bounded by env length
        if "=" in pair:
            k, v = pair.split("=", 1)
            caps[k.strip()] = 1 if v.strip().lower() in ("1", "true", "yes", "on") else 0
    assert isinstance(caps, dict)
    return caps


_containers_holder = {}
def _compute_containers():
    """Docker containers via `docker ps` (name, state, status). [] if docker is
    absent/unreachable. Only called when the docker capability is on."""
    try:
        r = subprocess.run(
            ["docker", "ps", "-a", "--no-trunc",
             "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"],
            capture_output=True, text=True, timeout=6,
        )
    except Exception:
        return []
    if r.returncode != 0:
        return []
    # Live CPU/mem per container (one docker stats snapshot). Best-effort.
    stats = {}
    try:
        sr = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemPerc}}"],
            capture_output=True, text=True, timeout=8,
        )
        if sr.returncode == 0:
            for sl in sr.stdout.strip().split("\n"):
                sp = sl.split("\t")
                if len(sp) == 3:
                    stats[sp[0]] = (sp[1].rstrip("%"), sp[2].rstrip("%"))
    except Exception:
        pass
    out = []
    for line in r.stdout.strip().split("\n")[:60]:         # bounded
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0]:
            continue
        cpu, mem = stats.get(parts[0], (None, None))
        try:
            cpu = round(float(cpu), 1) if cpu is not None else None
            mem = round(float(mem), 1) if mem is not None else None
        except ValueError:
            cpu = mem = None
        out.append({"name": parts[0], "state": parts[1], "status": parts[2],
                    "ok": parts[1] == "running", "cpu": cpu, "mem": mem})
    out.sort(key=lambda c: (not c["ok"], -(c.get("mem") or 0)))   # down first, then hungriest
    return out


def _get_containers():
    return _swr(_containers_holder, _compute_containers, lambda v: 8, cold=[])


def _read_host_nvme():
    """Physical NVMe drives (990 PRO + 970 EVO) from the host collector
    (ops/drive-vitals.sh). The LXC can't read device temps or true per-drive
    usage, so the host writes them here. Returns [] if missing/garbage."""
    try:
        with open(HOST_DRIVES_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for d in data.get("drives", []):
        total = d.get("total_bytes", 0)
        used = d.get("used_bytes", 0)
        out.append({
            "name": d.get("name", "?"),
            "total": _format_bytes(total),
            "used": _format_bytes(used),
            "free": _format_bytes(total - used),
            "percent": d.get("percent", 0),
            "temp_c": d.get("temp_c"),
            "_tb": total, "_ub": used,   # raw bytes so PROMETHEUS can sum the drives
        })
    return out


_io_prev = {}
def _io_rates():
    """Live network + disk throughput (bytes/s) from psutil counter deltas, plus
    load average. Keeps the previous sample across calls; first call returns zeros."""
    now = time.time()
    out = {"net_up": 0, "net_down": 0, "disk_r": 0, "disk_w": 0, "load": None, "load_pct": 0}
    try:
        n = psutil.net_io_counters()
        d = psutil.disk_io_counters()
        prev = _io_prev.get("v")
        _io_prev["v"] = (now, n.bytes_sent, n.bytes_recv,
                         getattr(d, "read_bytes", 0), getattr(d, "write_bytes", 0))
        if prev:
            dt = (now - prev[0]) or 1
            out["net_up"] = max(0, (n.bytes_sent - prev[1]) / dt)
            out["net_down"] = max(0, (n.bytes_recv - prev[2]) / dt)
            out["disk_r"] = max(0, (getattr(d, "read_bytes", 0) - prev[3]) / dt)
            out["disk_w"] = max(0, (getattr(d, "write_bytes", 0) - prev[4]) / dt)
    except Exception:
        pass
    try:
        la = os.getloadavg()
        ncpu = psutil.cpu_count() or 1
        out["load"] = [round(x, 2) for x in la]
        out["load_pct"] = round(min(100, la[0] / ncpu * 100), 1)   # 1-min load vs core count
    except Exception:
        pass
    return out


def _read_crontab_jobs():
    """ZEUS scheduled jobs from the user crontab (the FAI automation). Name from the
    script/business, schedule label, last-run from the redirected log's mtime; jobs
    with no run in >3 days are flagged. Most-recent first, capped. [] on failure."""
    import re as _re
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        lines = r.stdout.splitlines() if r.returncode == 0 else []
    except Exception:
        return []
    now = int(time.time())
    jobs = []
    for line in lines[:80]:                                   # bounded
        line = line.strip()
        if not line or line.startswith("#") or _re.match(r"^[A-Z_]+=", line):
            continue
        m = _re.match(r"^((?:[\d\*/,\-]+\s+){4}[\d\*/,\-]+)\s+(.*)$", line)
        if not m:
            continue
        sched, cmd = m.group(1), m.group(2)
        fm = _re.search(r"([\w.-]+)\.(?:sh|js|mjs|py)\b", cmd)
        base = os.path.basename(fm.group(1) if fm else cmd.split()[0]).replace("_", " ").replace("-", " ").strip(". ")
        tag = next((t for t in ("FAMILYCARESF", "FCSF", "FAI", "IBTAKAR", "whynow") if t.lower() in cmd.lower()), "")
        name = (("FCSF" if tag == "FAMILYCARESF" else tag) + ": " + base) if tag else base
        lm = _re.search(r">>?\s*(/[\w./-]+\.log)", cmd)
        last = int(os.path.getmtime(lm.group(1))) if (lm and os.path.exists(lm.group(1))) else None
        ok = last is not None and (now - last) < 3 * 86400
        jobs.append({"name": name[:38], "sched": sched, "last": last,
                     "ok": ok, "running": False, "next": None})
    jobs.sort(key=lambda j: (j["last"] is None, -(j["last"] or 0)))   # recent first, unknown last
    return jobs[:12]


def _get_disks_physical():
    """Per-physical-SSD usage for the array map (ZEUS: T5/T7/T9 behind the mergerfs
    pool). Maps each disk's model to a friendly name (trailing T5/T7/T9 token), finds
    its primary data/root mount, and reports usage. [] on any failure."""
    try:
        r = subprocess.run(["lsblk", "-J", "-b", "-o", "NAME,TYPE,MODEL,MOUNTPOINT"],
                           capture_output=True, text=True, timeout=5)
        data = json.loads(r.stdout) if r.returncode == 0 else {}
    except Exception:
        return []
    import re as _re
    out = []
    for dev in data.get("blockdevices", [])[:30]:            # bounded
        if dev.get("type") != "disk":
            continue
        model = (dev.get("model") or "").strip()
        m = _re.search(r"\b(T[0-9])\b", model)
        name = m.group(1) if m else (model.split()[-1] if model else dev.get("name", "?"))
        mount = None
        for ch in (dev.get("children") or [dev]):
            mp = ch.get("mountpoint")
            if mp and (mp.startswith("/srv") or mp == "/"):
                mount = mp
                break
        if not mount:
            continue
        try:
            u = os.statvfs(mount)
        except OSError:
            continue
        total = u.f_blocks * u.f_frsize
        used = total - (u.f_bfree * u.f_frsize)
        if total <= 0:
            continue
        out.append({"name": name, "total": _format_bytes(total),
                    "used": _format_bytes(used), "free": _format_bytes(u.f_bavail * u.f_frsize),
                    "percent": round(used / total * 100, 1),
                    "_tb": total, "_ub": used})   # raw bytes so PROMETHEUS can sum the pool
    out.sort(key=lambda d: d["name"])
    return out


def _get_disks():
    """Get disk info with time-based caching. Works on Linux NAS and macOS."""
    # Serve from cache if fresh enough
    try:
        with open(DISK_CACHE_FILE) as f:
            cache = json.load(f)
        if time.time() - cache.get("ts", 0) < DISK_CACHE_TTL:
            return cache["disks"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    if IS_MAC:
        disks = _get_disks_mac()
    else:
        disks = _get_disks_linux()

    # Persist cache
    try:
        with open(DISK_CACHE_FILE, "w") as f:
            json.dump({"ts": time.time(), "disks": disks}, f)
    except OSError:
        pass

    return disks


def _physical_disk_bytes(dev_name: str) -> int:
    """Get full physical disk capacity from /sys/block (not just the mounted partition)."""
    try:
        with open(f'/sys/block/{dev_name}/size') as f:
            return int(f.read().strip()) * 512  # sectors → bytes
    except (OSError, ValueError):
        return 0


def _make_disk_entry(mount, name, total_override: int = 0):
    """Create a disk info dict from a mount point. total_override replaces partition total with physical disk size."""
    try:
        usage = psutil.disk_usage(mount)
    except (OSError, PermissionError):
        return None
    total = total_override if total_override else usage.total
    return {
        "mount": mount,
        "name": name,
        "total": _format_bytes(total),
        "used": _format_bytes(usage.used),
        "free": _format_bytes(total - usage.used),
        "percent": round(usage.used / total * 100, 1) if total else 0,
    }


def _get_disks_mac():
    """On Mac: get PROMETHEUS pool locally, read individual drive stats from shared NAS file."""
    disks = []

    # PROMETHEUS pool — visible via SMB
    entry = _make_disk_entry("/Volumes/PROMETHEUS", "PROMETHEUS")
    if entry:
        disks.append(entry)

    # Individual NAS drives — read from .nas_drives.json (written by the NAS service)
    try:
        with open(NAS_DRIVES_FILE) as f:
            nas_data = json.load(f)
        for d in nas_data.get("drives", []):
            disks.append({
                "mount": d.get("device", ""),
                "name": d["name"],
                "total": d.get("total", "—"),
                "used": d.get("used", "—"),
                "free": d.get("free", "—"),
                "percent": d.get("percent", 0),
            })
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    return disks


def _get_disks_linux():
    """Detect drives on the Linux NAS and write results to shared file."""
    disks = []
    seen_names = set()

    # PROMETHEUS merged pool
    entry = _make_disk_entry(POOL_ROOT, "PROMETHEUS")
    if entry:
        disks.append(entry)
        seen_names.add("PROMETHEUS")

    # Physical NVMe drives (990 PRO + 970 EVO) fed by the host collector.
    nvme = _read_host_nvme()
    for d in nvme:
        if d["name"] not in seen_names:
            disks.append(d)
            seen_names.add(d["name"])

    # PROMETHEUS = ALL storage: the mount's own df only sees the 970 pool, so sum the
    # physical drives (970 + 990) for the true total/used.
    tot = sum(d.get("_tb", 0) for d in nvme)
    usd = sum(d.get("_ub", 0) for d in nvme)
    if tot > 0:
        for d in disks:
            if d.get("name") == "PROMETHEUS":
                d["total"] = _format_bytes(tot)
                d["used"] = _format_bytes(usd)
                d["free"] = _format_bytes(tot - usd)
                d["percent"] = round(usd / tot * 100, 1)
                break
    for d in nvme:                                   # drop raw helper keys
        d.pop("_tb", None); d.pop("_ub", None)

    # Individual drives by device path (also written to shared file for Mac clients)
    drive_entries = []
    for part in psutil.disk_partitions(all=True):
        mp = part.mountpoint
        dev = part.device

        name = None
        dev_frag = None
        for frag, drive_name in LINUX_DRIVE_MAP.items():
            if frag in dev:
                name = drive_name
                dev_frag = frag
                break

        if not name:
            continue

        if name in seen_names:
            continue

        phys = _physical_disk_bytes(dev_frag) if dev_frag else 0
        entry = _make_disk_entry(mp, name, total_override=phys)
        if entry:
            seen_names.add(name)
            disks.append(entry)
            drive_entries.append(entry)

    # Write individual drive stats to shared file so Mac clients can read them
    try:
        with open(NAS_DRIVES_FILE, "w") as f:
            json.dump({"ts": time.time(), "drives": drive_entries}, f)
    except OSError:
        pass

    return disks


_folder_cache = {"data": None}


def _load_folder_cache():
    try:
        with open(FOLDER_CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_folder_cache(cache):
    with open(FOLDER_CACHE_FILE, "w") as f:
        json.dump(cache, f)


def _du_single(path):
    """Get size of a single directory using du."""
    try:
        # macOS du doesn't support --block-size
        if IS_MAC:
            result = subprocess.run(
                ["du", "-sk", path],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                return int(result.stdout.split("\t")[0]) * 1024
        else:
            # Non-zero return is common & benign when vanishing tmp files trip
            # du's stat() (e.g. PROMETHEON's thumb generator). We still get a
            # correct total on stdout, so parse it regardless of exit code.
            result = subprocess.run(
                ["du", "-s", "--block-size=1", path],
                capture_output=True, text=True, timeout=120
            )
            line = (result.stdout or "").strip().splitlines()[-1] if result.stdout else ""
            if line and "\t" in line:
                try:
                    return int(line.split("\t")[0])
                except ValueError:
                    pass
    except Exception:
        pass
    return None


_foldersizes_holder = {}


def _get_folder_sizes():
    """Folder sizes, stale-while-revalidate cached (30s). The underlying du can
    take minutes on a big changed tree — keep it off the request path entirely."""
    return _swr(_foldersizes_holder, _compute_folder_sizes, lambda v: 30, cold=[])


def _compute_folder_sizes():
    """Return every top-level folder under POOL_ROOT with its du-size, sorted
    largest-first. Callers (home vitals, storage breakdown) slice as needed.
    Only re-measures folders whose mtime changed."""
    # Skip on Mac — du over SMB is painfully slow
    if IS_MAC:
        return _folder_cache.get("data") or []

    disk_cache = _load_folder_cache()

    try:
        # Skip hidden dirs, lost+found, and backup mirrors — the latter are huge
        # (du exceeds the timeout, so they never cache) and aren't "where active data
        # lives" anyway. Matches ARES, which shows active folders, not backups.
        subdirs = [d for d in os.listdir(POOL_ROOT)
                    if os.path.isdir(os.path.join(POOL_ROOT, d))
                    and not d.startswith(".") and d != "lost+found"
                    and "BACKUP" not in d.upper()]
    except OSError:
        return _folder_cache.get("data") or []

    changed = False
    for name in subdirs:
        path = os.path.join(POOL_ROOT, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue

        cached = disk_cache.get(name)
        if cached and cached.get("mtime") == mtime:
            continue
        size = _du_single(path)
        if size is not None:
            disk_cache[name] = {"size": size, "mtime": mtime}
            changed = True

    for name in list(disk_cache):
        if name not in subdirs:
            del disk_cache[name]
            changed = True

    if changed:
        _save_folder_cache(disk_cache)

    folders = []
    for name, info in disk_cache.items():
        folders.append({"name": name, "size": info["size"], "display": _format_bytes(info["size"])})
    folders.sort(key=lambda x: x["size"], reverse=True)

    try:
        usage = psutil.disk_usage(POOL_ROOT)
        for f in folders:
            f["percent"] = round(f["size"] / usage.total * 100, 1)
    except Exception:
        for f in folders:
            f["percent"] = 0

    _folder_cache["data"] = folders
    return folders


def _get_cpu_temp():
    """Read CPU temperature."""
    if IS_MAC:
        # macOS: try powermetrics or osx-cpu-temp if available
        try:
            result = subprocess.run(
                ["osx-cpu-temp"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                temp_str = result.stdout.strip().replace("°C", "")
                return round(float(temp_str), 1)
        except Exception:
            pass
        return None

    # Linux: hwmon
    hwmon_base = "/sys/class/hwmon"
    try:
        for hwmon in os.listdir(hwmon_base):
            name_path = os.path.join(hwmon_base, hwmon, "name")
            try:
                with open(name_path) as f:
                    name = f.read().strip()
            except OSError:
                continue
            if name in ("k10temp", "coretemp"):
                temp_path = os.path.join(hwmon_base, hwmon, "temp1_input")
                try:
                    with open(temp_path) as f:
                        return round(int(f.read().strip()) / 1000, 1)
                except (OSError, ValueError):
                    continue
    except OSError:
        pass
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000, 1)
    except Exception:
        return None


MORDOR_STATE_FILE = os.path.join(POOL_ROOT, "MORDOR", "server_schedule.log")


def _swr(holder, compute, ttl_for, cold=None):
    """Stale-while-revalidate cache. Returns the cached value INSTANTLY (even if
    slightly stale) and refreshes it on a background thread when older than
    ttl_for(value) seconds. `holder` is a dict; `ttl_for` is callable(value)->s.

    NEVER blocks the caller: a cold cache (first call per process, e.g. a
    request racing the startup prewarm) returns `cold` immediately and warms in
    the background — the frontend polls every 15s and renders missing values
    gracefully, so a one-poll placeholder beats a 3s hang.

    This keeps slow SSH probes (Proxmox host CPU sampling, offline-GPU connect
    timeouts) entirely off the request path, so /api/system-info stays fast."""
    now = time.time()
    ts = holder.get("ts")
    val = holder.get("val", cold)
    if (ts is None or now - ts >= ttl_for(val)) and not holder.get("refreshing"):
        holder["refreshing"] = True
        def _bg():
            try:
                v = compute()
                holder["val"] = v
                holder["ts"] = time.time()
            except Exception:
                pass
            finally:
                holder["refreshing"] = False
        threading.Thread(target=_bg, daemon=True).start()
    return val


def _compute_host_compute() -> dict:
    """SSH to the Proxmox host to read the REAL hardware stats (CPU %, memory,
    temp, model). The LXC only sees its 8GB cgroup slice — that's not the story
    the ARES dashboard wants to tell. (No caching here — wrapped by
    _get_host_compute in a stale-while-revalidate cache.)
    """
    host = os.getenv("PVE_SSH_HOST", "root@192.168.20.51")
    result = {}
    try:
        # Single SSH round-trip: take two /proc/stat samples (0.4s apart) for a
        # real CPU%, plus meminfo, plus Tctl from lm-sensors if available.
        script = r"""
A=$(awk '/^cpu / {print $2+$4":"$2+$4+$5}' /proc/stat)
sleep 0.4
B=$(awk '/^cpu / {print $2+$4":"$2+$4+$5}' /proc/stat)
echo "CPUSAMPLE $A $B"
grep -E '^(MemTotal|MemAvailable|MemFree|Buffers|Cached):' /proc/meminfo
grep -m1 '^model name' /proc/cpuinfo | sed 's/^model name[^:]*: //'
echo nproc=$(nproc)
sensors -u 2>/dev/null | awk '/(Tctl|Tccd1|temp1_input):/ {print $1, $2; exit}'
echo HOSTNAME=$(hostname)
        """
        proc = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=2", "-o", "StrictHostKeyChecking=no",
             "-o", "BatchMode=yes", "-o", "LogLevel=ERROR",
             host, script],
            capture_output=True, text=True, timeout=5,
        )
        out = proc.stdout if proc.returncode == 0 else ""
        mem = {}
        cpu_model = None
        nproc = None
        cpu_temp = None
        for line in out.splitlines():
            if line.startswith("CPUSAMPLE"):
                try:
                    _, a, b = line.split()
                    a1, a2 = a.split(":"); b1, b2 = b.split(":")
                    busy = int(b1) - int(a1); total = int(b2) - int(a2)
                    if total > 0:
                        result["cpu_percent"] = round(busy * 100 / total, 1)
                except Exception:
                    pass
            elif ":" in line and line.split(":",1)[0] in ("MemTotal", "MemAvailable", "MemFree", "Buffers", "Cached"):
                k, v = line.split(":", 1)
                try:
                    mem[k] = int(v.strip().split()[0]) * 1024  # kB → bytes
                except Exception:
                    pass
            elif line.startswith("nproc="):
                try: nproc = int(line.split("=",1)[1])
                except: pass
            elif "Tctl" in line or "Tccd1" in line or "temp1_input" in line:
                # "Tctl: 36.125"
                try:
                    cpu_temp = float(line.split()[-1])
                except Exception:
                    pass
            elif line.startswith("HOSTNAME="):
                pass
            elif line.strip() and "model name" not in line and not cpu_model and not line.startswith(("CPUSAMPLE","Mem","nproc","HOSTNAME","Tctl","Tccd","temp")):
                # First non-tagged line is the CPU model.
                cpu_model = line.strip()

        if mem.get("MemTotal") and mem.get("MemAvailable"):
            total = mem["MemTotal"]
            used = total - mem["MemAvailable"]
            result["memory_total_bytes"] = total
            result["memory_used_bytes"] = used
            result["memory_total"] = _format_bytes(total)
            result["memory_used"] = _format_bytes(used)
            result["memory_percent"] = round(used * 100 / total, 1)
        if cpu_model:
            result["cpu"] = cpu_model
            if nproc:
                result["cpu"] = f"{cpu_model} · {nproc} threads"
        if cpu_temp is not None:
            result["cpu_temp"] = round(cpu_temp, 1)
    except Exception:
        pass

    return result


_hostcompute_holder = {}
def _get_host_compute() -> dict:
    """Real Proxmox-host stats, stale-while-revalidate cached (5s). Returns
    instantly from cache; the ~0.5s SSH (it samples CPU over 0.4s) refreshes in
    the background."""
    return _swr(_hostcompute_holder, _compute_host_compute, lambda v: 5, cold={})


_hostdisks_holder = {}


def _get_host_disks() -> list:
    """Proxmox-host physical drives (AIRDISK), stale-while-revalidate cached
    (30s). Returns instantly from cache; the SSH probe refreshes in the
    background so it never blocks /api/system-info."""
    return _swr(_hostdisks_holder, _compute_host_disks, lambda v: 30, cold=[])


def _compute_host_disks() -> list:
    """SSH to the Proxmox host for the AIRDISK boot-SSD stats (pve-root +
    LVM-thin VM storage). NVMe is already visible inside the LXC via
    bind-mount so we skip it. (No caching here — wrapped by _get_host_disks.)
    """
    host = os.getenv("PVE_SSH_HOST", "root@192.168.20.51")
    drives = []
    try:
        cmd = (
            # Thin-pool utilization of the 'data' LV on VG 'pve' — this is what
            # actually matters for AIRDISK (VM disks + container rootfs are thin-provisioned).
            "lvs --noheadings --units b --nosuffix -o lv_size,data_percent pve/data 2>/dev/null; "
            "echo '---'; "
            # Physical size of the underlying block device.
            "lsblk -b -d -n -o SIZE /dev/sda 2>/dev/null"
        )
        proc = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=2", "-o", "StrictHostKeyChecking=no",
             "-o", "BatchMode=yes", "-o", "LogLevel=ERROR",
             host, cmd],
            capture_output=True, text=True, timeout=4,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            parts = proc.stdout.split("---")
            pool_line = parts[0].strip().split()
            pool_size = int(pool_line[0]) if pool_line and pool_line[0].isdigit() else 0
            pool_pct = float(pool_line[1]) if len(pool_line) > 1 else 0.0
            phys_size = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip().isdigit() else 0

            if phys_size > 0:
                used = int(pool_size * pool_pct / 100)
                # Percent shown is usage against the physical device, so overall
                # "disk is X% full" answer matches what you'd expect.
                percent = round(used / phys_size * 100, 1) if phys_size else 0
                drives.append({
                    "name": "AIRDISK",
                    "mount": "pve · LVM-thin",
                    "total": _format_bytes(phys_size),
                    "used": _format_bytes(used),
                    "free": _format_bytes(phys_size - used),
                    "percent": percent,
                })
    except Exception:
        pass

    return drives


_GPU_QUERY = "nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw --format=csv,noheader,nounits"


def _parse_gpu_csv(stdout: str):
    """Parse one nvidia-smi CSV line into a GPU dict, or None if unparseable."""
    parts = [p.strip() for p in stdout.strip().splitlines()[0].split(",")]
    if len(parts) < 6:
        return None
    def _to_float(v):
        try: return float(v)
        except ValueError: return 0.0
    name, temp, util, vmu, vmt, pwr = parts[:6]
    return {
        "online": True,
        "name": name,
        "temp_c": _to_float(temp),
        "util_pct": _to_float(util),
        "vram_used_mib": int(_to_float(vmu)),
        "vram_total_mib": int(_to_float(vmt)),
        "power_w": _to_float(pwr),
    }


def _compute_gpu_info() -> dict:
    """Query nvidia-smi for the 3080. Tries the local card first (works when the
    GPU is home in this LXC), then falls back to SSH into VM 300 (when the GPU is
    loaned out to the gaming VM). Returns the parsed dict or {'online': False}.
    (No caching here — wrapped by _get_gpu_info in a stale-while-revalidate cache.)
    """
    reason = "no gpu"

    # Local card (GPU not on loan) — fast path, no network.
    try:
        proc = subprocess.run(_GPU_QUERY.split(), capture_output=True, text=True, timeout=4)
        if proc.returncode == 0 and proc.stdout.strip():
            parsed = _parse_gpu_csv(proc.stdout)
            if parsed:
                return parsed
        reason = proc.stderr.strip()[:120] or "local nvidia-smi failed"
    except FileNotFoundError:
        reason = "no local nvidia-smi"
    except subprocess.TimeoutExpired:
        reason = "local timeout"
    except Exception as e:
        reason = str(e)[:120]

    # Loaned out — ask VM 300 over SSH.
    host = os.getenv("GPU_HOST", "zain@192.168.20.212")
    try:
        proc = subprocess.run(
            [
                "ssh", "-o", "ConnectTimeout=2", "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "LogLevel=ERROR", host, _GPU_QUERY,
            ],
            capture_output=True, text=True, timeout=4,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            parsed = _parse_gpu_csv(proc.stdout)
            if parsed:
                return parsed
        reason = proc.stderr.strip()[:120] or "nvidia-smi failed"
    except subprocess.TimeoutExpired:
        reason = "timeout"
    except Exception as e:
        reason = str(e)[:120]

    return {"online": False, "reason": reason}


_gpu_holder = {}
def _get_gpu_info() -> dict:
    """nvidia-smi on VM 300 over SSH, stale-while-revalidate cached: 5s while
    online (fresh metrics), 20s while offline so a down VM isn't re-probed every
    few seconds — the connect timeout is the costly part. Returns instantly from
    cache; refresh happens on a background thread."""
    return _swr(_gpu_holder, _compute_gpu_info, lambda v: 5 if v.get("online") else 20, cold={"online": False})


def _get_mordor_status() -> dict:
    """Check if the MORDOR Minecraft server is running."""
    status = {"online": False, "duration": "—"}

    try:
        # Check PID file first, then fall back to process scan
        pid_file = os.path.join(POOL_ROOT, "MORDOR", "server.pid")
        is_running = False
        if os.path.exists(pid_file):
            with open(pid_file) as f:
                pid = int(f.read().strip())
            try:
                os.kill(pid, 0)
                is_running = True
            except (OSError, ProcessLookupError):
                pass
        if not is_running:
            result = subprocess.run(
                ["pgrep", "-f", "forge-1.7.10.*universal.jar"],
                capture_output=True, text=True, timeout=5
            )
            is_running = result.returncode == 0
    except Exception:
        is_running = False

    status["online"] = is_running

    # Calculate duration from the schedule log
    try:
        log_path = MORDOR_STATE_FILE
        if os.path.exists(log_path):
            with open(log_path, "r") as f:
                lines = f.readlines()
            # Find last start or stop event
            last_event = None
            last_time = None
            for line in reversed(lines):
                if "starting server" in line.lower() or "Starting Minecraft" in line:
                    last_event = "start"
                    last_time = line.split("]")[0].lstrip("[").strip()
                    break
                elif "stopped" in line.lower() or "Stopping Minecraft" in line:
                    last_event = "stop"
                    last_time = line.split("]")[0].lstrip("[").strip()
                    break

            if last_time:
                from datetime import datetime
                try:
                    event_dt = datetime.strptime(last_time, "%Y-%m-%d %H:%M:%S")
                    delta = datetime.now() - event_dt
                    total_secs = int(delta.total_seconds())
                    if total_secs < 0:
                        total_secs = 0
                    hours, rem = divmod(total_secs, 3600)
                    minutes = rem // 60
                    parts = []
                    if hours:
                        parts.append(f"{hours}h")
                    parts.append(f"{minutes}m")
                    status["duration"] = " ".join(parts)
                except (ValueError, TypeError):
                    pass
    except Exception:
        pass

    return status


def get_system_info() -> dict:
    """Gather system information for display."""
    # OS info
    if IS_MAC:
        os_name = f"macOS {platform.mac_ver()[0]}"
    else:
        try:
            with open("/etc/os-release") as f:
                os_info = {}
                for line in f:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        os_info[k] = v.strip('"')
            os_name = os_info.get("PRETTY_NAME", "Linux")
        except Exception:
            os_name = "Linux"

    # Uptime
    try:
        uptime_secs = psutil.boot_time()
        delta = timedelta(seconds=int(time.time() - uptime_secs))
        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes = remainder // 60
        parts = []
        if days: parts.append(f"{days} day{'s' if days != 1 else ''}")
        if hours: parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes: parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
        uptime_str = ", ".join(parts) if parts else "just started"
    except Exception:
        uptime_str = "N/A"

    # CPU
    cpu_percent = psutil.cpu_percent(interval=0)
    if IS_MAC:
        cpu_desc = f"{platform.processor()} ({psutil.cpu_count()} cores)"
    else:
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if "model name" in line:
                        cpu_desc = line.split(":")[1].strip()
                        break
                else:
                    cpu_desc = f"{psutil.cpu_count()} cores"
        except Exception:
            cpu_desc = f"{psutil.cpu_count()} cores"

    # Memory
    mem = psutil.virtual_memory()

    caps = _capabilities()

    # Disks (cached). The Proxmox-host probes (AIRDISK drives + real host CPU/mem
    # over SSH) are ARES-LXC-only: ARES's Flask runs inside LXC 101 and reaches
    # past its cgroup to the host. On a bare-metal box (e.g. ZEUS) they would
    # waste an SSH connect-timeout every refresh and — same LAN — could even report
    # the WRONG box's numbers. Gate them on the proxmox capability.
    disks = _get_disks() + (_get_host_disks() if caps.get("proxmox") else [])
    if os.getenv("HOST_BRAND", "").upper() == "ZEUS":
        phys = _get_disks_physical()            # per-SSD rows (T5/T7/T9) for the array map
        disks = disks + phys
        # PROMETHEUS = the whole pool: mergerfs' statvfs only reports one branch, so
        # sum the physical SSDs for the true total capacity/used.
        tot = sum(d.get("_tb", 0) for d in phys)
        usd = sum(d.get("_ub", 0) for d in phys)
        if tot > 0:
            for d in disks:
                if d.get("name") == "PROMETHEUS":
                    d["total"] = _format_bytes(tot)
                    d["used"] = _format_bytes(usd)
                    d["free"] = _format_bytes(tot - usd)
                    d["percent"] = round(usd / tot * 100, 1)
                    break
        for d in phys:                          # drop raw helper keys
            d.pop("_tb", None); d.pop("_ub", None)

    # Top-level folder sizes
    folders = _get_folder_sizes()

    host_compute = _get_host_compute() if caps.get("proxmox") else {}
    out = {
        "hostname": os.getenv("HOST_BRAND", "ARES"),
        "folders": folders,
        "os": os_name,
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "cpu": host_compute.get("cpu") or cpu_desc,
        "cpu_percent": host_compute.get("cpu_percent", cpu_percent),
        "cpu_temp": host_compute.get("cpu_temp", _get_cpu_temp()),
        "memory_total": host_compute.get("memory_total", _format_bytes(mem.total)),
        "memory_used": host_compute.get("memory_used", _format_bytes(mem.used)),
        "memory_percent": host_compute.get("memory_percent", round(mem.percent, 1)),
        "uptime": uptime_str,
        "disks": disks,
        "python": platform.python_version(),
        "mordor": _get_mordor_status() if caps.get("mordor") else {"online": False},
        "gpu": _get_gpu_info() if caps.get("gpu") else {"online": False},
        "crons": _read_crontab_jobs() if os.getenv("HOST_BRAND", "").upper() == "ZEUS" else _read_host_crons(),
        "io": _io_rates(),
        "caps": caps,
    }
    if caps.get("docker"):
        out["containers"] = _get_containers()
    return out
