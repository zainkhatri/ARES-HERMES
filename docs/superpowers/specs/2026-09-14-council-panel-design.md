# Council Panel — Incident Page Redesign

Date: 2026-09-14

## Problem

Council review is currently one LLM call returning a single merged verdict
string. The incident page shows that one sentence and a wall of technical
text. Zain wants to skim a fix like a PR: simple title, plain-English
explanation, each council member's individual vote and take (with person
icons), a summary of what will happen with pros/cons, and a Merge button.

## Design

### Council becomes a 4-member panel

`council.py` gains `panel_review(context: str) -> dict`. Four personas,
each an independent headless `claude -p` call (run sequentially — cheap
enough at 4, and subprocess parallelism buys little here):

| Persona | Icon | Lens |
|---|---|---|
| Security | 🛡️ | Does this leak/expose anything, touch secrets, widen attack surface? |
| Correctness | 🔍 | Does the fix actually solve the root cause? Is it right? |
| Blast-Radius | 💥 | Worst case if this is wrong? What else could it break? |
| Pragmatist | ⚙️ | Is this worth doing? Will it actually work in practice? |

Each returns `{approve: bool, verdict: str}` (one plain-English sentence,
written for a non-expert). Panel result:

```json
{
  "votes": [{"persona": "Security", "icon": "🛡️", "approve": true, "verdict": "..."}, ...],
  "approved": true,          // >= 3 of 4 approve
  "summary": "..."           // one extra cheap call synthesizes: what happens
                             // if merged, pros list, cons list -- see below
}
```

The synthesis call returns `{what_happens: str, pros: [str], cons: [str]}`
in simple terms. Fail-closed everywhere: any persona call error counts as
a rejection vote with verdict "review failed"; synthesis failure yields an
empty summary (page still renders votes).

### Storage

`diagnosis.council_votes` (list as above), `diagnosis.council_summary`
(`{what_happens, pros, cons}`). The old `council_verdict` string stays
populated (joined one-liner) for backward compat with the /logs index page.

### finalize.py

Both `_council_review` (diff path) and `_council_review_recommendation`
(commands/manual path) are replaced by one `council.panel_review(context)`
call with path-appropriate context text. Decision rule: `approved` (3/4)
maps to the same statuses as before (council_approved / recommendation_ready
/ council_held). The mechanical denylist gates still run BEFORE the panel,
unchanged.

### Incident page layout (top to bottom)

1. Status dot + status word (unchanged)
2. **Title** — fix_title promoted to the page title position (big, plain);
   the unit-name incident title moves to a small subtitle line
3. **What this fix does** — 1-2 sentence plain explanation (first paragraph
   of reasoning, or fix_title if reasoning missing)
4. **Why** — remaining reasoning paragraphs (existing prose treatment)
5. **Council votes** — responsive 4-card grid: icon, persona name, big
   ✓ APPROVE / ✗ REJECT, one-sentence take
6. **If you merge this** — summary card: what_happens text, then two-column
   pros (green) / cons (red) lists
7. **Merge button** — replaces "Apply this fix"; same POST
   /api/autofix/approve endpoint, restyled as a merge action. Reject stays.
8. Diff / manual steps (existing treatment)
9. Collapsed session log (existing treatment)

Old incidents without `council_votes` render the legacy single-verdict card
so existing data doesn't break.

## Out of scope

- No change to the auto-execute/sign-off gating from the previous spec.
- No change to the /logs index or job pages.
- Existing two resolved incidents keep their single-verdict display.
