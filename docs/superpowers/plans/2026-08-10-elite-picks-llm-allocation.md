# Elite Picks — LLM Scoring & Dynamic Allocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed weighted-conviction ranking with an llama-judged 0–100 score per stock, split a budget dynamically across only the names that clear a conviction bar, and add an on-demand Claude-Code-CLI deep-research pass.

**Architecture:** Two independent tiers. Tier 1 (weekly, cheap): `elite_picks.py` adds one batched llama call that scores candidates, with a deterministic data-score fallback; the score is cached into `elite_picks.json`. Tier 2 (on-demand, expensive): an async, disk-backed `app.py` route shells out to `claude -p` for a per-stock brief. Budget allocation is pure client-side JS extracted into a testable module.

**Tech Stack:** Python 3 (stdlib + `requests`), Ollama `llama3.1:8b` on VM300, Claude Code CLI (`claude -p`), vanilla JS (no framework), Flask/gunicorn in LXC 101.

## Global Constraints

- **No test framework.** Repo uses standalone `assert`-based scripts. Python tests: `python3 tests/<name>.py` (exit 0 + print `OK`). JS tests: `node tests/<name>.mjs`.
- **Power of Ten spirit:** bounded loops, ≥2 assertions per non-trivial function, validate params, check return values, functions ≤ ~60 lines.
- **`elite_picks.py` stays standalone** — no Flask/CLIP imports (the weekly cron imports it cheaply). It may import `os`, `re`, `json`, `time`, `collections`, `concurrent.futures`, `requests` only.
- **Git:** subject-line-only commits, NO body, NO `Co-Authored-By`. Branch off `main` before the first commit (`git checkout -b feat/elite-picks-llm`).
- **Deploy:** files are host-side, bind-mounted into LXC 101. `system/watcher.py` auto-restarts `ares` on `.py` change; template/JS changes need `pct exec 101 -- systemctl restart ares.service`.
- **Ollama:** `OLLAMA_HOST` default `http://192.168.20.212:11434`, `OLLAMA_MODEL` default `llama3.1:8b`.
- **Constants (verbatim):** `BAR=65`, `GAMMA=2`, `MAX_FUNDED=10`, llama `temperature=0.2 seed=42 timeout=90`, deep-research `timeout=180`, running-marker TTL `300`s, max concurrent deep jobs `2`.
- **`claude -p` JSON shape:** `{result: str, is_error: bool, usage: {...}, ...}` — parse `.result`, check `.is_error`.

## File Structure

- **Modify** `elite_picks.py` — add `_llm_scores()`, `_attach_llm_scores()`, constants; wire both into `compute_elite_picks()`.
- **Create** `static/elite_alloc.js` — pure `allocateBudget()` (budget split math), UMD export for node tests.
- **Modify** `templates/breakdown.html` — load `elite_alloc.js`; rewrite `draw()`/`rowHtml()` to use `llm_score`; remove the 3/5/10/All selector; show llama reason; add the deep-research expander + polling.
- **Modify** `app.py` — add `_deep_cache_paths()`, `_deep_lookup()`, `_deep_generate()`, and the `/api/elite-picks/deep/<ticker>` route.
- **Create** `tests/test_elite_scoring.py`, `tests/test_elite_alloc.mjs`, `tests/test_deep_research.py`.
- **Create dir** `ai_data/deep_research/`.

**Task order & deps:** Task 1 (alloc math) and Task 2 (llama scoring) are independent. Task 3 (UI scoring) needs 1 + 2. Task 4 (deep backend) is independent. Task 5 (deep UI) needs 4.

---

### Task 0: Branch

- [ ] **Step 1: Create the feature branch**

Run:
```bash
cd /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD
git checkout -b feat/elite-picks-llm
mkdir -p tests ai_data/deep_research
```
Expected: on branch `feat/elite-picks-llm`, dirs exist.

---

### Task 1: Budget allocation math (`static/elite_alloc.js`)

**Files:**
- Create: `static/elite_alloc.js`
- Test: `tests/test_elite_alloc.mjs`

**Interfaces:**
- Produces: `allocateBudget(picks, budget, opts) -> Array<{funded:boolean, amount:number, barPct:number}>`, index-aligned to `picks`. Each `pick` has a numeric `llm_score` (0–100). `opts` may override `{BAR, GAMMA, MAX_FUNDED}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_elite_alloc.mjs`:
```js
import { allocateBudget } from '../static/elite_alloc.js';
function eq(a, b, m) { if (a !== b) { console.error(`FAIL ${m}: ${a} !== ${b}`); process.exit(1); } }

// sums to budget exactly
const picks = [{llm_score:92},{llm_score:88},{llm_score:79},{llm_score:70},{llm_score:61}];
const a = allocateBudget(picks, 1000, {});
eq(a.reduce((s,x)=>s+x.amount,0), 1000, 'sum==budget');
// below-bar (61) unfunded, others funded
eq(a[4].funded, false, '61 below bar');
eq(a[0].funded, true, '92 funded');
// monotonic: higher score -> >= amount among funded
if (!(a[0].amount >= a[1].amount && a[1].amount >= a[2].amount)) { console.error('FAIL monotonic'); process.exit(1); }
// barPct is the absolute score
eq(a[0].barPct, 92, 'barPct==score');
// empty-funded edge: nothing clears BAR -> fund single top
const b = allocateBudget([{llm_score:10},{llm_score:5}], 500, {});
eq(b[0].funded, true, 'top funded when none clear');
eq(b[1].funded, false, 'second not funded');
eq(b[0].amount, 500, 'all budget to top');
// MAX_FUNDED cap
const many = Array.from({length:15}, () => ({llm_score:90}));
const c = allocateBudget(many, 1000, {});
eq(c.filter(x=>x.funded).length, 10, 'max 10 funded');
console.log('OK');
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node tests/test_elite_alloc.mjs`
Expected: FAIL — `Cannot find module '../static/elite_alloc.js'`.

- [ ] **Step 3: Write the module**

Create `static/elite_alloc.js`:
```js
// Pure budget-allocation math for Elite Picks. Split `budget` across only the
// picks whose llm_score clears BAR, weighted by score^GAMMA. Used by
// breakdown.html and node tests. No DOM, no globals beyond the export.
function allocateBudget(picks, budget, opts) {
  const BAR = (opts && opts.BAR != null) ? opts.BAR : 65;
  const GAMMA = (opts && opts.GAMMA != null) ? opts.GAMMA : 2;
  const MAX_FUNDED = (opts && opts.MAX_FUNDED != null) ? opts.MAX_FUNDED : 10;
  console.assert(Array.isArray(picks), 'picks must be an array');
  console.assert(budget >= 0, 'budget must be >= 0');

  const scored = picks.map((p, i) => ({ i, score: Number(p.llm_score) || 0 }));
  let funded = scored.filter(s => s.score >= BAR)
                     .sort((a, b) => b.score - a.score)
                     .slice(0, MAX_FUNDED);
  if (funded.length === 0 && scored.length) {
    funded = [scored.slice().sort((a, b) => b.score - a.score)[0]];
  }
  const fundedIdx = new Set(funded.map(s => s.i));
  const wsum = funded.reduce((s, f) => s + Math.pow(f.score, GAMMA), 0) || 1;

  const amounts = picks.map(() => 0);
  funded.forEach(f => { amounts[f.i] = Math.round(budget * Math.pow(f.score, GAMMA) / wsum); });
  if (funded.length) {                       // push rounding remainder onto the top pick
    const top = funded[0].i;
    amounts[top] += budget - amounts.reduce((s, a) => s + a, 0);
  }
  return picks.map((p, i) => ({
    funded: fundedIdx.has(i),
    amount: amounts[i],
    barPct: Math.max(0, Math.min(100, Number(p.llm_score) || 0)),
  }));
}
if (typeof module !== 'undefined' && module.exports) module.exports = { allocateBudget };
if (typeof window !== 'undefined') window.allocateBudget = allocateBudget;
export { allocateBudget };
```

Note: the file uses both `export` and `module.exports` so the browser `<script type="module">`/node ESM import and a plain `<script>` both work. The node test imports via ESM.

- [ ] **Step 4: Run test to verify it passes**

Run: `node tests/test_elite_alloc.mjs`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add static/elite_alloc.js tests/test_elite_alloc.mjs
git commit -m "feat: pure budget allocation module for elite picks"
```

---

### Task 2: llama scoring in `elite_picks.py`

**Files:**
- Modify: `elite_picks.py` (add constants near the other module constants; add `_llm_scores` and `_attach_llm_scores`; call both inside `compute_elite_picks`)
- Test: `tests/test_elite_scoring.py`

**Interfaces:**
- Consumes: existing `compute_elite_picks` internals — each pick dict already has `ticker`, `name`, `buyers`, `firms:[{name,heavy}]`, `momentum`, `upside`, `score` (weighted conviction int).
- Produces:
  - `_llm_scores(candidates: list[dict]) -> dict[str, dict]` → `{TICKER: {"score": int0_100, "reason": str}}`; `{}` on any failure.
  - `_attach_llm_scores(picks: list[dict], llm_map: dict) -> list[dict]` → mutates picks adding `data_score:int`, `llm_score:int`, `llm_reason:str`; sorts by `llm_score` desc; sets `rank`. Returns the list.
  - `compute_elite_picks` result gains per-pick `data_score/llm_score/llm_reason` and `result["sources"]["llm"]: bool`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_elite_scoring.py`:
```python
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import elite_picks as ep

class _FakeResp:
    def __init__(self, code, payload): self.status_code = code; self._p = payload
    def json(self): return self._p

def test_llm_scores_parses_and_clamps():
    payload = {"response": json.dumps({"scores": [
        {"ticker": "AMZN", "score": 150, "reason": "many elites, strong momentum"},
        {"ticker": "v", "score": "88", "reason": "legends only"},
        {"ticker": "BAD", "score": None, "reason": "x"},        # dropped: bad score
        {"ticker": "NORE", "score": 50, "reason": ""},           # dropped: empty reason
    ]})}
    ep.requests = type("R", (), {"post": staticmethod(lambda *a, **k: _FakeResp(200, payload))})
    out = ep._llm_scores([{"ticker": "AMZN", "name": "Amazon", "buyers": 9, "firms": [], "momentum": None, "upside": None}])
    assert out["AMZN"]["score"] == 100, out          # clamped 150->100
    assert out["V"]["score"] == 88                    # upper-cased + coerced
    assert "BAD" not in out and "NORE" not in out
    print("  llm_scores OK")

def test_llm_scores_failure_returns_empty():
    def boom(*a, **k): raise RuntimeError("ollama down")
    ep.requests = type("R", (), {"post": staticmethod(boom)})
    assert ep._llm_scores([{"ticker": "X", "name": "X", "buyers": 1, "firms": []}]) == {}
    print("  llm_scores failure OK")

def test_attach_uses_llm_then_falls_back():
    picks = [
        {"ticker": "A", "score": 10}, {"ticker": "B", "score": 20}, {"ticker": "C", "score": 5},
    ]
    ep._attach_llm_scores(picks, {"A": {"score": 95, "reason": "r"}})
    by = {p["ticker"]: p for p in picks}
    assert by["A"]["llm_score"] == 95 and by["A"]["llm_reason"] == "r"   # llama value
    assert by["B"]["llm_score"] == 100                                   # fallback: 20 is max -> 100
    assert by["C"]["llm_score"] == 25                                    # fallback: 5/20*100
    assert picks[0]["ticker"] == "A" and picks[0]["rank"] == 1           # sorted by llm_score
    print("  attach OK")

if __name__ == "__main__":
    test_llm_scores_parses_and_clamps()
    test_llm_scores_failure_returns_empty()
    test_attach_uses_llm_then_falls_back()
    print("OK")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_elite_scoring.py`
Expected: FAIL — `AttributeError: module 'elite_picks' has no attribute '_llm_scores'`.

- [ ] **Step 3: Add constants + functions to `elite_picks.py`**

Add near the top constants (after the `WORKERS`/`_UA` block), the module must already `import requests` at top (it does). Add:
```python
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://192.168.20.212:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
BAR = 65                 # mirrored in static/elite_alloc.js — keep in sync
GAMMA = 2
MAX_FUNDED = 10

_LLM_PROMPT = (
    "You are scoring stocks by how strongly elite investors are accumulating them.\n"
    "For EACH stock return an integer score 0-100 driven by the signals: more "
    "elite_buyers and more heavy_hitters => higher; positive momentum_3mo and "
    "analyst_upside nudge higher. You may use general knowledge of the company for "
    "the reason and tie-breaking, but DO NOT invent prices, earnings, dates, or news. "
    "Keep each reason <= 15 words. Return JSON only, no prose:\n"
    '{"scores":[{"ticker":"AAA","score":0,"reason":"..."}]}\n\nSTOCKS:\n'
)
```

Then add the two functions (place them above `compute_elite_picks`):
```python
def _llm_scores(candidates):
    """Batched llama rating. Returns {TICKER: {'score':int, 'reason':str}}, or {} on failure."""
    assert isinstance(candidates, list), "candidates must be a list"
    if not candidates:
        return {}
    items = [{"ticker": c["ticker"], "name": c["name"], "elite_buyers": c["buyers"],
              "heavy_hitters": [f["name"] for f in c.get("firms", []) if f.get("heavy")],
              "momentum_3mo": c.get("momentum"), "analyst_upside": c.get("upside")}
             for c in candidates[:MAX_CANDIDATES]]
    try:
        r = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": _LLM_PROMPT + json.dumps(items),
                  "stream": False, "format": "json",
                  "options": {"temperature": 0.2, "seed": 42}},
            timeout=90)
        if r.status_code != 200:
            return {}
        raw = json.loads(r.json().get("response", "{}"))
        out = {}
        for s in (raw.get("scores") or [])[:MAX_CANDIDATES]:
            t = str(s.get("ticker", "")).upper().strip()
            reason = str(s.get("reason", "")).strip()[:120]
            if not t or not reason:
                continue
            try:
                sc = int(round(float(s.get("score"))))
            except (TypeError, ValueError):
                continue
            out[t] = {"score": max(0, min(100, sc)), "reason": reason}
        return out
    except Exception:
        return {}


def _attach_llm_scores(picks, llm_map):
    """Add data_score (normalized) + llm_score/llm_reason (llama or fallback); sort + rank."""
    assert isinstance(picks, list), "picks must be a list"
    assert isinstance(llm_map, dict), "llm_map must be a dict"
    maxw = max((p.get("score", 0) for p in picks), default=1) or 1
    for p in picks:
        p["data_score"] = round(100 * p.get("score", 0) / maxw)
        hit = llm_map.get(p["ticker"])
        p["llm_score"] = hit["score"] if hit else p["data_score"]
        p["llm_reason"] = hit["reason"] if hit else ""
    picks.sort(key=lambda p: p["llm_score"], reverse=True)
    for i, p in enumerate(picks):
        p["rank"] = i + 1
    return picks
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_elite_scoring.py`
Expected: `OK`.

- [ ] **Step 5: Wire into `compute_elite_picks`**

In `elite_picks.py`, find the block that finishes the picks list (currently):
```python
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:     # bounded workers
        picks = list(ex.map(enrich, cands))
    picks.sort(key=lambda p: p["score"], reverse=True)
    for i, p in enumerate(picks):
        p["rank"] = i + 1

    result = {"picks": picks[:20], "quarter": quarter, "generated_ts": time.time(),
              "sources": {"superinvestor": True,
                          "momentum": any(p["momentum"] is not None for p in picks),
                          "congress": bool(congress),
                          "analyst": any(p["upside"] is not None for p in picks)}}
```
Replace it with:
```python
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:     # bounded workers
        picks = list(ex.map(enrich, cands))

    llm_map = _llm_scores(picks)                            # batched llama judgment
    _attach_llm_scores(picks, llm_map)                      # adds llm_score/reason, sorts, ranks

    result = {"picks": picks[:20], "quarter": quarter, "generated_ts": time.time(),
              "sources": {"superinvestor": True,
                          "momentum": any(p["momentum"] is not None for p in picks),
                          "congress": bool(congress),
                          "analyst": any(p["upside"] is not None for p in picks),
                          "llm": bool(llm_map)}}
```

- [ ] **Step 6: Live smoke test against real Ollama**

Run: `.venv/bin/python elite_picks.py`
Expected: prints `[elite-picks] 20 picks | Q1 2026 | sources={... 'llm': True} | err=None` (or `'llm': False` if VM300 is unreachable — that is the valid fallback path, not a failure).

- [ ] **Step 7: Commit**

```bash
git add elite_picks.py tests/test_elite_scoring.py
git commit -m "feat: llama 0-100 scoring with data-score fallback in elite picks"
```

---

### Task 3: UI — score-driven rows & dynamic allocation (`templates/breakdown.html`)

**Files:**
- Modify: `templates/breakdown.html` (load `elite_alloc.js`; remove positions selector; rewrite `allocate`/`rowHtml`/`draw`; add reason line; meter = score/100)

**Interfaces:**
- Consumes: `allocateBudget()` from Task 1; per-pick `llm_score`, `llm_reason`, `data_score`, and `DATA.sources.llm` from Task 2.

- [ ] **Step 1: Load the allocation module**

In `templates/breakdown.html`, immediately before the main `<script>` block (the line `<script>` at the top of the JS), add:
```html
<script src="/static/elite_alloc.js"></script>
```

- [ ] **Step 2: Remove the positions selector (HTML)**

Delete this block from the toolbar:
```html
                    <div class="ep-seg" id="ep-pos" role="group" aria-label="How many picks to fund">
                        <button data-n="3">3</button><button data-n="5">5</button><button data-n="10">10</button><button data-n="all">All</button>
                    </div>
```

- [ ] **Step 3: Remove the positions selector (JS state + wiring)**

Delete the `let positions = ...` line, and delete the entire `const posBox = ...` / `syncPosButtons` / `posBox.addEventListener(...)` / `syncPosButtons();` block. Replace the `allocate(picks)` function with a thin adapter over the shared module:
```js
function allocate(picks) {
    return allocateBudget(picks, budget, { BAR: 65, GAMMA: 2, MAX_FUNDED: 10 });
}
```

- [ ] **Step 4: Show the llama reason + score in `rowHtml`**

Replace the `<div class="ep-firms">…</div>` region and the metrics block so each row shows the reason and the funded/below-bar state. Replace `rowHtml` body's return template with:
```js
    const reason = p.llm_reason ? `<div class="ep-reason">“${esc(p.llm_reason)}”</div>` : '';
    const dr = 'https://www.dataroma.com/m/activity.php?sym=' + encodeURIComponent(p.ticker) + '&typ=a';
    const alloc = a.funded
        ? `<div class="ep-alloc">${USD0.format(a.amount)}</div><div class="ep-alloc-u">conviction ${p.llm_score}</div>`
        : `<div class="ep-alloc off">—</div><div class="ep-alloc-u">below bar · ${p.llm_score}</div>`;
    return `<div class="ep-row${a.funded ? '' : ' off'}">
        <div class="ep-rank">${p.rank}</div>
        <div class="ep-id">
            <a class="ep-tk" href="${dr}" target="_blank" rel="noopener">${esc(p.ticker)}</a>
            <div class="ep-nm">${esc(p.name)}</div>
            ${reason}
            <div class="ep-firms">${firms || '—'}</div>
            ${chipHtml ? `<div class="ep-chips">${chipHtml}</div>` : ''}
        </div>
        <div class="ep-metrics">${alloc}
            <div class="ep-elites"><b>${p.buyers}</b> elites${p.heavies ? ` · <b>${p.heavies}</b> heavy` : ''}</div>
        </div>
        <div class="ep-bar" role="img" aria-label="Conviction ${a.barPct.toFixed(0)} of 100"><span style="width:${a.barPct.toFixed(1)}%"></span></div>
    </div>`;
```

- [ ] **Step 5: Add the reason style**

In the `.ep-*` `<style>` block, after the `.ep-firms` rule add:
```css
    .ep-reason { margin-top:6px; font-family:'JetBrains Mono',monospace; font-size:11px; font-style:italic; color:var(--text-2); line-height:1.5; }
```

- [ ] **Step 6: Show scoring mode in the hero panel**

In `draw()`, in the `ep-sources` innerHTML, change the Budget row line to append the scoring mode. Replace:
```js
        `<div class="fx-hm-row"><span class="fx-hm-key">Budget</span><span class="fx-hm-val">${USD0.format(budget)}<span style="color:var(--text-3);font-size:11px"> · top ${fundedN}</span></span></div>`
```
with:
```js
        `<div class="fx-hm-row"><span class="fx-hm-key">Budget</span><span class="fx-hm-val">${USD0.format(budget)}</span></div>`
        + `<div class="fx-hm-row"><span class="fx-hm-key">Scoring</span><span class="fx-hm-val ${sources.llm ? 'up' : ''}"${sources.llm ? '' : ' style="color:var(--text-4)"'}>${sources.llm ? 'LLAMA' : 'DATA-ONLY'}</span></div>`
```
Also delete the now-unused `const fundedN = ...` line just above it.

- [ ] **Step 7: Verify with a headless preview**

Run:
```bash
python3 - <<'PY'
import json
tpl = open('templates/breakdown.html', encoding='utf-8').read()
tpl = tpl.replace('src="/static/elite_alloc.js"', 'src="elite_alloc_preview.js"')
d = json.load(open('elite_picks.json'))
payload = json.dumps({"picks": d["picks"], "sources": d["sources"], "quarter": d.get("quarter","")})
tpl = tpl.replace('function refresh() { load(true); }\nload(false);',
                  'function refresh() { draw(); }\nDATA = %s;\ndraw();' % payload)
open('/tmp/ep_preview.html','w').write(tpl)
import shutil; shutil.copy('static/elite_alloc.js', '/tmp/elite_alloc_preview.js')
PY
google-chrome --headless=new --no-sandbox --disable-gpu --hide-scrollbars \
  --window-size=1180,1500 --force-device-scale-factor=1.5 \
  --screenshot=/tmp/ep_shot.png "file:///tmp/ep_preview.html"
```
Expected: `/tmp/ep_shot.png` shows rows with an italic reason line, a "conviction NN" label, `$` amounts on funded rows, "below bar" on the rest, and no 3/5/10/All selector. Read the screenshot to confirm.

- [ ] **Step 8: Restart + commit**

```bash
pct exec 101 -- systemctl restart ares.service
git add templates/breakdown.html
git commit -m "feat: score-driven rows and dynamic budget allocation UI"
```

---

### Task 4: Deep-research backend (`app.py`)

**Files:**
- Modify: `app.py` (add helpers + route near the elite-picks route ~line 990)
- Test: `tests/test_deep_research.py`

**Interfaces:**
- Produces:
  - `_deep_cache_paths(ticker: str, quarter: str) -> (json_path: str, running_path: str)` (sanitized).
  - `_deep_lookup(ticker: str, quarter: str) -> dict|None` → `{"status":"ready","brief":{…}}` or `{"status":"running"}` or `None`.
  - `_deep_generate(ticker: str, quarter: str, signals: dict) -> None` (runs `claude -p`, writes json, clears marker).
  - Route `GET /api/elite-picks/deep/<ticker>` → JSON `{"status":"ready"|"running"|"error", …}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_deep_research.py`:
```python
import os, sys, json, time, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app

def test_cache_paths_sanitize():
    j, m = app._deep_cache_paths("brk.b", "Q1 2026")
    assert os.path.basename(j) == "BRK.B_Q12026.json", j
    assert m.endswith(".running")
    # path traversal is stripped
    j2, _ = app._deep_cache_paths("../../etc/pw", "Q1 2026")
    assert "/etc/" not in j2 and os.path.basename(j2).endswith(".json")
    print("  paths OK")

def test_lookup_states(tmpdir_monkeypatch=None):
    d = tempfile.mkdtemp()
    app._DEEP_DIR = d
    assert app._deep_lookup("AAA", "Q1") is None                     # nothing yet
    j, m = app._deep_cache_paths("AAA", "Q1")
    open(m, "w").write(str(time.time()))
    assert app._deep_lookup("AAA", "Q1")["status"] == "running"      # fresh marker
    open(m, "w").write("0")                                          # stale marker
    assert app._deep_lookup("AAA", "Q1") is None
    json.dump({"summary": "x", "bull": [], "bear": [], "used_web": False}, open(j, "w"))
    r = app._deep_lookup("AAA", "Q1")
    assert r["status"] == "ready" and r["brief"]["summary"] == "x"   # cache wins
    print("  lookup OK")

if __name__ == "__main__":
    test_cache_paths_sanitize()
    test_lookup_states()
    print("OK")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_deep_research.py`
Expected: FAIL — `AttributeError: module 'app' has no attribute '_deep_cache_paths'`.

- [ ] **Step 3: Add helpers + route to `app.py`**

`app.py` already imports `os`, `re`, `json`, `time`, `subprocess`, `threading`, `request`, `jsonify`, `require_auth`, and defines `_APP_DIR`. Add right after the `elite_picks_api` route:
```python
_DEEP_DIR = os.path.join(_APP_DIR, "ai_data", "deep_research")
_DEEP_TTL = 300            # running-marker staleness (s)
_DEEP_SEM = threading.Semaphore(2)   # cap concurrent claude jobs


def _deep_cache_paths(ticker, quarter):
    assert ticker, "ticker required"
    base = re.sub(r"[^A-Z0-9.]", "", str(ticker).upper())[:6]
    q = re.sub(r"[^0-9A-Za-z]", "", str(quarter or "na"))
    stem = os.path.join(_DEEP_DIR, f"{base}_{q}")
    return stem + ".json", stem + ".running"


def _deep_lookup(ticker, quarter):
    """FS-only state: ready (cached brief), running (fresh marker), or None."""
    js, mk = _deep_cache_paths(ticker, quarter)
    if os.path.exists(js):
        try:
            return {"status": "ready", "brief": json.load(open(js))}
        except Exception:
            pass
    if os.path.exists(mk):
        try:
            if time.time() - float(open(mk).read().strip() or 0) < _DEEP_TTL:
                return {"status": "running"}
        except Exception:
            pass
    return None


def _deep_generate(ticker, quarter, signals):
    """Run `claude -p` for a one-stock brief; write JSON cache; always clear the marker."""
    js, mk = _deep_cache_paths(ticker, quarter)
    prompt = (
        f"Research the stock {ticker} ({signals.get('name','')}). Context (do not just "
        f"repeat it): {json.dumps(signals)}. Write a concise investor brief. Use general "
        f"knowledge and web search; DO NOT invent specific prices, earnings, or dates. "
        f'Return ONLY JSON: {{"summary": "2-3 sentences on the business and why elite '
        f'investors may be accumulating it", "bull": ["...","..."], "bear": ["...","..."]}}'
    )
    with _DEEP_SEM:
        try:
            proc = subprocess.run(
                ["claude", "-p", prompt, "--output-format", "json",
                 "--allowedTools", "WebSearch", "WebFetch"],
                capture_output=True, text=True, timeout=180, cwd="/root")
            data = json.loads(proc.stdout or "{}")
            if data.get("is_error") or not data.get("result"):
                raise RuntimeError("claude cli error")
            brief = json.loads(data["result"])
            brief["used_web"] = bool(
                (data.get("usage") or {}).get("server_tool_use", {}).get("web_search_requests"))
            brief["generated_ts"] = time.time()
            os.makedirs(_DEEP_DIR, exist_ok=True)
            json.dump(brief, open(js, "w"))
        except Exception:
            pass
        finally:
            try:
                os.remove(mk)
            except OSError:
                pass


@app.route("/api/elite-picks/deep/<ticker>")
@require_auth
def elite_deep(ticker):
    if not re.fullmatch(r"[A-Za-z.]{1,6}", ticker):
        return jsonify({"status": "error", "msg": "bad ticker"}), 400
    data = compute_elite_picks(force=False)
    quarter = data.get("quarter", "")
    pick = next((p for p in data.get("picks", []) if p["ticker"].upper() == ticker.upper()), None)
    if not pick:
        return jsonify({"status": "error", "msg": "unknown ticker"}), 404
    if request.args.get("refresh") != "1":
        cur = _deep_lookup(ticker, quarter)
        if cur:
            return jsonify(cur)
    os.makedirs(_DEEP_DIR, exist_ok=True)
    js, mk = _deep_cache_paths(ticker, quarter)
    if request.args.get("refresh") == "1":
        try:
            os.remove(js)
        except OSError:
            pass
    open(mk, "w").write(str(time.time()))
    signals = {k: pick.get(k) for k in ("name", "buyers", "heavies", "momentum", "upside", "llm_reason")}
    threading.Thread(target=_deep_generate, args=(ticker, quarter, signals), daemon=True).start()
    return jsonify({"status": "running"})
```
If `app.py` is missing any of `subprocess`/`threading` at module top, add `import subprocess` / `import threading` with the other stdlib imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_deep_research.py`
Expected: `OK`.

- [ ] **Step 5: Verify the CLI is authenticated in LXC 101**

Run: `pct exec 101 -- bash -lc 'cd /mnt/data/PROJECTS/ARES-DASHBOARD 2>/dev/null; claude -p "reply with the word ok" --output-format json 2>&1 | head -c 300'`
Expected: JSON containing `"result":"ok"` and `"is_error":false`. If it errors with an auth message, STOP and tell the user: the LXC 101 `claude` login expired — they must re-auth (`pct exec 101 -- claude login`) before deep research works. The rest of the feature is unaffected.

- [ ] **Step 6: Commit**

```bash
git add app.py tests/test_deep_research.py
git commit -m "feat: on-demand claude-cli deep research endpoint for elite picks"
```

---

### Task 5: Deep-research UI (`templates/breakdown.html`)

**Files:**
- Modify: `templates/breakdown.html` (add expander button per row, fetch/poll loop, brief renderer, styles)

**Interfaces:**
- Consumes: `GET /api/elite-picks/deep/<ticker>` from Task 4 returning `{status, brief?, msg?}`.

- [ ] **Step 1: Add the expander button to each row**

In `rowHtml`, inside the `ep-id` div after the `ep-chips` line, add a button + container:
```js
            <div class="ep-deep-wrap"><button class="ep-deep-btn" onclick="deepResearch('${esc(p.ticker)}', this)">Deep research ↗</button><div class="ep-deep" id="deep-${esc(p.ticker)}"></div></div>
```

- [ ] **Step 2: Add the poll + render logic**

Before `load(false);` at the bottom of the `<script>`, add:
```js
const deepTimers = {};
async function deepResearch(ticker, btn) {
    const box = document.getElementById('deep-' + ticker);
    if (deepTimers[ticker]) return;                       // already polling
    btn.disabled = true; btn.textContent = 'Researching…';
    box.innerHTML = '<div class="ep-deep-load">querying claude · web search…</div>';
    let tries = 0;                                        // bounded: ~2 min
    const poll = async () => {
        tries++;
        try {
            const r = await fetch('/api/elite-picks/deep/' + encodeURIComponent(ticker));
            const d = await r.json();
            if (d.status === 'ready') { stop(); renderBrief(box, d.brief); btn.textContent = 'Deep research ↗'; btn.disabled = false; return; }
            if (d.status === 'error') { stop(); box.innerHTML = `<div class="ep-deep-err">${esc(d.msg || 'unavailable')}</div>`; btn.textContent = 'Retry'; btn.disabled = false; return; }
        } catch (e) { /* keep polling */ }
        if (tries > 40) { stop(); box.innerHTML = '<div class="ep-deep-err">timed out — try again</div>'; btn.textContent = 'Retry'; btn.disabled = false; }
    };
    const stop = () => { clearInterval(deepTimers[ticker]); delete deepTimers[ticker]; };
    deepTimers[ticker] = setInterval(poll, 3000); poll();
}
function renderBrief(box, b) {
    if (!b) { box.innerHTML = '<div class="ep-deep-err">no data</div>'; return; }
    const li = xs => (xs || []).map(x => `<li>${esc(x)}</li>`).join('');
    box.innerHTML = `<div class="ep-deep-card">
        <p class="ep-deep-sum">${esc(b.summary || '')}</p>
        <div class="ep-deep-cols">
            <div><div class="ep-deep-h up">Bull</div><ul>${li(b.bull)}</ul></div>
            <div><div class="ep-deep-h dn">Bear</div><ul>${li(b.bear)}</ul></div>
        </div>
        <div class="ep-deep-foot">${b.used_web ? 'web-researched' : 'from model knowledge'} · not financial advice</div>
    </div>`;
}
```

- [ ] **Step 3: Add styles**

In the `.ep-*` `<style>` block add:
```css
    .ep-deep-wrap { margin-top:9px; }
    .ep-deep-btn { background:none; border:1px solid var(--line-2); color:var(--text-3); font-family:'JetBrains Mono',monospace; font-size:9px; letter-spacing:.1em; text-transform:uppercase; padding:3px 9px; transition:color .15s,border-color .15s; }
    .ep-deep-btn:hover:not(:disabled) { color:var(--ares); border-color:var(--ares); }
    .ep-deep-btn:disabled { opacity:.6; }
    .ep-deep { }
    .ep-deep-load, .ep-deep-err { margin-top:8px; font-family:'JetBrains Mono',monospace; font-size:10px; color:var(--text-3); }
    .ep-deep-err { color:var(--down); }
    .ep-deep-card { margin-top:10px; border:1px solid var(--line-2); background:var(--bg-2); padding:12px 14px; }
    .ep-deep-sum { font-family:'JetBrains Mono',monospace; font-size:11px; line-height:1.6; color:var(--text-1); }
    .ep-deep-cols { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-top:10px; }
    .ep-deep-h { font-family:'Bricolage Grotesque',sans-serif; font-weight:700; font-size:11px; letter-spacing:.06em; text-transform:uppercase; margin-bottom:4px; }
    .ep-deep-h.up { color:var(--up); } .ep-deep-h.dn { color:var(--down); }
    .ep-deep-cols ul { list-style:none; display:flex; flex-direction:column; gap:5px; }
    .ep-deep-cols li { font-family:'JetBrains Mono',monospace; font-size:10.5px; line-height:1.5; color:var(--text-2); padding-left:11px; position:relative; }
    .ep-deep-cols li::before { content:'·'; position:absolute; left:0; color:var(--text-3); }
    .ep-deep-foot { margin-top:10px; font-family:'JetBrains Mono',monospace; font-size:9px; color:var(--text-4); letter-spacing:.06em; }
    @media (max-width:560px) { .ep-deep-cols { grid-template-columns:1fr; } }
```

- [ ] **Step 4: Verify live in the browser**

Run: `pct exec 101 -- systemctl restart ares.service`
Then in a logged-in browser open `/breakdown`, click "Deep research" on the top pick. Expected: shows "Researching…", then within ~1–2 min renders a summary + bull/bear card with a "web-researched / not financial advice" footer. (If the LXC login was flagged in Task 4 Step 5, this shows the error state instead — expected until re-auth.)

- [ ] **Step 5: Commit**

```bash
git add templates/breakdown.html
git commit -m "feat: on-demand deep research panel with polling in elite picks UI"
```

---

## Self-Review

**1. Spec coverage:**
- Tier 1 batched llama scoring + guardrail prompt → Task 2 (`_llm_scores`, `_LLM_PROMPT`). ✓
- Deterministic fallback / normalized data score → Task 2 (`_attach_llm_scores`, tested). ✓
- Allocation equation (BAR/GAMMA/MAX_FUNDED, reconcile, empty-funded edge, meter=score/100) → Task 1 (module + tests) and Task 3 (wired). ✓
- Deep research via `claude -p`, async disk-backed, cache-per-quarter, `?refresh=1`, concurrency cap, error/degradation → Task 4. ✓
- UI: remove selector, reason line, funded/below-bar, scoring-mode indicator, deep expander + polling → Tasks 3 & 5. ✓
- `llm:false` degradation surfaced in UI → Task 3 Step 6 (Scoring: DATA-ONLY). ✓
- Testing (elite_picks self-check + node allocation assert + deep-state assert) → Tasks 1, 2, 4. ✓
- No `ANTHROPIC_API_KEY` dependency → confirmed, CLI-only. ✓

**2. Placeholder scan:** No TBD/TODO; every code step shows complete code; every test step shows the assertions. ✓

**3. Type consistency:** `allocateBudget(picks, budget, opts)` used identically in Task 1 test, module, and Task 3 adapter. `_llm_scores`/`_attach_llm_scores` signatures match between Task 2 test, definitions, and the `compute_elite_picks` wiring. `_deep_lookup`/`_deep_cache_paths`/`_deep_generate` signatures match between Task 4 test, definitions, and route. Per-pick fields `llm_score`/`llm_reason`/`data_score` produced in Task 2, consumed in Tasks 3 & 5. Route shape `{status, brief, msg}` matches Task 4 producer and Task 5 consumer. ✓

**Note for the executor:** `elite_picks.py` `BAR/GAMMA/MAX_FUNDED` (Task 2) and `static/elite_alloc.js` defaults (Task 1) are intentionally duplicated across the Python/JS boundary. They are only *used* client-side today; the Python copies exist for future server-side use and as documentation. If you change one, change both (a `# keep in sync` comment marks it).
