# ARES Terminal → Native xterm.js Rebuild

**Date:** 2026-08-18
**Status:** Design approved; ready for implementation planning.

## Goal

Rebuild the ARES web terminal page (`templates/shell.html`) so it is butter-smooth,
matches the dashboard's HUD aesthetic, and works well on both desktop (keyboard-native)
and mobile (on-screen key row + reliable typing).

## Problem with the current design

The current terminal is two disconnected systems bolted together:

- **Display:** a cross-origin `ttyd` terminal shown in an `<iframe>` (`/shell/` → :7681).
  Because it is cross-origin, it cannot be styled to match the dashboard or directly
  controlled.
- **Control:** an HTTP side-channel (`ares-shell-ctl`, `/ctl/*` → :7683) that pokes the
  tmux session behind ttyd for **every** keystroke, tab switch, scroll, and typed line
  (`/key`, `/text`, `/scroll`, `/capture`), plus a native `<input>` bar that injects
  typed text over HTTP.

Every input is an HTTP round-trip (laggy), the terminal wears ttyd's own theme (off
brand), and the mobile experience is clunky. This was reviewed by an LLM council; the
verdict was to replace the iframe + HTTP side-channel with a **native in-page terminal**,
not to decorate the iframe.

## Architecture — native xterm.js on the existing PTY WebSocket

Replace the ttyd iframe **and** the HTTP input side-channel with a native
[`xterm.js`](https://xtermjs.org/) terminal (loaded from CDN, no build step) wired
directly to the existing PTY-over-WebSocket backend:

- `ws.onmessage → term.write(data)` and `term.onData → ws.send(data)` — real, direct
  keyboard I/O; no per-keystroke HTTP.
- `xterm-addon-fit` computes `{cols, rows}` on resize and sends them over the WS.
- xterm.js theme is mapped to the dashboard tokens so the terminal *looks* like the
  dashboard.

### Backend feasibility (verified against the code — no backend changes needed)

`system/pty_ws.py` (port 7686, exposed via Caddy at `/pty-ws/*`) already provides
everything this needs:

- **Resize:** `_set_winsize(fd, cols, rows)` issues `TIOCSWINSZ`; a text frame
  `{"cols":N,"rows":M}` triggers it. ✅
- **Protocol:** the first WS frame names the session to attach (a bare session name → a
  shared `dtach`/`tmux new-session -A`); a `{"list":true}` frame lists sessions;
  `{"cols","rows"}` resizes; `{"ctl":"clear"}` is a line-kill; otherwise frames are raw
  PTY bytes. ✅ **The web terminal attaches to the tmux session that backs the tab strip
  (so windows show as tabs), not the bare `dtach` shell** — the smoke test (step 1)
  confirms the exact session name to use.
- **Auth:** the `/pty-ws/*` Caddy route is gated by **Tailscale source IP** (non-Tailscale
  → 403), *not* by a login cookie — so there is no cookie-handoff problem; a browser
  already on Tailscale may open it. ✅
- **Already proven:** the current page already opens this WebSocket (`_ptyWs`), so the
  browser→`pty_ws` connection works today. ✅

The iOS app already uses this same backend natively (SwiftTerm), so the contract is
battle-tested.

## Components (the rebuilt `templates/shell.html`)

- **HUD nav** — reuse the shared kit's top bar (already on the page from the prior HUD
  rebuild): brand chip · TERMINAL · back-to-home · jump-to-ZEUS.
- **Terminal panel** — one full-height `.mod` panel (`c-tl c-tr c-bl c-br`) that fills the
  viewport below the nav (`.hud-shell` is a flex column; the panel is `flex:1`, the xterm
  mount is `height:100%`).
- **Tab strip (panel head)** — the tmux window list: active window = red pill + glowing
  dot; inactive = dim; `×` closes; `+ ares` / `+ zeus` spawn a window on either box.
  `pty · dtach` status on the right. Driven by the existing window control commands
  (`ares-shell-ctl` `/windows`,`/new`,`/select`,`/kill`, or the equivalent `pty_ws`
  commands — implementer picks the simplest that preserves today's behavior).
- **xterm.js mount** — themed to the dashboard: `--bg-0` background, JetBrains Mono,
  `--text-1` foreground, red prompt/cursor (`--ares`) with a soft glow, ANSI palette tuned
  to the dashboard reds/greens; block cursor.
- **Mobile key row** — fixed bottom, horizontally scrollable, 38px tap targets: esc · tab
  · ctrl · ↑ ↓ ← → · `/` · `|` · `~` · ⌫ · copy. Buttons send raw escape sequences over the
  WS. Shown only at mobile widths.
- **Off-screen input** — a visually-hidden `<input>` (autocorrect/autocapitalize/spellcheck
  off) focused on tap to reliably capture the mobile soft keyboard; its input relays to the
  WS. `visualViewport` resize keeps the terminal above the keyboard, not behind it.

## Preserved behaviors

- tmux window tabs (new / next / close / select) on both ARES and ZEUS.
- Image paste → upload to `_scratch` → inject the host path into the terminal (now injected
  over the WS instead of the `/text` HTTP route).
- Copy: native xterm selection + a copy button using `term.getSelection()` (replaces
  `/capture`).
- Reconnect: on WS close, backoff-reconnect the WebSocket (replaces the reload-the-iframe
  logic).

## Dropped

- The `ttyd` `<iframe>` and its reload/reconnect logic.
- The HTTP input side-channel for this page: `/key`, `/text`, `/scroll`, `/capture`, and
  the text-injection `<input>` bar. (The `ares-shell-ctl` service itself stays for the tab
  strip's window commands unless the implementer moves those to `pty_ws`.)

## Libraries

`xterm`, `xterm-addon-fit`, and `xterm-addon-web-links` from a **pinned** CDN
(`jsdelivr`/`unpkg`), consistent with the app's no-build-step approach. Pin exact versions
so the terminal can't break from an upstream change.

## Build / de-risk order (mobile matters most)

1. **Smoke test** — from the logged-in dashboard console, open the `/pty-ws/` WebSocket,
   send the attach frame, confirm PTY bytes return. (Already known to work; confirms the
   attach-frame shape to use.)
2. **Bare terminal** — xterm.js + fit addon + WS I/O in the panel: type `ls`, see output.
   No theming yet.
3. **Resize** — `ResizeObserver`/window resize → `fitAddon.fit()` → send `{cols,rows}`.
4. **Mobile input** — off-screen input capture + the key row + `visualViewport` handling.
   Solve this *before* polish — it is the load-bearing risk.
5. **Theme + tab strip + chrome** — xterm theme mapped to tokens, the HUD panel, tab strip,
   copy/paste, reconnect.

## Verification

- **Pre-deploy:** the standalone render harness can screenshot the **static chrome**
  (nav, panel, tab strip, key row) — but not a live terminal (the WS/PTY isn't reachable
  from the harness).
- **Post-deploy (human, logged in over Tailscale):** typing is immediate; scrollback is
  smooth; resize reflows correctly; the tab strip switches/creates/closes windows; the
  mobile key row + soft-keyboard typing work on a phone; copy/paste and image-paste work;
  reconnect recovers after the socket drops.
- **Regression:** `home.html` and the other pages are untouched.

## Out of scope

- Any backend change to `pty_ws.py` (it already meets the contract).
- The terminal↔dashboard integrations the council imagined (sentinel-parsing vitals,
  command palette) — future ideas, not this rebuild.
- Touching `home.html` or other op-module pages.

## Success criteria

1. The terminal is a native in-page `xterm.js` instance on the `pty_ws` WebSocket — no
   ttyd iframe, no per-keystroke HTTP.
2. It visibly matches the dashboard HUD aesthetic (panel chrome, JetBrains Mono, red
   accent, block cursor).
3. Desktop: keyboard-native, immediate typing, smooth scrollback, native copy.
4. Mobile: the key row + off-screen input give reliable typing; the terminal stays above
   the soft keyboard.
5. tmux tabs, image-paste, copy, and reconnect all still work.
