# Native xterm.js Terminal — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the ARES terminal page's cross-origin `ttyd` iframe + per-keystroke HTTP control channel with a native, dashboard-themed `xterm.js` terminal wired directly to the existing `pty_ws` WebSocket.

**Architecture:** Vendor `xterm.js` + the fit addon into `static/vendor/`. In `templates/shell.html`, swap the `<iframe>` for an xterm mount, open the `/pty-ws/` WebSocket with `{"open":"web"}`, pipe binary frames both ways (`term.onData` → binary send; binary recv → `term.write`), send `{cols,rows}` on resize, repurpose the on-screen key row to send raw escape bytes, and theme xterm to the dashboard tokens. Frontend-only; `pty_ws.py` is unchanged. The tmux tab strip keeps using `ares-shell-ctl`.

**Tech Stack:** Flask/Jinja templates, vanilla JS, hand CSS, `xterm@5.3.0` + `xterm-addon-fit@0.8.0` (self-hosted). No build step.

## Global Constraints

- **Frontend only.** Do NOT modify `system/pty_ws.py` or any `.py` — the backend already meets the contract.
- **Do NOT modify `templates/home.html`** or other pages.
- **pty_ws protocol (exact):** first WS frame = text `{"open":"web"}` (attach tmux session `web`). Keystrokes/input = **binary** frames (text frames are parsed as JSON, so input MUST be binary). Resize = text `{"cols":N,"rows":M}`. Server→client: **binary** = PTY bytes → `term.write`; **text** = `{"windows":[…]}` control reply. WS URL over HTTPS = `wss://' + location.host + '/pty-ws/'`.
- **Self-host xterm** under `static/vendor/xterm/` (no runtime CDN dependency).
- **Preserve:** tmux tab strip (`ares-shell-ctl` via `/ctl`), image-paste→upload→inject, copy, reconnect.
- **Out of scope / do not attempt to port:** the page's auxiliary `block-view` / `claude-view` alternate renderings (they depend on the old `/capture` polling). Leave their markup/JS in place untouched if it doesn't interfere; the deliverable is the raw native terminal.
- **Deploy:** edit on the host; apply with `pct exec 101 -- systemctl restart ares`; propagate with `./deploy/sync-to-zeus.sh --restart`.
- **Commits:** subject line only, no body, no `Co-Authored-By`. Stage ONLY the files each task names — the repo has unrelated dirty files (`app.py`, `templates/home.html`, `.host_gpu.json`) that must stay uncommitted; never `git add -A`/`.`/`commit -a`.

## Verification note (applies to every task)

The standalone render harness can load the page and check the **static shell + JS wiring** (xterm instance created, mount + key row render, theme applied, no console reference errors) but **cannot** exercise live terminal I/O — the `/pty-ws/` WebSocket needs the real Caddy + Tailscale, unreachable from the `:8765` preview. So each task's automated test is a **render + console-clean** check; live I/O (typing, resize reflow, tabs, mobile keyboard, copy/paste) is a **human live-check list** produced at the end and run logged-in over Tailscale.

**Preview harness:** from repo root —
```bash
python3 -c "from jinja2 import Environment, FileSystemLoader; open('_preview.html','w').write(Environment(loader=FileSystemLoader('templates')).get_template('shell.html').render(boot={'hostname':'ARES'}))"
(python3 -m http.server 8765 --bind 0.0.0.0 >/tmp/prev.log 2>&1 &)
# Playwright → http://100.77.42.110:8765/_preview.html ; stop with: fuser -k 8765/tcp  (own line); rm -f _preview.html
```

## File structure

- **Create** `static/vendor/xterm/xterm.js`, `xterm.css`, `xterm-addon-fit.js` — self-hosted terminal library.
- **Modify** `templates/shell.html` — swap iframe→xterm mount, WS I/O, resize, key row, theme, copy/paste. This is the whole rebuild.

---

## Task 1: Vendor xterm.js and mount a bare terminal

**Files:**
- Create: `static/vendor/xterm/xterm.js`, `static/vendor/xterm/xterm.css`, `static/vendor/xterm/xterm-addon-fit.js`
- Modify: `templates/shell.html`

**Interfaces:**
- Produces: a global `window.Terminal` (xterm) and `window.FitAddon.FitAddon`; a mounted terminal at `#term` with a module-scoped `term` (xterm `Terminal`) and `fit` (`FitAddon`) that later tasks use.

- [ ] **Step 1: Download the pinned library files**

```bash
cd /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD
mkdir -p static/vendor/xterm
curl -fsSL https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js            -o static/vendor/xterm/xterm.js
curl -fsSL https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css           -o static/vendor/xterm/xterm.css
curl -fsSL https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js -o static/vendor/xterm/xterm-addon-fit.js
wc -c static/vendor/xterm/*    # expect three non-empty files (xterm.js ~280KB)
```

- [ ] **Step 2: Link the assets in `shell.html`'s `<head>`** — add after the existing stylesheet links:

```html
<link rel="stylesheet" href="/static/vendor/xterm/xterm.css">
<script src="/static/vendor/xterm/xterm.js"></script>
<script src="/static/vendor/xterm/xterm-addon-fit.js"></script>
```

- [ ] **Step 3: Replace the iframe with an xterm mount.** In the body, change the terminal element from `<iframe id="term" title="ARES shell" allow="clipboard-read; clipboard-write">…</iframe>` to:

```html
<div id="term" class="xterm-mount"></div>
```

Add layout CSS to the page's inline `<style>` so the mount fills the panel body:

```css
.xterm-mount { width:100%; height:100%; }
#term .xterm { height:100%; padding:8px 10px; }
#term .xterm-viewport::-webkit-scrollbar { width:9px; }
#term .xterm-viewport::-webkit-scrollbar-thumb { background:rgba(var(--ares-rgb),.28); }
```

- [ ] **Step 4: Instantiate the terminal.** Near the top of the page's main `<script>` IIFE (after the element lookups), replace the old `term.src = …` line and iframe-reconnect block with:

```js
var term = new Terminal({
    fontFamily: "'JetBrains Mono', monospace",
    fontSize: 13.5,
    lineHeight: 1.2,
    cursorBlink: true,
    cursorStyle: 'block',
    scrollback: 5000,
    allowProposedApi: true
});
var fit = new FitAddon.FitAddon();
term.loadAddon(fit);
term.open(document.getElementById('term'));
try { fit.fit(); } catch (e) {}
```

Delete the old iframe reconnect helpers (`_termHealth`, `_scheduleReconnect`, `_termSrc`, and the `term.addEventListener('error', …)` / `setInterval(_termHealth,…)` lines) — they reference an iframe that no longer exists.

- [ ] **Step 5: Test — bare terminal renders, no errors**

Render + serve (harness above). Playwright: navigate to `/_preview.html`; `browser_evaluate` returning `typeof window.Terminal + '/' + !!document.querySelector('#term .xterm')` → expect `"function/true"`. Screenshot: an empty dark terminal fills the panel. `browser_console_messages`: no reference/undefined errors (a failed `/pty-ws/` connection error is fine — that's Task 2). Stop server (`fuser -k 8765/tcp` own line); `rm -f _preview.html`.

- [ ] **Step 6: Commit**

```bash
git add static/vendor/xterm/xterm.js static/vendor/xterm/xterm.css static/vendor/xterm/xterm-addon-fit.js templates/shell.html
git commit -m "Terminal: vendor xterm.js and mount a native terminal (replaces iframe)"
```

---

## Task 2: Wire xterm ↔ pty_ws WebSocket (core I/O)

**Files:**
- Modify: `templates/shell.html`

**Interfaces:**
- Consumes: `term`, `fit` from Task 1; `PTY_WS_URL` (already defined in the page as `wss://' + location.host + '/pty-ws/'` over HTTPS).
- Produces: a module-scoped `ws` (the live WebSocket) and a `wsSend(str)` helper (encodes a string to a binary frame) that Tasks 3–5 use.

- [ ] **Step 1: Add the connect logic.** Replace the existing `_openPtyWs`/`_ptyWs` block with a single connect function. It attaches session `web`, streams binary both ways, parses text frames as `{windows}`, and reconnects with backoff:

```js
var ws = null;
var _wsRetry = 0;
function wsSend(s) {                                  // input MUST be binary frames
    if (ws && ws.readyState === 1) ws.send(new TextEncoder().encode(s));
}
function connectPty() {
    ws = new WebSocket(PTY_WS_URL);
    ws.binaryType = 'arraybuffer';                    // so PTY output arrives as ArrayBuffer
    ws.onopen = function () {
        _wsRetry = 0;
        ws.send(JSON.stringify({ open: 'web' }));     // attach tmux session "web"
        try { fit.fit(); ws.send(JSON.stringify({ cols: term.cols, rows: term.rows })); } catch (e) {}
        term.focus();
    };
    ws.onmessage = function (ev) {
        if (typeof ev.data === 'string') {            // control JSON (e.g. {windows:[...]})
            try { var d = JSON.parse(ev.data); if (d.windows) renderTabs(d.windows); } catch (e) {}
        } else {
            term.write(new Uint8Array(ev.data));      // raw PTY bytes
        }
    };
    ws.onclose = function () {
        _wsRetry = Math.min(_wsRetry + 1, 6);
        setTimeout(connectPty, 400 * _wsRetry);       // backoff reconnect
    };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
}
term.onData(function (data) { wsSend(data); });       // keystrokes → PTY
connectPty();
```

- [ ] **Step 2: Remove the redundant native input bar.** Typing now goes straight to the PTY, so delete the `<form id="inputbar">…<input id="line">…<button id="send"></button></form>` markup and its `form.addEventListener('submit', …)` handler + the `line`/`send` references. (The key row is handled in Task 4; do not remove it here.)

- [ ] **Step 3: Test — WS wiring present, no errors**

Render + serve. Playwright: `browser_evaluate` returning `typeof connectPty + '/' + typeof wsSend` → `"function/function"`. `browser_console_messages`: the only error may be the WS failing to reach `/pty-ws/` on `:8765` (expected in preview) — no reference/undefined errors. Screenshot still shows the terminal. Stop server; `rm -f _preview.html`.

- [ ] **Step 4: Commit**

```bash
git add templates/shell.html
git commit -m "Terminal: xterm <-> pty_ws binary I/O + reconnect; drop HTTP input bar"
```

---

## Task 3: Resize (fit addon → pty_ws)

**Files:**
- Modify: `templates/shell.html`

**Interfaces:**
- Consumes: `term`, `fit`, `ws` from Tasks 1–2.
- Produces: a `doFit()` helper.

- [ ] **Step 1: Add resize wiring** (after the connect logic):

```js
function doFit() {
    try {
        fit.fit();
        if (ws && ws.readyState === 1) ws.send(JSON.stringify({ cols: term.cols, rows: term.rows }));
    } catch (e) {}
}
window.addEventListener('resize', doFit);
new ResizeObserver(doFit).observe(document.getElementById('term'));
```

- [ ] **Step 2: Test — resize handler present**

Render + serve. Playwright: `browser_evaluate` returning `typeof doFit` → `"function"`; then call `browser_resize` to 900×600 and confirm no console errors and the terminal still fills the panel (screenshot). Stop server; `rm -f _preview.html`.

- [ ] **Step 3: Commit**

```bash
git add templates/shell.html
git commit -m "Terminal: fit-addon resize -> pty_ws {cols,rows}"
```

---

## Task 4: Mobile key row + soft-keyboard handling

**Files:**
- Modify: `templates/shell.html`

**Interfaces:**
- Consumes: `wsSend`, `term`, `doFit`.
- Produces: a working on-screen key row (mobile) that sends raw escape bytes, and `visualViewport` handling.

- [ ] **Step 1: Ensure the key-row markup exists** inside the panel, after the terminal mount (reuse/repurpose the existing `#keys` element; its buttons carry `data-seq`):

```html
<div class="keyrow" id="keys">
  <button class="k" data-k="esc">esc</button>
  <button class="k" data-k="tab">tab</button>
  <button class="k" data-k="ctrlc">ctrl-c</button>
  <button class="k" data-k="up">↑</button>
  <button class="k" data-k="down">↓</button>
  <button class="k" data-k="left">←</button>
  <button class="k" data-k="right">→</button>
  <button class="k" data-k="slash">/</button>
  <button class="k" data-k="pipe">|</button>
  <button class="k" data-k="tilde">~</button>
  <button class="k" data-k="bksp">⌫</button>
  <button class="k" id="kbd-copy">copy</button>
</div>
```

Buttons carry a key NAME (`data-k`) — the actual escape bytes live in a JS map so no control characters sit in the HTML. Replace the old keybar `#keys` click handler (which called `api('/key',…)`/`api('/scroll',…)`) with:

```js
var KEYSEQ = {
    esc: '\x1b', tab: '\t', ctrlc: '\x03', bksp: '\x7f',
    up: '\x1b[A', down: '\x1b[B', left: '\x1b[D', right: '\x1b[C',
    slash: '/', pipe: '|', tilde: '~'
};
document.getElementById('keys').addEventListener('click', function (e) {
    var b = e.target.closest('.k'); if (!b) return;
    if (b.id === 'kbd-copy') return;                  // handled in Task 5
    var seq = KEYSEQ[b.getAttribute('data-k')];
    if (seq != null) { wsSend(seq); term.focus(); }
});
```

- [ ] **Step 2: Key-row CSS** (in the page `<style>`; hidden on desktop, shown at mobile widths):

```css
.keyrow { display:none; }
@media (max-width:820px){
  .keyrow { display:flex; gap:6px; overflow-x:auto; padding:8px 10px calc(8px + var(--sab));
            border-top:1px solid rgba(var(--ares-rgb),.14); background:var(--bg-1); }
  .keyrow .k { flex:0 0 auto; min-width:42px; height:38px; display:grid; place-items:center;
               font:inherit; font-size:12px; color:var(--text-2);
               border:1px solid var(--ember-dim); background:rgba(var(--ares-rgb),.05); cursor:pointer; }
  .keyrow .k:active { border-color:var(--ares); color:var(--text-1); }
}
```

- [ ] **Step 3: Soft-keyboard handling.** xterm's own hidden textarea captures input; make it mobile-friendly and keep the terminal above the keyboard:

```js
if (term.textarea) {
    term.textarea.setAttribute('autocorrect', 'off');
    term.textarea.setAttribute('autocapitalize', 'off');
    term.textarea.setAttribute('spellcheck', 'false');
}
document.getElementById('term').addEventListener('click', function () { term.focus(); });
if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', function () {
        document.querySelector('.term-panel').style.maxHeight = window.visualViewport.height + 'px';
        doFit();
    });
}
```

- [ ] **Step 4: Test — key row renders at mobile width and wires up**

Render + serve. Playwright: `browser_resize` 402×740; screenshot → the key row shows at the bottom with esc/tab/ctrl-c/arrows/etc. `browser_evaluate`: click `esc` button and confirm no console error (the `wsSend` no-ops without a live WS, which is fine). Stop server; `rm -f _preview.html`.

- [ ] **Step 5: Commit**

```bash
git add templates/shell.html
git commit -m "Terminal: mobile key row (raw escape bytes) + soft-keyboard/visualViewport handling"
```

---

## Task 5: Theme, copy/paste, tab strip, deploy

**Files:**
- Modify: `templates/shell.html`

**Interfaces:**
- Consumes: everything above; the existing `renderTabs`/`api` tab-strip code (kept from the prior HUD rebuild).

- [ ] **Step 1: Apply the dashboard xterm theme.** Add to the `Terminal({...})` options from Task 1 (edit that object) a `theme`:

```js
theme: {
    background: '#070403', foreground: '#f6ece9',
    cursor: '#f5402d', cursorAccent: '#070403',
    selectionBackground: 'rgba(245,64,45,0.30)',
    black: '#1c0c09', red: '#f5402d', green: '#10b981', yellow: '#f59e0b',
    blue: '#7dd3fc', magenta: '#ff7a45', cyan: '#22d3ee', white: '#c99a90',
    brightBlack: '#5a3d37', brightRed: '#ff7a45', brightGreen: '#34d399', brightYellow: '#fbbf24',
    brightBlue: '#93c5fd', brightMagenta: '#ff9d5c', brightCyan: '#67e8f9', brightWhite: '#f6ece9'
}
```

- [ ] **Step 2: Native copy** — wire the `#kbd-copy` button to xterm's selection:

```js
document.getElementById('kbd-copy').addEventListener('click', function () {
    var sel = term.getSelection();
    if (sel && navigator.clipboard) navigator.clipboard.writeText(sel).catch(function () {});
});
```

- [ ] **Step 3: Image paste → inject over WS.** In the existing paste handler that uploads an image to `_scratch`, change the final injection from the old `api('/text', {text: path, enter:false})` to `wsSend(path)` (inject the host path straight into the PTY). Keep the upload logic unchanged.

- [ ] **Step 4: Confirm the tab strip still drives the session.** The existing `renderTabs`/`api('/select'|'/new'|'/kill', …)` code (over `/ctl`) selects windows in tmux session `web`; the attached xterm reflects the switch. Leave this code intact. (If `renderTabs` was referenced by the WS `onmessage` in Task 2, it already exists — verify no duplicate definition.)

- [ ] **Step 5: Test — themed terminal + copy button render**

Render + serve. Playwright: screenshot desktop (1440×860) → terminal reads in dashboard colors (warm-black bg, would show red cursor once connected), HUD panel + tab strip; mobile (402×740) → key row with a `copy` button. `browser_console_messages`: no reference errors. Stop server; `rm -f _preview.html`.

- [ ] **Step 6: Deploy + serve check**

```bash
pct exec 101 -- systemctl restart ares
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/terminal    # expect 200 or 302
./deploy/sync-to-zeus.sh --restart
```

- [ ] **Step 7: Commit**

```bash
git add templates/shell.html
git commit -m "Terminal: dashboard xterm theme, native copy, WS image-paste, tab-strip integration"
```

- [ ] **Step 8: Produce the human live-check list** (append to `.superpowers/sdd/terminal-native-report.md`, since live I/O is auth-gated). At `http://100.77.42.110:8080/terminal`, logged in over Tailscale: (1) typing appears instantly, `ls`/commands run; (2) scrollback is smooth; (3) resizing the window reflows the terminal (no wrong wrapping); (4) tab strip switches/creates/closes windows on ARES and ZEUS and the terminal follows; (5) on a phone: the key row works, the soft keyboard types reliably, the terminal stays above the keyboard; (6) drag-select + `copy` copies; image-paste injects a path; (7) killing/restarting the socket reconnects.

---

## Self-review notes

- **Spec coverage:** native xterm on pty_ws → Tasks 1–2; resize → 3; mobile key row + soft keyboard → 4; theme + copy + image-paste + tab strip + deploy → 5. "Dropped: iframe + `/key`/`/text`/`/scroll`/`/capture` + input bar" → iframe removed (T1), input bar removed (T2), key row no longer calls `/key`/`/scroll` (T4), copy uses `getSelection` not `/capture` (T5). Backend untouched (global constraint). `block-view`/`claude-view` explicitly out of scope.
- **Protocol consistency:** attach `{"open":"web"}`, binary input via `wsSend` (TextEncoder), `binaryType='arraybuffer'`, resize `{cols,rows}`, text→`{windows}` — used identically in Tasks 2–4.
- **No `git add -A`:** every task stages only its named files.
