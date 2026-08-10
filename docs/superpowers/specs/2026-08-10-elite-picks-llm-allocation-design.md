# Elite Picks — LLM-weighted scoring & dynamic budget allocation

**Date:** 2026-08-10
**Status:** Design approved, pending spec review
**Touches:** `elite_picks.py`, `app.py`, `templates/breakdown.html`, new `ai_data/deep_research/`

## Problem

The `/breakdown` "Elite Picks" page ranks stocks by a fixed weighted-conviction
formula and splits a budget linearly across a manually-chosen top-N. The user
wants the weighing to be *dynamic and calculated* — use the local LLM (llama)
to judge each stock, let the number of funded names fall out of the scores, and
offer an on-demand deep-research pass for names worth a closer look.

## Decisions (from brainstorming)

1. **"Areas" = individual stocks** — llama weighs each pick and decides which to buy.
2. **llama output = a 0–100 rating + one-line reason per stock** (Approach A). A
   deterministic equation turns ratings into dollars (LLMs are unreliable at arithmetic).
3. **Hybrid guardrailed basis** — the score is driven by our scraped signals; llama
   may use general company knowledge for the *reason* and tie-breaking, but must not
   invent prices, earnings, dates, or news.
4. **Tiered research** — cheap llama scoring runs weekly (automatic); an expensive
   Claude deep-research ("silver surfer") pass runs **on-demand per stock**, cached.
5. **Score-gated dynamic funding** — user enters a total budget (default **$1,000**);
   only names clearing a conviction bar get funded; count is emergent, not a knob.
6. **Deep research uses the Claude Code CLI (`claude -p`), not the Anthropic API** —
   no `ANTHROPIC_API_KEY` needed; uses the existing logged-in CLI in LXC 101.

## Architecture — two independent tiers

Expensive work never blocks cheap work.

### Tier 1 — Weekly scoring (automatic, cheap, `elite_picks.py`)
Runs in the existing `ares-elite-picks.timer` (Mon 06:00 PT) via `compute_elite_picks`:
```
1. _dataroma_buys()        candidates + signals (unchanged)
2. _momentum / _analyst    best-effort enrich (unchanged)
3. _llm_scores(candidates) NEW: one batched llama call (VM300 Ollama)
                           → {ticker: {score 0-100, reason}}
4. attach llm_score + llm_reason to each pick
5. rank by llm_score (fallback: normalized data score if llama unavailable)
6. write elite_picks.json (adds llm_score, llm_reason, llm:bool)
```

### Tier 2 — Deep research "silver surfer" (on-demand, `app.py`)
```
Click "Deep research" on a pick
 → GET /api/elite-picks/deep/<ticker>
 → background thread runs: claude -p "<prompt>" --output-format json
                           --allowedTools WebSearch WebFetch
 → writes ai_data/deep_research/<TICKER>_<quarter>.json  (+ .running marker)
 → endpoint returns {status: ready|running|error}; page polls until ready
```

**Allocation stays client-side** so budget edits are instant (no server round-trip).

## Tier 1 detail — the llama scoring contract

`_llm_scores(candidates)` in `elite_picks.py` (standalone; calls Ollama HTTP directly):

- **Transport:** `POST {OLLAMA_HOST}/api/generate`, `model=llama3.1:8b`,
  `stream=false`, `format="json"`, `options={temperature:0.2, seed:42}`,
  `timeout=90`. Low temp + fixed seed → stable week-to-week on identical data.
- **Input:** one batched prompt with a JSON list; each candidate carries only
  signals we actually have:
  `{ticker, name, elite_buyers, heavy_hitters:[names], momentum_3mo|null, analyst_upside|null}`.
- **Prompt (guardrailed):** score 0–100 by how strongly elites are accumulating
  (more buyers + more heavyweights = higher; positive momentum/upside nudge up);
  general company knowledge allowed for the reason/tie-breaks only; no invented
  prices/earnings/dates/news; reason ≤ 15 words; return JSON only
  `{"scores":[{"ticker","score","reason"}]}`.
- **Validation:** clamp score to int 0–100; require non-empty reason; any ticker
  llama drops or garbles → falls back to its normalized data score. Whole-call
  failure → all picks use data score, result flagged `llm:false`.

**Deterministic fallback / llama-down path:**
`data_weight` = existing weighted conviction (3×heavy + 1×regular + congress·2 + upside·0.2 + momentum·0.1).
`norm = round(100 × data_weight / max_data_weight)` → guarantees a valid 0–100 score
even with Ollama offline (covers the GPU-on-loan case too).

## Tier 1 detail — the allocation equation (client-side JS)

```
Constants:  BAR = 65        # conviction bar to get funded
            GAMMA = 2        # concentration exponent (higher = more top-heavy)
            MAX_FUNDED = 10  # breadth cap

funded = picks where llm_score >= BAR, top MAX_FUNDED by score
if funded is empty:  funded = [highest-scored pick]     # never leave the user empty
weight_i = llm_score_i ^ GAMMA
amount_i = round(BUDGET * weight_i / sum(weight over funded))
reconcile: add the rounding remainder to the top pick so sum(amount) == BUDGET exactly
unfunded picks: amount 0, shown "below bar"
row meter width = llm_score / 100          # absolute conviction, not relative
```

**Worked example** ($1,000, BAR 65, GAMMA 2): scores AMZN 92, V 88, MSFT 79,
SLM 70, NKE 61 → NKE below bar (unfunded). Weights 8464/7744/6241/4900 (Σ 27349)
→ AMZN $310 · V $283 · MSFT $228 · SLM $179 (Σ $1,000).

`BAR`, `GAMMA`, `MAX_FUNDED` are named constants, tunable later.

## Tier 2 detail — deep research via Claude Code CLI

- **Runner:** `subprocess` list form (no shell) in a background thread:
  `["claude","-p", prompt, "--output-format","json","--allowedTools","WebSearch","WebFetch"]`,
  cwd `/root` (LXC 101, runs as root where the CLI auth lives), `timeout=180`.
  Parse stdout JSON → `.result` text.
- **Prompt:** built server-side from the pick's cached signals + llama score/reason;
  ticker regex-validated `^[A-Z.]{1,6}$` and passed as a single argv. Asks for a
  tight brief: what the company does · why elites may be accumulating · 2 bull ·
  2 bear · ends "not financial advice." No fabricated figures.
- **Async, disk-backed state** (works across gunicorn workers):
  - result: `ai_data/deep_research/<TICKER>_<quarter>.json`
    `{summary, bull:[..], bear:[..], used_web:bool, generated_ts, model}`
  - in-progress: `<TICKER>_<quarter>.running` (timestamped; stale >5 min ⇒ retryable)
  - endpoint returns `{status:"ready", brief}` | `{status:"running"}` | `{status:"error", msg}`
  - `?refresh=1` forces a re-run; failures are not cached.
- **Concurrency:** the `.running` marker dedupes duplicate jobs per ticker; cap total
  concurrent jobs at 2.
- **Quarter suffix** auto-invalidates the cache when a new 13F quarter lands.

## Error handling / degradation

| Failure | Behavior |
|---|---|
| Ollama down/slow/timeout | Batch falls back to normalized data score; `llm:false`; UI note "scores: data-only (llama offline)". Page works. |
| llama drops/garbles one ticker | That ticker uses its data score; others keep llama scores. |
| Dataroma down | Serve stale `elite_picks.json` (existing behavior). |
| claude CLI errors / LXC login expired | Deep-research returns `{status:"error", msg:"claude CLI unavailable — re-auth in LXC 101"}`. Weekly scoring unaffected. |
| claude job exceeds 180s | Killed; marker cleared; status error. |
| GPU on loan (VM300) | Covered by the Ollama-down fallback. |

All external calls have bounded timeouts; cron `TimeoutStartSec=600`.

## UI changes (`templates/breakdown.html`, existing house style)

- **Remove** the 3/5/10/All selector (funding is now dynamic via the bar). Keep the
  budget input (default $1,000, persisted in `localStorage`).
- Each row: llama **conviction score** drives the meter (`score/100`) and shows as a
  number; llama **reason** is a muted italic line under the company name; the
  heavy-hitter roster stays (grounding). Funded rows show the **$ amount** (headline
  right metric); below-bar rows show "below bar" muted.
- Hero: top pick + its llama reason; sources panel gains **"Scoring: llama / data-only"**
  alongside Budget.
- Each row gets a **"Deep research"** expander → polls the endpoint, shows
  loading/error states, renders the brief with a `used_web` badge; collapsed by default.

## Testing

- `elite_picks.py` `__main__` self-check (extends existing): asserts every pick has
  `llm_score` ∈ [0,100]; the llama-down path produces valid data-score fallbacks;
  gating respects `BAR`.
- One runnable assert for the allocation equation (pure JS function, run under `node`):
  funded amounts sum exactly to budget; concentration monotonic in score; empty-funded
  edge funds the top pick.

## Out of scope (YAGNI)

- Sector/theme grouping (user chose per-stock).
- llama choosing the total budget (personal risk decision stays with the user).
- Whole-share / live-price sizing (price feed blocked from this host; revisit if a
  working source/key appears).
- Position caps / max-weight guardrails (GAMMA + BAR suffice for now; add later if needed).
