# ARES WiFi Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A physical-console WiFi picker on ARES: when offline at login, show nearby networks, pick one, enter the password, save it, and drop into the normal shell — while unattended boots still auto-connect and never block.

**Architecture:** Three bash scripts installed under `/usr/local`, built on the existing wpa_supplicant + ifupdown stack (no NetworkManager/iwd). `ares-net-online` = connectivity check. `ares-wifi` = whiptail picker (scan→menu→password→connect→verify) whose pure parser is unit-tested via `--selftest`. A `/root/.profile` guard launches the picker only on a physical TTY when offline; a `wifi` command runs it on demand.

**Tech Stack:** bash, whiptail, wpa_cli, dhclient. No pytest — tests are the scripts' own `--selftest` and exit-code checks (these are system scripts, not the Flask app).

## Global Constraints

- Interface is `wlp6s0` (Realtek RTL8852AE). WiFi config file: `/etc/wpa_supplicant.conf`.
- Scripts live at: `/usr/local/sbin/ares-net-online`, `/usr/local/sbin/ares-wifi`, symlink `/usr/local/bin/wifi -> /usr/local/sbin/ares-wifi`.
- The picker is NEVER a boot service — it is login-triggered only, and must never block an unattended boot. Headless auto-connect is handled separately by the existing `wifi-uplink-ensure.service` + wpa_supplicant priorities.
- The login hook must be wrapped so any failure still yields a shell (never lock root out).
- Trigger guard: physical TTY (`/dev/tty[1-6]`) AND no `$SSH_CONNECTION` AND offline.
- `wpa_supplicant.conf` needs `ctrl_interface=DIR=/run/wpa_supplicant GROUP=netdev` and `update_config=1` for `wpa_cli save_config` to persist. Back it up before editing.
- All scripts start with `#!/bin/bash` and `set -u`. Not a git repo — "commit" = run the script's selftest/checks and confirm green.
- `scan_results` format (verified): first line is a header; data lines are TAB-separated `bssid<TAB>freq<TAB>signal<TAB>flags<TAB>ssid`, e.g. `90:72:40:1e:31:d9\t5180\t-49\t[WPA2-PSK-CCMP][ESS]\tjanjee`.

---

### Task 1: `ares-net-online` connectivity check

**Files:**
- Create: `/usr/local/sbin/ares-net-online`

**Interfaces:**
- Produces: an executable that exits `0` when online, `1` when offline. Fast (<3s).

- [ ] **Step 1: Write the script**

```bash
#!/bin/bash
# Exit 0 if the box has working internet via the WiFi uplink, else 1. Fast.
set -u
IFACE="${ARES_WIFI_IFACE:-wlp6s0}"
# 1) must have a default route out the wifi interface
ip -4 route show default 2>/dev/null | grep -q "dev $IFACE" || exit 1
# 2) must actually reach the internet (route can exist but be dead)
if command -v curl >/dev/null 2>&1; then
    curl -sf --max-time 3 -o /dev/null http://connectivitycheck.gstatic.com/generate_204 && exit 0
fi
ping -c1 -W2 1.1.1.1 >/dev/null 2>&1 && exit 0
exit 1
```

- [ ] **Step 2: Install + make executable**

```bash
install -m 0755 /dev/stdin /usr/local/sbin/ares-net-online < <(cat)   # or: chmod +x after writing
chmod +x /usr/local/sbin/ares-net-online
```
(If written directly to the path, just `chmod +x /usr/local/sbin/ares-net-online`.)

- [ ] **Step 3: Test — online now, so it must exit 0**

Run: `/usr/local/sbin/ares-net-online; echo "exit=$?"`
Expected: `exit=0` (the box is currently online).

- [ ] **Step 4: Test the offline path deterministically (fake interface)**

Run: `ARES_WIFI_IFACE=nonexistent0 /usr/local/sbin/ares-net-online; echo "exit=$?"`
Expected: `exit=1` (no default route on a bogus interface → offline branch).

---

### Task 2: `ares-wifi` scan parser + `--selftest`

**Files:**
- Create: `/usr/local/sbin/ares-wifi`

**Interfaces:**
- Produces: `ares-wifi --selftest` (exit 0 if the parser output matches expected).
- Internal: `parse_scan_results` reads `scan_results` text on stdin, emits
  `ssid<TAB>signal<TAB>state` per line (state = `locked` or `open`), deduped by
  SSID keeping the strongest signal, sorted by signal descending, blank SSIDs
  dropped.

- [ ] **Step 1: Write the script skeleton with the parser + selftest**

```bash
#!/bin/bash
# ARES WiFi picker. Console-only network selection on the existing wpa_supplicant
# stack. See docs/superpowers/specs/2026-09-04-ares-wifi-picker-design.md.
set -u
IFACE="${ARES_WIFI_IFACE:-wlp6s0}"

# Read `wpa_cli scan_results` text on stdin -> "ssid<TAB>signal<TAB>state",
# deduped by ssid (strongest wins), sorted strongest-first, blanks dropped.
parse_scan_results() {
    awk -F'\t' '
        NF>=5 && $1 ~ /^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$/ {
            ssid=$5; sig=$3+0; flags=$4
            if (ssid=="") next
            state=(flags ~ /WPA|RSN|WEP/) ? "locked" : "open"
            if (!(ssid in best) || sig > best[ssid]) { best[ssid]=sig; st[ssid]=state }
        }
        END { for (s in best) printf "%s\t%d\t%s\n", s, best[s], st[s] }
    ' | sort -t$'\t' -k2,2nr
}

selftest() {
    local sample expected got
    sample=$'bssid / frequency / signal level / flags / ssid\n90:72:40:1e:31:d9\t5180\t-49\t[WPA2-PSK-CCMP][ESS]\tjanjee\naa:bb:cc:dd:ee:01\t2412\t-70\t[WPA2-PSK-CCMP][ESS]\tNETGEAR\naa:bb:cc:dd:ee:02\t5200\t-55\t[WPA2-PSK-CCMP][ESS]\tNETGEAR\naa:bb:cc:dd:ee:03\t2437\t-80\t[ESS]\txfinitywifi\naa:bb:cc:dd:ee:04\t2462\t-60\t[ESS]\t'
    expected=$'janjee\t-49\tlocked\nNETGEAR\t-55\tlocked\nxfinitywifi\t-80\topen'
    got="$(printf '%s\n' "$sample" | parse_scan_results)"
    if [ "$got" = "$expected" ]; then
        echo "selftest OK"; return 0
    else
        echo "selftest FAIL"; echo "--- expected ---"; printf '%s\n' "$expected"
        echo "--- got ---"; printf '%s\n' "$got"; return 1
    fi
}

case "${1:-}" in
    --selftest) selftest; exit $? ;;
    *) echo "interactive mode not implemented yet (Task 4)"; exit 0 ;;
esac
```

- [ ] **Step 2: Install + executable**

`chmod +x /usr/local/sbin/ares-wifi`

- [ ] **Step 3: Run selftest, expect PASS**

Run: `/usr/local/sbin/ares-wifi --selftest; echo "exit=$?"`
Expected: `selftest OK` and `exit=0`. (Verifies dedupe of NETGEAR to -55, blank-SSID drop, open/locked classification, strongest-first sort.)

- [ ] **Step 4: Confirm it fails loudly if the parser breaks**

Temporarily change `sort -k2,2nr` to `sort -k2,2n` (ascending), rerun selftest → expect `selftest FAIL` with a diff, then revert. (Proves the test actually guards ordering.)

---

### Task 3: `wpa_supplicant.conf` prep (headers + priority + backup)

**Files:**
- Modify: `/etc/wpa_supplicant.conf`

**Interfaces:**
- Produces: a `wpa_supplicant.conf` that begins with `ctrl_interface=...` and
  `update_config=1`, with the existing `janjee` network preserved and given a
  `priority` if missing, so `wpa_cli save_config` works.

- [ ] **Step 1: Back up**

```bash
cp -a /etc/wpa_supplicant.conf /etc/wpa_supplicant.conf.bak-$(date +%Y%m%d-%H%M%S)
```

- [ ] **Step 2: Prepend the required headers if absent**

```bash
grep -q '^ctrl_interface=' /etc/wpa_supplicant.conf || \
  sed -i '1i ctrl_interface=DIR=/run/wpa_supplicant GROUP=netdev' /etc/wpa_supplicant.conf
grep -q '^update_config=1' /etc/wpa_supplicant.conf || \
  sed -i '/^ctrl_interface=/a update_config=1' /etc/wpa_supplicant.conf
```

- [ ] **Step 3: Reload wpa_supplicant so the running daemon picks up ctrl/update_config**

```bash
wpa_cli -i wlp6s0 reconfigure
```
Expected: `OK`.

- [ ] **Step 4: Verify save_config now works (round-trips without error)**

```bash
wpa_cli -i wlp6s0 save_config
```
Expected: `OK` (fails with `FAIL` if `update_config=1` isn't active). Confirm the
file still has the `janjee` block and now the two headers:
```bash
grep -nE '^ctrl_interface=|^update_config=|ssid="janjee"' /etc/wpa_supplicant.conf
```
Expected: all three present. Connectivity must be UNAFFECTED — verify:
`/usr/local/sbin/ares-net-online; echo $?` → `0`.

---

### Task 4: `ares-wifi` interactive picker (scan → menu → password → connect → verify)

**Files:**
- Modify: `/usr/local/sbin/ares-wifi` (replace the Task-2 stub `*)` branch)

**Interfaces:**
- Consumes: `parse_scan_results` (Task 2), `/usr/local/sbin/ares-net-online` (Task 1), the prepped `wpa_supplicant.conf` (Task 3).
- Produces: interactive `ares-wifi` (no args) and `ares-wifi --dry-run` (menu, no apply).

- [ ] **Step 1: Add helper functions above the `case` block**

```bash
# strongest-first list -> whiptail menu tag/item pairs on stdout
signal_glyph() { # $1 = dBm
    local s=$1
    if   [ "$s" -ge -50 ]; then echo "▂▄▆█"
    elif [ "$s" -ge -60 ]; then echo "▂▄▆░"
    elif [ "$s" -ge -70 ]; then echo "▂▄░░"
    else                        echo "▂░░░"; fi
}

do_scan() { # echoes parsed networks (ssid<TAB>signal<TAB>state)
    wpa_cli -i "$IFACE" scan >/dev/null 2>&1
    sleep 3
    wpa_cli -i "$IFACE" scan_results 2>/dev/null | parse_scan_results
}

next_priority() { # one higher than the current max saved priority (default 0)
    local n; n=$(wpa_cli -i "$IFACE" list_networks 2>/dev/null | tail -n +2 | wc -l)
    echo "$n"
}

pick_menu() { # $1 = parsed-networks text; echoes chosen ssid, or __rescan__/__shell__
    local args=() ssid sig state lock
    while IFS=$'\t' read -r ssid sig state; do
        [ -z "$ssid" ] && continue
        lock=""; [ "$state" = "locked" ] && lock="🔒"
        args+=("$ssid" "$(signal_glyph "$sig")  $lock")
    done <<< "$1"
    args+=("__rescan__" "↻  Rescan" "__shell__" "⇥  Skip — drop to shell")
    whiptail --title "Select WiFi" --menu "Choose a network:" 20 60 12 \
        "${args[@]}" 3>&1 1>&2 2>&3
}

connect_ssid() { # $1=ssid $2=state("locked"/"open"); returns 0 on verified internet
    local ssid="$1" state="$2" psk="" id
    if [ "$state" = "locked" ]; then
        psk=$(whiptail --title "$ssid" --passwordbox "Password for \"$ssid\":" 10 60 3>&1 1>&2 2>&3) || return 2
        [ -z "$psk" ] && { whiptail --msgbox "Empty password — cancelled." 8 50; return 2; }
    fi
    id=$(wpa_cli -i "$IFACE" add_network | tail -1)
    wpa_cli -i "$IFACE" set_network "$id" ssid "\"$ssid\"" >/dev/null
    if [ "$state" = "locked" ]; then
        wpa_cli -i "$IFACE" set_network "$id" psk "\"$psk\"" >/dev/null
    else
        wpa_cli -i "$IFACE" set_network "$id" key_mgmt NONE >/dev/null
    fi
    wpa_cli -i "$IFACE" set_network "$id" priority "$(next_priority)" >/dev/null
    wpa_cli -i "$IFACE" enable_network "$id" >/dev/null
    wpa_cli -i "$IFACE" select_network "$id" >/dev/null   # associate now
    { TERM=vt100 whiptail --title "ARES WiFi" --infobox \
        "Connecting to $ssid…\nAssociating + getting IP…" 8 50; } || true
    dhclient -1 "$IFACE" >/dev/null 2>&1
    local i
    for i in $(seq 1 20); do
        if /usr/local/sbin/ares-net-online; then
            wpa_cli -i "$IFACE" save_config >/dev/null   # persist on success
            return 0
        fi
        sleep 1
    done
    # failure: forget the bad network so it can't poison future boots
    wpa_cli -i "$IFACE" remove_network "$id" >/dev/null
    wpa_cli -i "$IFACE" save_config >/dev/null
    wpa_cli -i "$IFACE" reconfigure >/dev/null
    return 1
}
```

- [ ] **Step 2: Replace the stub `*)` branch with the interactive loop**

```bash
case "${1:-}" in
    --selftest) selftest; exit $? ;;
    --dry-run)
        nets="$(do_scan)"
        choice="$(pick_menu "$nets")" || { echo "(cancelled)"; exit 0; }
        echo "would connect to: $choice (dry-run, nothing applied)"; exit 0 ;;
    *)
        while true; do
            nets="$(do_scan)"
            if [ -z "$nets" ]; then
                whiptail --title "ARES WiFi" --yesno "No networks found. Rescan?" 8 50 \
                    && continue || exit 0
            fi
            choice="$(pick_menu "$nets")" || exit 0          # Cancel -> shell
            case "$choice" in
                __shell__|"") exit 0 ;;                       # Skip -> shell
                __rescan__)   continue ;;
                *)
                    state=$(printf '%s\n' "$nets" | awk -F'\t' -v s="$choice" '$1==s{print $3; exit}')
                    if connect_ssid "$choice" "$state"; then
                        ip=$(ip -4 -o addr show "$IFACE" | awk '{print $4}' | cut -d/ -f1)
                        whiptail --title "Connected" --msgbox \
                          "✓ Online via $choice ($ip)\n✓ Saved — auto-connects here next boot\n\nDropping to shell…" 11 55
                        exit 0
                    else
                        whiptail --title "Failed" --msgbox \
                          "✗ Could not connect to $choice\n(auth failed or no DHCP lease)\n\nForgetting it, back to the list…" 11 55
                    fi ;;
            esac
        done ;;
esac
```

- [ ] **Step 3: Re-run selftest (parser must still pass after the edits)**

Run: `/usr/local/sbin/ares-wifi --selftest`
Expected: `selftest OK`.

- [ ] **Step 4: Live dry-run (safe while online — scans + shows the real menu, applies nothing)**

Run: `/usr/local/sbin/ares-wifi --dry-run`
Expected: a whiptail menu listing real nearby SSIDs (janjee etc.) with signal
glyphs + 🔒; picking one prints `would connect to: <ssid> (dry-run…)`; Cancel prints `(cancelled)`. Nothing in `wpa_supplicant.conf` changes — verify with
`md5sum /etc/wpa_supplicant.conf` before/after are identical.

---

### Task 5: login hook + `wifi` command

**Files:**
- Modify: `/root/.profile`
- Create: symlink `/usr/local/bin/wifi -> /usr/local/sbin/ares-wifi`

**Interfaces:**
- Consumes: `ares-net-online` (Task 1), `ares-wifi` (Tasks 2/4).
- Produces: auto-launch of the picker on a physical TTY when offline; a `wifi` command anywhere.

- [ ] **Step 1: Create the `wifi` command symlink**

```bash
ln -sf /usr/local/sbin/ares-wifi /usr/local/bin/wifi
wifi --selftest    # resolves through the symlink
```
Expected: `selftest OK`.

- [ ] **Step 2: Append the guarded hook to `/root/.profile`**

```bash
cat >> /root/.profile <<'EOF'

# ---- ARES WiFi picker: physical console + offline only -----------------------
# Never runs over SSH (already online) and never blocks: any failure -> shell.
case "$(tty 2>/dev/null)" in
  /dev/tty[1-6])
    if [ -z "${SSH_CONNECTION:-}" ] && ! /usr/local/sbin/ares-net-online; then
      /usr/local/sbin/ares-wifi || true
    fi ;;
esac
# -----------------------------------------------------------------------------
EOF
```

- [ ] **Step 3: Verify the hook does NOT trigger in the current (online) session**

Run: `bash -lc 'echo reached-shell'` — but note this runs in a pts, so the
`tty` guard already excludes it. Directly test the guard logic:
```bash
tty; echo "SSH_CONNECTION=${SSH_CONNECTION:-<empty>}"; /usr/local/sbin/ares-net-online; echo "online=$?"
```
Expected: current tty is `/dev/pts/*` (not tty1-6) OR `SSH_CONNECTION` set OR `online=0` — ANY of which means the picker would be skipped. Confirm at least one guard excludes the current session (it will: you're online and/or on pts/SSH).

- [ ] **Step 4: Simulate the offline-console branch safely (no real login needed)**

```bash
ARES_WIFI_IFACE=nonexistent0 bash -c '
  if ! /usr/local/sbin/ares-net-online; then echo "would-launch-picker (offline branch reached)"; fi'
```
Expected: prints `would-launch-picker...` — proving the offline branch fires when
`ares-net-online` reports offline. (We do NOT actually launch the TUI here.)

- [ ] **Step 5: Final integration check — nothing broke, box still online**

```bash
/usr/local/sbin/ares-net-online && echo "STILL ONLINE ✓"
tail -12 /root/.profile        # hook present and well-formed
ls -l /usr/local/bin/wifi /usr/local/sbin/ares-wifi /usr/local/sbin/ares-net-online
```
Expected: `STILL ONLINE ✓`, the hook block present, all three files executable.

---

## Self-Review

**Spec coverage:**
- Custom whiptail picker on existing stack → Tasks 2, 4. ✓
- `connectivity-check` helper → Task 1. ✓
- Scan/parse/dedupe/sort/signal/lock → Task 2 `parse_scan_results` + `--selftest`. ✓
- Menu with Rescan + Skip-to-shell escape hatches, can't trap user → Task 4 `pick_menu`/loop. ✓
- Password box; open networks skip → Task 4 `connect_ssid`. ✓
- Connect via wpa_cli + save_config + priority (remember every place) → Task 4 + `next_priority`. ✓
- Verify internet, on success exec/drop to shell, on failure self-clean → Task 4 loop + `remove_network`. ✓
- `wpa_supplicant.conf` needs `ctrl_interface`+`update_config`, backup → Task 3. ✓
- Login hook: physical TTY + non-SSH + offline, wrapped so never locks out → Task 5. ✓
- `wifi` manual command + `--dry-run` preview → Task 5 / Task 4. ✓
- `--selftest` for the parser → Task 2. ✓
- Never a boot service / never blocks unattended boot → hook is login-only (Task 5); no unit created. ✓

**Placeholder scan:** No TBD/TODO. Every code step has complete code. The Task-2 stub `*)` branch is explicitly replaced in Task 4 Step 2.

**Type/name consistency:** `IFACE`, `parse_scan_results`, `do_scan`, `pick_menu`, `connect_ssid`, `next_priority`, `signal_glyph` consistent across Tasks 2/4. `ares-net-online` path identical in Tasks 1/4/5. `__rescan__`/`__shell__` sentinels consistent between `pick_menu` and the loop.

**Risk note:** Task 4 Step 4 verifies `--dry-run` mutates nothing (md5sum). The only real-mutation path (`connect_ssid` writing/saving networks) is exercised for real only on an actual relocation or a deliberate live test — flagged for manual verification, with the failure path self-cleaning.
