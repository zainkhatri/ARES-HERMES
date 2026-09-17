# /logs Pages Redesign — Design

Date: 2026-09-14

## Problem

The `/logs` pages (incident detail, job log, index) were built as a narrow
centered column (1040px max-width), small text, status shown only as a tiny
pill. Feedback: doesn't use available width, hard to read, status unclear.

## Design (approved via brainstorming + visual mockup, option A)

**Incident detail page** (`log_view.html`, incident mode):
- Full-width, two-column grid: ~70% main content, ~30% sticky sidebar
  (`position: sticky`, stays visible while the main column scrolls).
- Sidebar (top to bottom): large colored status badge (not a pill — a full
  block with icon-scale color), box (ARES/EROS/ZEUS), timestamp, council
  verdict as its own bordered callout, Approve/Reject buttons near the top
  of the sidebar (only rendered when `show_approve_reject` is true, same
  backend condition as today).
- Main column: diagnosis text, then either the diff (real diff styling —
  gutter background per line, not just colored text) or manual steps, then
  the session log.

**Job log page** (`log_view.html`, job mode): full-width, no sidebar (no
status/approve concept for jobs). Small header strip above the log: last
run / next run / ok-or-not, replacing the current bare title.

**`/logs` index page** (`logs_index.html`): widen from 1040px to fluid
full-width rows (capped only by a generous max, e.g. 1600px, for readability
on ultrawide). Same status-badge treatment as the detail page for visual
consistency.

## Implementation approach

Single shared template (`log_view.html`) already serves both job and
incident pages via a `sections` list built by two different routes. Add a
`sidebar` list (same shape as `sections`) that's empty for job pages and
populated for incident pages. Template renders a two-column CSS grid when
`sidebar` is non-empty, single column otherwise — no route logic duplicated,
just richer data passed from `incident_log_page()`.

## Out of scope

No new backend behavior — this is templates/CSS + the data routes already
pass, reshaped into sidebar vs main sections. Approve/Reject logic,
denylist, council review: unchanged.
