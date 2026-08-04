# Terminal — Command Blocks & Claudelog Rendering

**Date:** 2026-08-03  
**Scope:** `templates/shell.html`, `system/shell_ctl.py`, `Caddyfile.proposed`  
**Goal:** Make command blocks and claudelog actually visible in the phone terminal — the backend in `pty_ws.py` is complete; the frontend display is missing.

---

## Root Cause

`pty_ws.py` (:7686) produces structured data (block dicts, claudelog events) via WebSocket in console mode. `shell.html` never connects to pty_ws — it uses `shell_ctl.py` (:7683, HTTP) for tab management and shows raw terminal output via a ttyd iframe. Two bugs compound this:

1. `shell_ctl /new` for ARES creates windows with plain `bash -l`, not `bash --rcfile block-shell.rc`, so phone-created tabs emit zero OSC 133 markers.
2. There is no block or claudelog renderer in shell.html.

---

## What Changes

### 1. Caddyfile: proxy pty_ws as wss://

pty_ws binds to `100.77.42.110:7686`. The browser refuses `ws://` when the page is on HTTPS. Add a Tailscale-gated WebSocket proxy to the Caddyfile (both `:8443` and `:8080` blocks, same pattern as the `/shell/` proxy):

```caddyfile
handle_path /pty-ws/* {
    route {
        @notts not remote_ip 100.64.0.0/10 fd7a:115c:a1e0::/48
        respond @notts "Forbidden" 403
        reverse_proxy 100.77.42.110:7686
    }
}
```

Place this **before** the `import static_media` line in each server block. Caddy handles WebSocket upgrade automatically via `reverse_proxy`.

Client URL in shell.html:
```js
var PTY_WS_URL = location.protocol === 'https:'
    ? 'wss://' + location.host + '/pty-ws/'
    : 'ws://100.77.42.110:7686';
```

### 2. shell_ctl.py: fix `/new` for ARES

Current code creates `bash -l` — no OSC 133 markers.

Replace the ARES branch of `/new`:

```python
BLOCK_RC = "/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/system/block-shell.rc"

# in do_POST, path == "/new", target == "ares":
subprocess.run(
    ["tmux", "new-window", "-t", SESSION, "-n", "shell",
     f"bash --rcfile {BLOCK_RC} -i",
     ";", "set-option", "-w", "automatic-rename", "off"],
    capture_output=True)
```

HERMES branch stays unchanged (`ssh HERMES_SSH`).

### 3. shell.html: pty_ws WebSocket client + renderers

#### 3a. Console WebSocket

Open one WebSocket to pty_ws in console mode on page load. Send the opening frame `{"open": "web", "mode": "console"}` (`"web"` is the tmux session name — verified from `SESSION = "web"` in shell_ctl.py). Reconnect with 2s backoff on drop.

```js
var _ptyWs = null;
var _ptyWsReady = false;
var SESSION_NAME = 'web';

function _openPtyWs() {
    _ptyWs = new WebSocket(PTY_WS_URL);
    _ptyWs.onopen = function () {
        _ptyWs.send(JSON.stringify({ open: SESSION_NAME, mode: 'console' }));
        _ptyWsReady = true;
    };
    _ptyWs.onmessage = function (e) {
        var d = JSON.parse(e.data);
        if (d.blocks)      _renderBlocks(d.blocks);
        if (d.claudelog)   _renderClaudelog(d.claudelog);
        if (d.windows)     renderTabs(d.windows);  // same renderTabs, kind now included
    };
    _ptyWs.onclose = function () {
        _ptyWsReady = false;
        setTimeout(_openPtyWs, 2000);
    };
}
```

`SESSION_NAME` is hardcoded `'web'` — the tmux session name shared by shell_ctl.py and pty_ws.py. No Flask template variable needed.

#### 3b. View switching

Two display elements live side-by-side under the tab strip; only one is visible at a time:

```html
<iframe id="term" ...></iframe>
<div id="block-view" style="display:none; flex:1; overflow-y:auto; padding:8px 10px;"></div>
<div id="claude-view" style="display:none; flex:1; overflow-y:auto; padding:10px 12px;"></div>
```

When a tab is selected, `setView(kind)` is called:

```js
function setView(kind) {
    var termEl   = document.getElementById('term');
    var blockEl  = document.getElementById('block-view');
    var claudeEl = document.getElementById('claude-view');
    termEl.style.display   = kind === 'raw'    ? 'block' : 'none';
    blockEl.style.display  = kind === 'shell'  ? 'flex'  : 'none';
    claudeEl.style.display = kind === 'claude' ? 'flex'  : 'none';
    _currentView = kind;
    if (kind === 'shell')  _pollBlocks();
    if (kind === 'claude') _pollClaudelog();
}
```

`kind` comes from the windows list returned by pty_ws (`kind: "shell" | "claude" | null`). Windows without a kind (pre-existing tmux sessions, HERMES tabs) fall back to `kind = 'raw'`.

#### 3c. Tabs from pty_ws

pty_ws `_windows()` already returns `{i, name, active, kind, src}`. The `renderTabs` function already works from shell_ctl's `{index, name, active}`. Adapt it to accept either shape (`w.index ?? w.i` for the index field). When a tab is clicked, `setView(w.kind || 'raw')` is called alongside the existing `/select` fetch to shell_ctl.

Initial windows poll: send `{"ctl": "windows"}` over the pty_ws console socket after it opens. Subsequent refreshes keep the existing 3s `setInterval(refresh, 3000)` for fallback, but the pty_ws socket also sends updated windows on every `newwin`/`killwin` response.

#### 3d. Block renderer

Poll interval: 800ms while `_currentView === 'shell'`, via `setInterval(_pollBlocks, 800)` (clear on view switch). Send `{"ctl": "blocks", "win": activeWindowIndex}`.

`_renderBlocks(blocks)`:
- Receives newest-last array of `{cmd, out, exit}` dicts (up to 60).
- Full-replace the `#block-view` contents on each poll (simple, avoids dedup complexity; 60 cards is fast to re-render).
- Strip ANSI client-side before rendering: `s.replace(/\x1b\[[0-9;]*m/g, '')`.
- Each card:

```html
<div class="block-card">
  <div class="block-cmd">
    <span class="block-prompt">❯</span>
    <span class="block-cmd-text">ls -la</span>
    <span class="block-exit exit-0">0</span>   <!-- green if 0, red otherwise -->
  </div>
  <pre class="block-out">total 48\n...</pre>  <!-- omitted if out is empty -->
</div>
```

CSS for block cards uses the existing dashboard tokens (`--bg-1`, `--bg-2`, `--nav-ares`, `--nav-blue`, `JetBrains Mono`). Empty-output blocks (bare commands like `cd`) show only the cmd row, no `<pre>`.

Auto-scroll: after re-render, scroll `#block-view` to bottom unless the user has scrolled up (track with a scroll listener that sets `_userScrolledBlocks = true` on upward scroll; reset to false when scroll hits bottom).

#### 3e. Claudelog renderer

Poll interval: 1s while `_currentView === 'claude'`, via `setInterval(_pollClaudelog, 1000)`. Send `{"ctl": "claudelog", "src": "ares", "win": activeWindowIndex}`.

`_renderClaudelog(data)`:
- `data.reset === true` → clear `#claude-view` and reset local event array.
- `data.waiting === true` → show a single "Waiting for Claude..." dim line, no other content.
- `data.events` → append to local array, re-render.

Event rendering by role:

| Role | Appearance |
|---|---|
| `"user"` | Dim header `YOU`, text in a muted box |
| `"assistant"` | Header `CLAUDE`, text in slightly lighter box, `white-space: pre-wrap` |
| `"tool"` | Single line chip: `[tool name] detail` — JetBrains Mono, dim, no box |

Full-replace on `reset`, append-and-re-render on incremental events. Auto-scroll to bottom same pattern as blocks.

Truncate long assistant text at 2000 chars with a "…" suffix (the backend caps at 8000 already, but long responses shouldn't dominate the view).

---

## What Doesn't Change

- ttyd iframe stays for `raw` kind windows (HERMES ssh, pre-existing tmux sessions)
- keybar stays unchanged (Escape, Tab, ^C, scroll, tabs — all work for both raw and block views via existing `/key` and `/scroll` shell_ctl endpoints)
- inputbar stays (text injection via shell_ctl `/text` works for all views)
- Auth, copy, image paste — unchanged

---

## File Changelist

| File | Change |
|---|---|
| `Caddyfile.proposed` | Add `/pty-ws/` Tailscale-gated WebSocket proxy in both server blocks |
| `system/shell_ctl.py` | Fix `/new` ARES branch: `bash --rcfile block-shell.rc -i` + `automatic-rename off` |
| `templates/shell.html` | Add pty_ws WebSocket client, `#block-view`, `#claude-view`, block/claudelog renderers, view switching, adapt `renderTabs` to pty_ws window shape |

---

## Out of Scope

- HERMES claudelog in this UI (HERMES tab is `raw` kind → ttyd; the full HERMES claudelog feature is a separate tab type)
- Streaming (server push): the claudelog/blocks poll model stays request-response; WebSocket push would require pty_ws to track which windows have open console listeners and push on file change — separate project
- ANSI colors in block output: stripped client-side for now; add ansi-to-html if color turns out to matter
- Block history persistence: `/tmp/ares-blocks/` is ephemeral; blocks vanish on reboot — separate project
