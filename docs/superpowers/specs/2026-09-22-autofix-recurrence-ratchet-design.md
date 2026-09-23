# Autofix recurrence ratchet — design

Date: 2026-09-22
Branch: `feature/autofix-recurrence-ratchet`

## Problem

The autofix pipeline (`ops/autofix/`) detects incidents, triages them, and either
skips, escalates, or fixes them. Three real gaps were verified against the live
incident store (`ops/autofix/incidents.json`, 44 incidents):

1. **Genuine issues rot in hidden `triaged_skip` limbo.** The ZEUS backup failure
   (`zeus-horcrux` scheduled job) was detected 5 separate times and skipped all 5.
   Routing ignores recurrence count. The dashboard hides `triaged_skip`
   (`app.py:747`), so a real, recurring, data-loss issue is invisible.

2. **A resolved fix that regresses is treated as brand-new.** `watcher.run_once`
   dedups only against *pending* statuses (`watcher.py:163`). When a `resolved`
   fix's signature recurs — the exact "the fix did not hold" signal (e.g. an
   exec-bit lost again) — the system discards that signal and starts over.

3. **Dashboard shows stale merge-ready items.** An incident fixed out-of-band (a
   human commit) still shows as an actionable `council_approved` "Merge Ready"
   card, because nothing rechecks whether the underlying failure is still present.

Plus one filesystem artifact: a stray `ops/autofix/~/.claude` directory from a
botched command with an unexpanded `~`.

## Non-goals

- No changes to council logic or the approval/sign-off gates.
- No external side effects — no alert files, no push notifications.
- No auto-refixing of regressions. A regressed fix is deliberately handed to a
  human, because the identical fix already failed.
- No per-fix custom health probers.

## Design

### 1. Recurrence context (store layer — `incident_store.py`)

Add `IncidentStore.recurrence_context(signature)` returning:

```python
{"skip_count": int, "last_status": str | None}
```

- `skip_count`: count of prior incidents with this signature whose status is
  `triaged_skip`.
- `last_status`: status of the most-recent prior incident with this signature
  (`None` if none).

Computed on demand from the existing incidents list — no schema change. The scan
is bounded by a module constant `MAX_INCIDENT_SCAN` (Power-of-Ten rule 2: fixed
upper bound). Two assertions: `signature` is a non-empty str; result counts are
`>= 0`.

### 2. New status `recurring_needs_human`

- Add to `VALID_STATUSES` in `incident_store.py`.
- Exclude it from the dashboard hide-filter (`app.py:747` currently drops
  `rejected` and `triaged_skip`). It is NOT dropped, so it always lands in the
  "Needs You" bucket (`app.py:756`).
- The incident detail carries diagnosis notes used by the template:
  - `regressed_from`: prior incident id (regression case), or
  - `skipped_n_times`: the skip count that tripped the ratchet.

### 3. Ratchet (in `watcher.run_once` / `_triage_and_route`)

Evaluated for each source right after `existing = find_by_signature(signature)`
and `ctx = store.recurrence_context(signature)`:

- **Regression rule.** If `ctx["last_status"] == "resolved"` and the failure is
  present now → create the incident and set status **directly** to
  `recurring_needs_human` with `regressed_from=<prior id>`. Do NOT run triage or
  spend a diagnosis session — the identical fix already failed.
- **Repeat-skip rule.** Otherwise route through triage as today. If triage
  decides skip **and** `ctx["skip_count"] >= SKIP_RATCHET_N` → set
  `recurring_needs_human` (`skipped_n_times=<count>`) instead of `triaged_skip`.

`SKIP_RATCHET_N = 3` (module constant in `watcher.py`).

`_triage_and_route` gains a `skip_count` parameter so both of its
`triaged_skip` exits can promote when over threshold.

### 4. Durability = passive via the ratchet

No separate active post-apply prober. The next watcher poll after a resolve is
the durability check: if the signature recurs, the Regression rule fires. This
catches regressions whether they happen in 3 minutes or 3 days, with no per-fix
custom health logic.

### 5. Auto-close-when-healthy + cleanup

- In `run_once`, after collecting the current failing signatures for the
  *pollable* sources (systemd units, dashboard jobs, alert files): any incident
  in `council_approved` or `recurring_needs_human` for one of those sources whose
  signature is NOT in the current-failing set → set `resolved`
  (`cleared_out_of_band=True`). Never applied to remote recommendation-only
  findings (`recommendation_ready`), which have no pollable local signal.
- Delete the stray `ops/autofix/~/` directory (one-time housekeeping, done in the
  implementation commit, not in code).

### 6. `apply.py` note

`apply.py` applies file fixes through git (`git add` + commit), which preserves
file mode. There is no mode-stripping bug in the write path; the historical
exec-bit churn is covered generally by the durability ratchet, not a targeted
patch.

## Testing (TDD)

New tests under `tests/`:

- `test_autofix_recurrence.py`
  - regression: prior `resolved` + failure present → `recurring_needs_human`,
    `regressed_from` set, triage NOT called.
  - repeat-skip: `skip_count >= 3` + triage=skip → `recurring_needs_human`.
  - below threshold: `skip_count < 3` + triage=skip → stays `triaged_skip`.
- `test_autofix_auto_close.py`
  - `council_approved` incident whose signature is no longer failing → `resolved`
    with `cleared_out_of_band`.
  - `recommendation_ready` incident is never auto-closed.

Existing autofix tests (`test_autofix_watcher`, `test_autofix_dedup`,
`test_autofix_triage`, `test_autofix_incident_store`, ...) must stay green.

## Files touched

- `ops/autofix/incident_store.py` — `recurrence_context`, new status.
- `ops/autofix/watcher.py` — ratchet rules, auto-close, `SKIP_RATCHET_N`.
- `app.py` — dashboard filter/bucket for the new status.
- templates — recurrence badge (the autofix incidents view).
- `tests/test_autofix_recurrence.py`, `tests/test_autofix_auto_close.py` — new.
- Remove `ops/autofix/~/`.
