# ARES WiFi Picker — boot-time console network selection

Date: 2026-09-04
Status: Approved (design), pending implementation plan

## Problem

ARES is a headless Proxmox host whose only uplink is WiFi (`wlp6s0`, Realtek
RTL8852AE). It now persists a known network across reboots (see
`ares-wifi-uplink` memory: `inet dhcp` + `wpa_supplicant.conf` + the
`wifi-uplink-ensure.service` safety net). But when the box is moved to a **new
place** with an **unknown** WiFi router, there is no way to select a new network
from the physical console — you'd have to hand-edit `wpa_supplicant.conf`.

Goal: at the physical console, if there is no internet, automatically present a
terminal WiFi picker — list nearby networks, pick one, enter the password, and
on success save it (so it auto-connects next boot) and drop into the normal ARES
shell (where `ssh ares` lands). Known networks must still auto-connect on
unattended boots with nobody present; the picker must never block boot.

## Design decisions (locked)

- **Approach A**: custom `whiptail` picker on the EXISTING stack (ifupdown +
  wpa_supplicant). No NetworkManager, no iwd — zero risk to the Proxmox
  networking (`vmbr0`/`vmbr1`) just stabilized.
- **Interactive-only trigger**: the picker runs from root's login on a PHYSICAL
  TTY when offline. It is NOT a boot service and never blocks an unattended boot.
- **Headless auto-connect is separate**: `wifi-uplink-ensure.service` +
  wpa_supplicant network priorities handle nobody-present boots. The picker only
  covers the "I'm standing here at a new place" case.

## Architecture — three small units

### 1. `connectivity-check` (helper)
`/usr/local/sbin/ares-net-online` — exit 0 if online, 1 if not. Fast (<3s):
default route on `wlp6s0` AND a reachability probe (`curl -sf --max-time 3` to a
known endpoint, fallback `ping -c1 -W2 1.1.1.1`). Reused by the login hook and
the picker's verify step.

### 2. `ares-wifi` picker (the TUI)
`/usr/local/sbin/ares-wifi` — bash + whiptail. Flow:

1. **Scan**: `wpa_cli -i wlp6s0 scan`, wait, `wpa_cli -i wlp6s0 scan_results`.
   Parse tab-separated output → (bssid, freq, signal, flags, ssid). Drop blank
   SSIDs, dedupe by SSID keeping the strongest signal, sort by signal desc.
   Render a signal glyph (▂▄▆█ tiers by dBm) and a lock glyph if flags contain
   WPA/WPA2/WPA3/RSN.
2. **Menu**: `whiptail --menu` of SSIDs + glyphs, plus two synthetic entries:
   `__rescan__` (re-run scan) and `__shell__` (skip — exit 0 to drop to shell).
   Cancel also drops to shell. The menu can NEVER trap the user.
3. **Password**: for a locked network, `whiptail --passwordbox`. Open network →
   skip (key_mgmt=NONE). Empty password on a locked network → re-prompt once.
4. **Connect** (via wpa_cli so it persists to wpa_supplicant.conf):
   `id=$(wpa_cli -i wlp6s0 add_network)`, `set_network $id ssid '"…"'`,
   `set_network $id psk '"…"'` (or `key_mgmt NONE`),
   `set_network $id priority <next>`, `enable_network $id`, `save_config`,
   then `wpa_cli reconfigure` and obtain a lease (`dhclient -1 wlp6s0`).
5. **Verify**: poll `ares-net-online` up to ~20s. Success → show the assigned IP
   + "saved" message, then `exec` the login shell (see hook). Failure →
   `remove_network $id` + `save_config` (so the bad entry can't poison future
   boots) + `wpa_cli reconfigure`, show error, return to the menu.

Priority: newly saved network gets `priority = (max existing priority) + 1`, so
the current location is preferred when multiple known networks are in range.
This is how "remember every place" works — each success appends a
`network={… priority=N}` block; wpa_supplicant auto-selects among them on boot.

Flags: `--dry-run` (scan + show the menu but never apply — for previewing while
online) and `--selftest` (feed fixed sample scan output to the parser and assert
the parsed list; no wifi needed).

### 3. Login hook + `wifi` command
- `/root/.bash_profile` (or a sourced `/root/.config/ares-wifi-hook.sh`):
  ```sh
  # Physical console + offline → launch the picker; else fall through to shell.
  case "$(tty)" in
    /dev/tty[1-6])
      if [ -z "$SSH_CONNECTION" ] && ! /usr/local/sbin/ares-net-online; then
        /usr/local/sbin/ares-wifi || true   # never let it block login
      fi ;;
  esac
  ```
  Wrapped so ANY failure still yields a shell — root can never be locked out.
- `wifi` = symlink `/usr/local/bin/wifi -> /usr/local/sbin/ares-wifi` so the
  picker can be launched manually on demand, even when online.

## Prep change to `wpa_supplicant.conf`
Ensure the file begins with:
```
ctrl_interface=DIR=/run/wpa_supplicant GROUP=netdev
update_config=1
```
`update_config=1` is required for `wpa_cli save_config` to persist. The existing
`network={ ssid="janjee" … }` block is preserved (and gets a `priority` if
absent). Back up the file before editing.

## Error handling / safety
- Picker is login-triggered, NEVER a boot unit → unattended boot is never blocked.
- Login hook wrapped (`|| true`) → a broken picker still drops to a shell.
- Offline check gated to physical TTY + non-SSH → online/SSH logins get zero
  added latency and never see the picker.
- Failed connections self-clean (`remove_network` + `save_config`).
- Bouncing WiFi mid-picker is safe: you're at the console, offline, no SSH to cut.
- All scripts `set -u`; connect/verify have hard timeouts (no infinite hang).

## Testing
- `--selftest`: sample `scan_results` text → assert parser yields the expected
  sorted, deduped (ssid, signal, locked) list. Runnable, no hardware.
- `ares-net-online`: assert exit 0 while currently online (integration check).
- TUI + live association: manual, via `--dry-run` (menu, no apply) and a real
  connect on next relocation.

## Out of scope (YAGNI)
- Captive portals (hotel/airport splash pages) — known limitation, not handled.
- WPA-Enterprise / 802.1X — PSK/open only.
- Hidden SSIDs — add `scan_ssid=1` support later if needed.
- GUI/web control — this is console-only by design (works with no network).
