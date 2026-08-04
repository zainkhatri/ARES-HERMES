# Terminal Blocks & Claudelog Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make command blocks and claudelog visible in the phone terminal — wire the existing pty_ws.py backend to a new rendering layer in shell.html.

**Architecture:** A single console-mode WebSocket to pty_ws (:7686) handles window kind metadata, block polling (800ms), and claudelog polling (1s). shell.html shows one of three views per active tab: ttyd iframe (raw), `#block-view` (shell kind), or `#claude-view` (claude kind). Write actions (select, kill, key, text, scroll) stay on shell_ctl HTTP. A Caddy proxy exposes pty_ws as wss:// for HTTPS access.

**Tech Stack:** Python (shell_ctl.py), Caddyfile (Caddy v2), vanilla JS + HTML/CSS (shell.html), websockets (already installed in pty_ws venv)

## Global Constraints

- tmux session name is `"web"` — hardcoded in both shell_ctl.py and the new JS
- pty_ws binds to `100.77.42.110:7686` — Tailscale IP only, never LAN-exposed
- Caddyfile edits go in `Caddyfile.proposed` (not the live Caddyfile) — the ops team applies it
- `/pty-ws/` Caddy proxy must be Tailscale-gated: `not remote_ip 100.64.0.0/10 fd7a:115c:a1e0::/48` → 403
- No new npm packages, no new Python packages
- Block ANSI strip regex: `/\x1b\[[0-9;]*[A-Za-z]/g` (strips all CSI sequences including SGR)
- Poll intervals: blocks = 800ms, claudelog = 1000ms
- Max assistant text displayed: 2000 chars (truncate with `…`)
- All CSS uses existing dashboard tokens: `--bg-0`, `--bg-1`, `--nav-ares`, `--nav-blue`, `JetBrains Mono`, `Bricolage Grotesque`

---

## File Map

| File | What changes |
|---|---|
| `Caddyfile.proposed` | Add `/pty-ws/*` Tailscale-gated WebSocket proxy (two server blocks) |
| `system/shell_ctl.py` | Add `BLOCK_RC` constant; fix `/new` ARES branch to use block-shell.rc |
| `templates/shell.html` | Add `#block-view` + `#claude-view` HTML; block/claude CSS; pty_ws WebSocket client; `setView()`; `_pollBlocks()` + `_renderBlocks()`; `_pollClaudelog()` + `_renderClaudelog()` |

---

### Task 1: Caddyfile — wss:// proxy for pty_ws

**Files:**
- Modify: `Caddyfile.proposed:46-66` (`:8443` block), `Caddyfile.proposed:81-101` (`:8080` block)

**Interfaces:**
- Produces: `wss://ares.tail3045df.ts.net/pty-ws/` and `ws://100.77.42.110:8080/pty-ws/` — reachable by browser WebSocket

- [ ] **Step 1: Add the proxy block to `:8443` server block**

In `Caddyfile.proposed`, find the `:8443` block. After the `handle_path /ctl/*` block (ending around line 65) and before `import static_media` (line 66), insert:

```caddyfile
	handle_path /pty-ws/* {
		route {
			@notts not remote_ip 100.64.0.0/10 fd7a:115c:a1e0::/48
			respond @notts "Forbidden" 403
			reverse_proxy 100.77.42.110:7686
		}
	}
```

- [ ] **Step 2: Add the same proxy block to `:8080` server block**

In `Caddyfile.proposed`, find the `:8080` block. After its `handle_path /ctl/*` block (around line 100) and before `import static_media` (line 101), insert the identical block:

```caddyfile
	handle_path /pty-ws/* {
		route {
			@notts not remote_ip 100.64.0.0/10 fd7a:115c:a1e0::/48
			respond @notts "Forbidden" 403
			reverse_proxy 100.77.42.110:7686
		}
	}
```

- [ ] **Step 3: Verify Caddy syntax**

```bash
caddy validate --config /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/Caddyfile.proposed
```

Expected: `Valid configuration` (exit 0). If caddy isn't on PATH: `/usr/bin/caddy validate ...`

- [ ] **Step 4: Write a WebSocket connectivity test**

Create `/tmp/test_pty_ws_proxy.py`:

```python
#!/usr/bin/env python3
"""Verify pty_ws is reachable directly (Caddy proxy tested separately after deploy)."""
import asyncio, json, sys
sys.path.insert(0, '/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD')

async def test():
    import websockets
    uri = 'ws://100.77.42.110:7686'
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({'list': True}))
        resp = json.loads(await ws.recv())
        assert 'sessions' in resp, f'Expected sessions key, got: {resp}'
        print(f'PASS: pty_ws reachable, sessions: {resp["sessions"]}')

asyncio.run(test())
```

- [ ] **Step 5: Run the test**

```bash
cd /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD
python3 /tmp/test_pty_ws_proxy.py
```

Expected: `PASS: pty_ws reachable, sessions: [...]`

- [ ] **Step 6: Commit**

```bash
git -C /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD \
  add Caddyfile.proposed && \
  git -C /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD \
  commit -m "Caddyfile: add /pty-ws/ Tailscale-gated WebSocket proxy for block/claudelog"
```

---

### Task 2: shell_ctl.py — fix /new ARES to emit OSC 133 markers

**Files:**
- Modify: `system/shell_ctl.py:26-29` (constants), `system/shell_ctl.py:117-129` (/new handler)

**Interfaces:**
- Produces: `POST /new {target:"ares"}` creates a tmux window running `bash --rcfile block-shell.rc -i` named `"shell"` with auto-rename off

- [ ] **Step 1: Write the test first**

Create `/tmp/test_shell_ctl_new.py`:

```python
#!/usr/bin/env python3
"""Verify /new creates a block-shell window. Requires shell_ctl to be running."""
import http.client, json, subprocess, time

conn = http.client.HTTPConnection('100.77.42.110', 7683, timeout=5)
conn.request('POST', '/new', json.dumps({'target': 'ares'}),
             {'Content-Type': 'application/json'})
resp = conn.getresponse()
assert resp.status == 200, f'Expected 200, got {resp.status}'
data = json.loads(resp.read())

shell_wins = [w for w in data['windows'] if w['name'] == 'shell']
assert shell_wins, (
    f'Expected window named "shell", got names: {[w["name"] for w in data["windows"]]}. '
    f'FAIL: /new still uses plain bash -l'
)

# Clean up: kill the window we just made
idx = shell_wins[-1]['index']
conn2 = http.client.HTTPConnection('100.77.42.110', 7683, timeout=5)
conn2.request('POST', '/kill', json.dumps({'index': str(idx)}),
              {'Content-Type': 'application/json'})
conn2.getresponse().read()

print(f'PASS: new ARES window name="shell" at index {idx}')
```

- [ ] **Step 2: Run test — verify it FAILS**

```bash
python3 /tmp/test_shell_ctl_new.py
```

Expected: `AssertionError: Expected window named "shell", got names: ['sh', ...]`
(Current code names it `sh` and uses `bash -l`.)

- [ ] **Step 3: Add BLOCK_RC constant and fix /new**

In `system/shell_ctl.py`, after line 29 (`HERMES_SSH = ...`), add:

```python
BLOCK_RC = "/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/system/block-shell.rc"
```

Then find the `/new` POST handler (around line 117). Replace:

```python
        if self.path == "/new":
            target = d.get("target", "ares")
            if target == "hermes":
                subprocess.run(
                    ["tmux", "new-window", "-t", SESSION, "-n", "hermes",
                     "ssh", HERMES_SSH],
                    capture_output=True)
            else:
                subprocess.run(
                    ["tmux", "new-window", "-t", SESSION, "-n", "sh",
                     "bash", "-l"],
                    capture_output=True)
            self._json({"windows": _windows()})
```

With:

```python
        if self.path == "/new":
            target = d.get("target", "ares")
            if target == "hermes":
                subprocess.run(
                    ["tmux", "new-window", "-t", SESSION, "-n", "hermes",
                     "ssh", HERMES_SSH],
                    capture_output=True)
            else:
                subprocess.run(
                    ["tmux", "new-window", "-t", SESSION, "-n", "shell",
                     f"bash --rcfile {BLOCK_RC} -i",
                     ";", "set-option", "-w", "automatic-rename", "off"],
                    capture_output=True)
            self._json({"windows": _windows()})
```

- [ ] **Step 4: Restart shell_ctl and run test — verify it PASSES**

```bash
# Restart shell_ctl (runs as a host service)
systemctl restart ares-shell-ctl 2>/dev/null || \
  pkill -f shell_ctl.py && sleep 1 && \
  nohup python3 /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/system/shell_ctl.py &

sleep 1
python3 /tmp/test_shell_ctl_new.py
```

Expected: `PASS: new ARES window name="shell" at index N`

- [ ] **Step 5: Commit**

```bash
git -C /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD \
  add system/shell_ctl.py && \
  git -C /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD \
  commit -m "shell_ctl: fix /new ARES to use block-shell.rc so OSC 133 markers fire"
```

---

### Task 3: shell.html — WebSocket client + view infrastructure

**Files:**
- Modify: `templates/shell.html` (HTML body + style block + script block)

**Interfaces:**
- Consumes: pty_ws console WebSocket (`ws://100.77.42.110:7686` or `wss://.../pty-ws/`); opening frame `{"open":"web","mode":"console"}`; control message `{"ctl":"windows"}` returns `{"windows":[{i,name,active,kind,src}]}`
- Produces: `setView(kind)` — switches visible panel; `_activeWin()` — returns active window index; `_windowKinds` — map of window index → kind; adapted `renderTabs()` accepting both pty_ws and shell_ctl window shapes

- [ ] **Step 1: Add `#block-view` and `#claude-view` divs to HTML**

In `templates/shell.html`, find:

```html
        <!-- Live PTY into the ARES host's persistent tmux session "web" (ttyd). -->
        <iframe id="term" title="ARES shell" allow="clipboard-read; clipboard-write"></iframe>
```

Replace with:

```html
        <!-- Live PTY into the ARES host's persistent tmux session "web" (ttyd). -->
        <iframe id="term" title="ARES shell" allow="clipboard-read; clipboard-write"></iframe>
        <!-- Block view: Warp-style command cards for shell-kind windows -->
        <div id="block-view"></div>
        <!-- Claude view: chat feed for claude-kind windows -->
        <div id="claude-view"></div>
```

- [ ] **Step 2: Add CSS for both views**

In the `<style>` block of `shell.html`, before the closing `</style>` tag, add:

```css
        /* ── Block view ─────────────────────────────────────────────────── */
        #block-view, #claude-view {
            flex: 1 1 auto;
            overflow-y: auto;
            background: var(--bg-0);
            display: none;
            flex-direction: column;
            gap: 6px;
            padding: 8px 10px;
            min-height: 0;
        }
        .block-card {
            background: var(--bg-1);
            border: 1px solid rgba(255,255,255,0.05);
            border-radius: 4px;
            overflow: hidden;
            font-family: 'JetBrains Mono', monospace;
        }
        .block-cmd {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 6px 10px;
            border-bottom: 1px solid rgba(255,255,255,0.04);
        }
        .block-cmd-empty { border-bottom: none; }
        .block-prompt { color: var(--nav-ares); font-weight: 700; font-size: 13px; flex-shrink: 0; }
        .block-cmd-text { flex: 1; color: #e2e8f0; font-size: 12px; word-break: break-all; }
        .block-exit { font-size: 10px; padding: 2px 6px; border-radius: 3px; font-weight: 700; flex-shrink: 0; }
        .exit-ok  { background: rgba(34,197,94,0.15); color: #4ade80; }
        .exit-err { background: rgba(239,68,68,0.15); color: #f87171; }
        .block-out {
            padding: 6px 10px; margin: 0;
            color: #8ba3c0; font-size: 11px;
            white-space: pre-wrap; word-break: break-all;
            max-height: 280px; overflow-y: auto;
            font-family: 'JetBrains Mono', monospace;
        }
        /* ── Claude view ─────────────────────────────────────────────────── */
        .claude-waiting {
            color: #4a5d78; font-family: 'JetBrains Mono', monospace;
            font-size: 11px; padding: 24px; text-align: center;
        }
        .claude-user, .claude-assistant {
            background: var(--bg-1); border: 1px solid rgba(255,255,255,0.05);
            border-radius: 4px; padding: 8px 10px;
            font-family: 'JetBrains Mono', monospace;
        }
        .claude-role {
            font-size: 9px; font-weight: 700; letter-spacing: 0.12em;
            margin-bottom: 5px;
        }
        .claude-user .claude-role   { color: var(--nav-blue); }
        .claude-assistant .claude-role { color: var(--nav-ares); }
        .claude-text {
            color: #c4d4e4; font-size: 12px;
            white-space: pre-wrap; word-break: break-word; line-height: 1.55;
            font-family: 'Bricolage Grotesque', sans-serif;
        }
        .claude-user .claude-text { font-size: 11px; color: #8ba3c0; }
        .claude-tool {
            color: #4a5d78; font-size: 10px;
            font-family: 'JetBrains Mono', monospace;
            padding: 2px 4px; word-break: break-all;
        }
```

- [ ] **Step 3: Add pty_ws vars and helper functions to the script block**

In `templates/shell.html`, find:

```js
        var onHttps = location.protocol === 'https:';
        // Over HTTPS (Tailscale): use same-origin /ctl and /shell/ paths.
        // Over HTTP (iOS app, direct LAN): use host IPs directly.
        var CTL  = onHttps ? '/ctl' : 'http://100.77.42.110:7683';
        var DASH = onHttps ? '' : 'http://192.168.20.213:8080';  // for /api/ routes
        var term = document.getElementById('term');
        term.src = onHttps ? '/shell/' : 'http://100.77.42.110:7681/';
```

Replace with:

```js
        var onHttps = location.protocol === 'https:';
        // Over HTTPS (Tailscale): use same-origin /ctl and /shell/ paths.
        // Over HTTP (iOS app, direct LAN): use host IPs directly.
        var CTL  = onHttps ? '/ctl' : 'http://100.77.42.110:7683';
        var DASH = onHttps ? '' : 'http://192.168.20.213:8080';  // for /api/ routes
        var PTY_WS_URL = onHttps
            ? 'wss://' + location.host + '/pty-ws/'
            : 'ws://100.77.42.110:7686';
        var SESSION_NAME = 'web';
        var term = document.getElementById('term');
        term.src = onHttps ? '/shell/' : 'http://100.77.42.110:7681/';

        // Per-window kind map populated from pty_ws windows responses.
        var _windowKinds = {};   // index (int) → "shell" | "claude" | null
        var _currentView = 'raw';
        var _blockPollTimer = null;
        var _claudePollTimer = null;
        var _ptyWs = null;
        var _ptyWsReady = false;

        function _activeWin() {
            var w = lastWindows.find(function (w) { return w.active; });
            if (!w) return null;
            return w.index !== undefined ? w.index : w.i;
        }

        function _winKind(idx) {
            return (idx !== null && idx !== undefined) ? (_windowKinds[idx] || 'raw') : 'raw';
        }

        function _stripAnsi(s) {
            return s.replace(/\x1b\[[0-9;]*[A-Za-z]/g, '');
        }

        function _escHtml(s) {
            return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        }
```

- [ ] **Step 4: Add `setView()` and `_openPtyWs()` after the existing `lastWindows` declaration**

Find in the script:

```js
        var lastWindows = [];
```

After that line, add:

```js

        function setView(kind) {
            clearInterval(_blockPollTimer);
            clearInterval(_claudePollTimer);
            _blockPollTimer = null;
            _claudePollTimer = null;
            _currentView = kind;
            var termEl   = document.getElementById('term');
            var blockEl  = document.getElementById('block-view');
            var claudeEl = document.getElementById('claude-view');
            termEl.style.display   = kind === 'raw'    ? 'block' : 'none';
            blockEl.style.display  = kind === 'shell'  ? 'flex'  : 'none';
            claudeEl.style.display = kind === 'claude' ? 'flex'  : 'none';
            if (kind === 'shell') {
                _pollBlocks();
                _blockPollTimer = setInterval(_pollBlocks, 800);
            }
            if (kind === 'claude') {
                _pollClaudelog();
                _claudePollTimer = setInterval(_pollClaudelog, 1000);
            }
        }

        function _openPtyWs() {
            try { if (_ptyWs) _ptyWs.close(); } catch (e) {}
            _ptyWs = new WebSocket(PTY_WS_URL);
            _ptyWs.onopen = function () {
                _ptyWsReady = true;
                _ptyWs.send(JSON.stringify({ open: SESSION_NAME, mode: 'console' }));
                _ptyWs.send(JSON.stringify({ ctl: 'windows' }));
            };
            _ptyWs.onmessage = function (e) {
                var d;
                try { d = JSON.parse(e.data); } catch (_) { return; }
                if (d.windows) {
                    // Merge kind info into _windowKinds; update tab strip.
                    d.windows.forEach(function (w) {
                        var idx = w.i !== undefined ? w.i : w.index;
                        if (idx !== undefined && w.kind) _windowKinds[idx] = w.kind;
                    });
                    renderTabs(d.windows);
                }
                if (d.blocks)    _renderBlocks(d.blocks);
                if (d.claudelog) _renderClaudelog(d.claudelog);
            };
            _ptyWs.onclose = function () {
                _ptyWsReady = false;
                setTimeout(_openPtyWs, 2000);
            };
        }

        function _ptyCtl(msg) {
            if (_ptyWsReady && _ptyWs && _ptyWs.readyState === 1) {
                _ptyWs.send(JSON.stringify(msg));
            }
        }
```

- [ ] **Step 5: Adapt `renderTabs` to handle pty_ws window shape**

Find:

```js
        function renderTabs(windows) {
            lastWindows = windows || [];
            Array.prototype.slice.call(tabsEl.querySelectorAll('.tab:not(.tab-add)'))
                .forEach(function (n) { n.remove(); });
            (windows || []).forEach(function (w) {
                var chip  = document.createElement('button');
                chip.className = 'tab' + (w.active ? ' active' : '');
                var label = document.createElement('span');
                label.textContent = (w.name || 'sh') + ' ' + w.index;
                chip.appendChild(label);
                chip.addEventListener('click', function (e) {
                    if (e.target.classList.contains('x')) return;
                    guard(api('/select', { index: String(w.index) }));
                });
```

Replace with:

```js
        function renderTabs(windows) {
            lastWindows = windows || [];
            Array.prototype.slice.call(tabsEl.querySelectorAll('.tab:not(.tab-add)'))
                .forEach(function (n) { n.remove(); });
            (windows || []).forEach(function (w) {
                // pty_ws uses {i, kind} shape; shell_ctl uses {index} shape.
                var idx = w.index !== undefined ? w.index : w.i;
                if (w.kind) _windowKinds[idx] = w.kind;
                var chip  = document.createElement('button');
                chip.className = 'tab' + (w.active ? ' active' : '');
                var label = document.createElement('span');
                label.textContent = (w.name || 'sh') + ' ' + idx;
                chip.appendChild(label);
                chip.addEventListener('click', function (e) {
                    if (e.target.classList.contains('x')) return;
                    guard(api('/select', { index: String(idx) }));
                    setView(_winKind(idx));
                });
```

Also update the `w.index` reference in the close-button handler inside `renderTabs`. Find inside the same function:

```js
                if (windows.length > 1) {
                    var x = document.createElement('span');
                    x.className = 'x'; x.textContent = '×';
                    x.addEventListener('click', function (e) {
                        e.stopPropagation();
                        if (confirm('Close session ' + w.index + '?'))
                            guard(api('/kill', { index: String(w.index) }));
                    });
```

Replace with:

```js
                if (windows.length > 1) {
                    var x = document.createElement('span');
                    x.className = 'x'; x.textContent = '×';
                    x.addEventListener('click', function (e) {
                        e.stopPropagation();
                        if (confirm('Close session ' + idx + '?'))
                            guard(api('/kill', { index: String(idx) }));
                    });
```

- [ ] **Step 6: Start pty_ws open and initial setView at page load**

Find the last two lines in the script block:

```js
        refresh();
        setInterval(refresh, 3000);
```

Replace with:

```js
        _openPtyWs();
        setView('raw');
        refresh();
        setInterval(refresh, 3000);
```

- [ ] **Step 7: Manual verification — WebSocket connects and kinds propagate**

Open the dashboard terminal page (`/terminal`) in a browser on the Tailscale network. Open DevTools → Console. Verify:
- No WebSocket errors (no `ERR_CONNECTION_REFUSED` or `Failed to connect`)
- `_windowKinds` is populated: type `_windowKinds` in Console, expect `{0: "shell", 1: "claude", ...}` or similar based on active tmux windows
- Tab strip renders correctly (window names + indexes match)

- [ ] **Step 8: Commit**

```bash
git -C /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD \
  add templates/shell.html && \
  git -C /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD \
  commit -m "shell.html: pty_ws WebSocket client, view infrastructure, adapted renderTabs"
```

---

### Task 4: shell.html — block renderer

**Files:**
- Modify: `templates/shell.html` (script block only)

**Interfaces:**
- Consumes: `_ptyCtl(msg)` from Task 3; `_activeWin()` from Task 3; `_stripAnsi(s)` from Task 3; `_escHtml(s)` from Task 3; `setView()` from Task 3 (already starts `_pollBlocks()` for shell-kind tabs)
- Consumes: pty_ws response `{"blocks": [{cmd, out, exit}]}` — up to 60 blocks, newest-last

- [ ] **Step 1: Add `_pollBlocks()` and `_renderBlocks()` to the script**

After the `_ptyCtl` function added in Task 3, add:

```js
        var _userScrolledBlocks = false;

        function _pollBlocks() {
            var win = _activeWin();
            if (win === null || _currentView !== 'shell') return;
            _ptyCtl({ ctl: 'blocks', win: win });
        }

        function _renderBlocks(blocks) {
            var el = document.getElementById('block-view');
            if (!el) return;
            var atBottom = el.scrollHeight - el.scrollTop <= el.clientHeight + 20;
            el.innerHTML = '';
            (blocks || []).forEach(function (b) {
                var hasOut = b.out && b.out.trim();
                var card = document.createElement('div');
                card.className = 'block-card';

                var hdr = document.createElement('div');
                hdr.className = 'block-cmd' + (hasOut ? '' : ' block-cmd-empty');

                var prompt = document.createElement('span');
                prompt.className = 'block-prompt';
                prompt.textContent = '❯';
                hdr.appendChild(prompt);

                var cmdText = document.createElement('span');
                cmdText.className = 'block-cmd-text';
                cmdText.textContent = _stripAnsi(b.cmd || '');
                hdr.appendChild(cmdText);

                if (b.exit !== null && b.exit !== undefined) {
                    var badge = document.createElement('span');
                    badge.className = 'block-exit ' + (b.exit === 0 ? 'exit-ok' : 'exit-err');
                    badge.textContent = String(b.exit);
                    hdr.appendChild(badge);
                }

                card.appendChild(hdr);

                if (hasOut) {
                    var pre = document.createElement('pre');
                    pre.className = 'block-out';
                    pre.textContent = _stripAnsi(b.out);
                    card.appendChild(pre);
                }

                el.appendChild(card);
            });

            if (!_userScrolledBlocks || atBottom) el.scrollTop = el.scrollHeight;
        }

        // Track manual upward scroll so auto-scroll doesn't fight the user.
        document.getElementById('block-view').addEventListener('scroll', function () {
            var el = this;
            _userScrolledBlocks = el.scrollHeight - el.scrollTop > el.clientHeight + 40;
        });
```

- [ ] **Step 2: Manual verification — blocks render**

In a browser session with Tailscale access:

1. Go to `/terminal`. Click `+ARES` to open a new shell tab.
2. The view should switch to `#block-view` (ttyd iframe disappears). If it doesn't, the tab's kind didn't resolve — in DevTools run `_windowKinds` to check.
3. Type `ls /mnt/nvme/PROMETHEUS` in the input bar and press Send.
4. Within 800ms a command card should appear showing `❯ ls /mnt/nvme/PROMETHEUS` with the directory listing below it and a green `0` exit badge.
5. Type `false` and Send. A card should appear with a red `1` badge and no output.
6. Scroll up — auto-scroll should stop. New commands should not force scroll. Scroll back to bottom — auto-scroll resumes.

- [ ] **Step 3: Commit**

```bash
git -C /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD \
  add templates/shell.html && \
  git -C /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD \
  commit -m "shell.html: block renderer — Warp-style command cards with exit badges"
```

---

### Task 5: shell.html — claudelog renderer

**Files:**
- Modify: `templates/shell.html` (script block only)

**Interfaces:**
- Consumes: `_ptyCtl(msg)` from Task 3; `_activeWin()` from Task 3; `_escHtml(s)` from Task 3; `setView()` from Task 3 (already starts `_pollClaudelog()` for claude-kind tabs)
- Consumes: pty_ws response `{"claudelog": {reset, waiting, events, src, win}}` where events is `[{role:"user"|"assistant"|"tool", text?, name?, detail?}]`

- [ ] **Step 1: Add `_pollClaudelog()` and `_renderClaudelog()` to the script**

After the `_renderBlocks` block added in Task 4, add:

```js
        var _claudeEvents = [];
        var _userScrolledClaude = false;

        function _pollClaudelog() {
            var win = _activeWin();
            if (win === null || _currentView !== 'claude') return;
            _ptyCtl({ ctl: 'claudelog', src: 'ares', win: win });
        }

        function _renderClaudelog(data) {
            var el = document.getElementById('claude-view');
            if (!el) return;
            var atBottom = el.scrollHeight - el.scrollTop <= el.clientHeight + 20;

            if (data.reset) {
                _claudeEvents = [];
                el.innerHTML = '';
            }

            if (data.waiting) {
                el.innerHTML = '<div class="claude-waiting">Waiting for Claude…</div>';
                return;
            }

            if (data.events && data.events.length) {
                _claudeEvents = _claudeEvents.concat(data.events);
                _rebuildClaudeView(el);
            }

            if (!_userScrolledClaude || atBottom) el.scrollTop = el.scrollHeight;
        }

        function _rebuildClaudeView(el) {
            el.innerHTML = '';
            _claudeEvents.forEach(function (ev) {
                var div = document.createElement('div');
                if (ev.role === 'user') {
                    div.className = 'claude-user';
                    div.innerHTML =
                        '<div class="claude-role">YOU</div>' +
                        '<div class="claude-text">' + _escHtml(ev.text || '') + '</div>';
                } else if (ev.role === 'assistant') {
                    var txt = (ev.text || '');
                    var truncated = txt.length > 2000;
                    div.className = 'claude-assistant';
                    div.innerHTML =
                        '<div class="claude-role">CLAUDE</div>' +
                        '<div class="claude-text">' +
                        _escHtml(truncated ? txt.slice(0, 2000) + '…' : txt) +
                        '</div>';
                } else if (ev.role === 'tool') {
                    div.className = 'claude-tool';
                    div.textContent = '[' + (ev.name || 'tool') + '] ' + (ev.detail || '');
                } else {
                    return;
                }
                el.appendChild(div);
            });
        }

        document.getElementById('claude-view').addEventListener('scroll', function () {
            var el = this;
            _userScrolledClaude = el.scrollHeight - el.scrollTop > el.clientHeight + 40;
        });
```

- [ ] **Step 2: Manual verification — claudelog renders**

In a browser session with Tailscale access:

1. In a separate tmux window (on ARES), start `claude` in any directory so it's running.
2. Go to `/terminal`. The tab for that window should have `kind="claude"` — verify with `_windowKinds` in DevTools.
3. Click on the Claude tab. The view should switch to `#claude-view`.
4. Within 1s, the chat feed should appear showing the current conversation — user prompts as `YOU` blocks, Claude responses as `CLAUDE` blocks, tool calls as dim `[Bash] grep ...` lines.
5. Type a message to Claude via the input bar. Within 1s of Claude starting to reply, new events should appear.
6. Verify the "waiting" state: start a fresh `claude` session that hasn't received its first message yet. Switch to that tab. The view should show `Waiting for Claude…`.

- [ ] **Step 3: Commit**

```bash
git -C /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD \
  add templates/shell.html && \
  git -C /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD \
  commit -m "shell.html: claudelog renderer — chat feed with user/assistant/tool events"
```
