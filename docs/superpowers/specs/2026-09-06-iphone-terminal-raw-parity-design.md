# iPhone Terminal → Laptop-Parity Claude Code — Design

**Date:** 2026-09-06
**Status:** Approved (Mirror sync model), autonomous build authorized
**Repos:** `A&N` (native Swift app), `ARES-DASHBOARD` (Flask backend + `pty_ws`)

## Problem

The native ARES iPhone app (`more projects/A&N/ARES/TerminalScreen.swift`) defaults to a
**polled re-render / chat mode** (`pty_ws.console_loop`): it does not stream the terminal, it
polls `tmux capture-pane` and injects input via `tmux send-keys`. Consequences reported by the
owner:

- **Laggy** — polling snapshots, not streaming bytes.
- **Awful to type/code** — typing into a chat composer that replays keystrokes; autocorrect,
  smart-quotes, and auto-caps fight shell/code input.
- **Claude's questions invisible** — the parser reads the transcript log / OSC-133 blocks, so
  Claude Code's *interactive* TUI prompts (permission "Do you want to proceed? y/n", plan
  approvals, numbered menus) never render.
- **No image upload** — neither mode can send an image to Claude.
- **Tabs don't match tmux windows** — the tab strip / active window can diverge from what the
  Mac and dashboard show.

The app already contains a **RAW mode** (`SwiftTerm` streaming a real pty attach over
`pty_ws:7686`, `PTYTerminal`, TerminalScreen.swift ~line 1116) — the true laptop-parity
surface — but it is buried behind a small "RAW" chip as an escape hatch.

## Goal

Make the phone terminal behave "the exact same as laptop Claude Code": a real streaming TTY on
the shared tmux `web` session, typeable, with Claude's prompts visible and image upload working.

## Decisions (owner-approved)

- **Direction:** make RAW streaming the default; demote (do not yet delete) console/chat mode.
- **Primary use:** driving Claude Code in tmux `web`.
- **Sync model:** **Mirror** — the phone shows exactly the Mac's active window; switching a tab
  on either side moves both. `window-size latest` (already set globally, tmux 3.5a) means the
  most-recently-active client sets the width; Claude's TUI reflows. Accepted tradeoff: when the
  phone is the active typist, the shared window briefly takes the phone's width until the Mac
  types again.
- **Images:** both a Photos/camera picker and clipboard paste; upload to ARES; insert the
  returned file path into the pty so Claude reads the image by path (laptop drag-drop analog).

## Architecture

### Backend (`ARES-DASHBOARD`)

1. **`pty_ws` raw attach stays the transport.** RAW already does
   `{"open":"web"}` → `tmux new-session -A -s web` and streams bytes both ways with
   `{"cols":N,"rows":M}` resize frames. No protocol change required for the core.
2. **Image upload endpoint** — new authenticated Flask route
   `POST /api/terminal/upload` (multipart, `require_auth`). Writes the file to a terminal inbox
   dir under the pool (e.g. `PROMETHEUS/PROJECTS/_scratch/term-uploads/<ts>-<name>`), returns
   `{ "path": "<absolute path on ARES>" }`. Validates content-type is image/*, caps size,
   sanitizes filename. The client then sends that path into the pty as text (optionally wrapped
   in quotes) so it lands in the Claude prompt at the cursor.

### Client (`A&N` — `TerminalScreen.swift`)

1. **Default to RAW.** On entering the terminal, connect `PTYTerminal` and show
   `SwiftTermRepresentable` as the primary view. The console/blocks/transcript path becomes an
   optional toggle (kept for now; a later change may remove it and the associated
   `pty_ws.console_loop`, blocks parser, and transcript machinery).
2. **Tab strip = live tmux windows (Mirror).** Keep the existing `ctl: windows` /
   `selectwin` / `newwin` / `killwin` control channel to render tabs and switch the *real*
   active window; the SwiftTerm view follows because it renders the live attach. Ensure the
   window list refreshes on change and the active tab reflects `window_active`.
3. **Keyboard accessory bar** above the iOS keyboard (SwiftTerm input): a scrollable row of
   keys the TUI needs and the soft keyboard lacks —
   `Esc  Tab  Ctrl(sticky)  ⌥  ←  ↓  ↑  →  /  |  ~  -  ⌃C` — plus a Claude quick-answer row:
   `y  n  Enter  Shift-Tab (plan mode)  1  2  3`. Each key feeds the correct byte sequence into
   the pty (e.g. Esc = `0x1b`, Ctrl-C = `0x03`, arrows = `ESC [ A/B/C/D`, Shift-Tab =
   `ESC [ Z`). Sticky Ctrl modifies the next key.
4. **Typing hygiene.** The SwiftTerm view already bypasses the SwiftUI composer, so shell input
   avoids autocorrect. Verify autocorrect / smart-dashes / smart-quotes / auto-caps are all off
   on any text entry that reaches the pty. Support hardware keyboards.
5. **Image button + paste.** Add an image button to the terminal toolbar → `PHPickerViewController`
   (library) and camera; also intercept image paste from the clipboard. On selection: upload via
   `/api/terminal/upload`, then inject the returned path into the pty. Show a small inline
   "uploading…" state.
6. **Robustness.** Clean resize on rotation and keyboard show/hide (send `{"cols","rows"}` sized
   to the SwiftTerm view). Reconnect with backoff (already present for the console socket —
   mirror it for `PTYTerminal`). Keepalive so a backgrounded app reattaches instantly.

## Components (isolation)

| Unit | Purpose | Depends on |
|---|---|---|
| `pty_ws` raw attach | byte stream ↔ tmux `web` | tmux, dtach (unchanged) |
| `POST /api/terminal/upload` | store image, return ARES path | Flask `require_auth`, pool FS |
| `PTYTerminal` (Swift) | pty websocket + SwiftTerm feed/resize/reconnect | SwiftTerm, pty_ws |
| Tab strip (Swift) | list/select/new/kill tmux windows | `ctl` channel |
| Key accessory bar (Swift) | inject control byte sequences | `PTYTerminal.sendBytes` |
| Image picker/paste (Swift) | pick/paste → upload → inject path | PHPicker, upload endpoint |

## Error handling

- Upload endpoint: reject non-image / oversize with 4xx + message; client shows a toast, injects
  nothing.
- pty socket drop: show reconnecting state, backoff reconnect, re-send resize on reattach.
- Window list empty / tmux error: fall back to a single implicit window; never crash the view.

## Testing

- **Backend:** `test_terminal_upload.py` — happy path (image → path returned, file on disk),
  rejects non-image, rejects oversize, filename sanitization / no traversal.
- **Manual (owner, Mac+phone):** RAW opens by default on `web`; Claude's y/n permission prompt
  is visible and answerable via the `y` key; tab switch on phone moves the Mac's active window
  and vice-versa; accessory keys (Esc/Ctrl-C/arrows/Shift-Tab) work in Claude and vim; pick a
  Photo → path lands in the prompt and Claude reads it; paste a screenshot → same; rotate /
  toggle keyboard → no size corruption; kill the socket → auto-reconnects.

## Out of scope (YAGNI / follow-up)

- Deleting console/blocks/transcript mode and `console_loop` (demote now, delete later once RAW
  is proven).
- Multi-tmux-session browsing (owner chose the single shared `web` session / Mirror).
- Thumbnailing or previewing uploaded images in-app.

## Ponytail notes

- One primary input path (RAW), not two. We stop *relying* on the polled re-render; we don't add
  a parallel system.
- Reuse the existing `ctl` window channel and existing reconnect logic rather than inventing new
  ones.
- The upload endpoint is a thin multipart handler, not a media pipeline.
