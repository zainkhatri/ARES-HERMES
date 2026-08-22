# ARES security hardening pass — design

Date: 2026-08-22
Status: design (approved)

## Problem

A five-dimension security review of ARES (Flask dashboard in LXC 101, host services, Caddy,
Tailscale exposure) found confirmed Critical/High issues. The worst: the deployed `FLASK_SECRET`
is an unrotated human-readable placeholder that signs both session cookies and vault tokens
(forgeable auth), and two host services run as root with no authentication (host RCE reachable
from the tailnet and from a compromised LXC). This pass fixes Critical + High + the cheap Medium
web-hardening. Out of scope: vault PIN/KDF overhaul (M2), full `vt`→header migration, WebAuthn
key-derivation.

Scope note: **ZEUS/HERMES was not code-audited** — it shares `SSO_SECRET` and receives the
nightly rsync. This pass rotates the shared secret on both boxes and fixes the rsync exclusion,
but a ZEUS-side code audit is a separate effort.

## Deploy surfaces

| Surface | What | Deploy |
|---|---|---|
| LXC 101 app | `app.py` | host bind-mount → `pct exec 101 -- systemctl restart ares` |
| Host services | `system/shell_ctl.py`, `system/pty_ws.py` (root on host) | restart host systemd units |
| Caddy | `/etc/caddy/Caddyfile` (+ repo `Caddyfile.proposed`) | `systemctl reload caddy` |
| ZEUS | `SSO_SECRET` in ZEUS `.env` | rotate + restart ZEUS ares |
| A&N app | `ConsoleView` terminal token (C2) | Mac build → device install |

## Design

### Group A — Secrets & fail-closed config (C1, H1, M4, M5, M7)

- **Rotate secrets.** New 32-byte random `FLASK_SECRET` in ARES `.env`; new `SSO_SECRET` shared
  value in ARES `.env` and ZEUS `.env` (must match). Rotating logs out all sessions and
  invalidates in-flight `?vt=`/SSO tokens — expected.
- **Fail closed.** `app.secret_key = os.environ["FLASK_SECRET"]` (crash if unset); assert it does
  not contain `change-in-prod`/`replace-me`. Boot-assert `ARES_PASSWORD` is set and ≠ `prometheus`
  (`app.py:72`).
- **Constant-time login** (`app.py:406`): `secrets.compare_digest` for password AND username.
- **Login rate limit**: per-IP fail counter, 5 fails → 60s lockout, mirroring
  `_vault_auth_fail_tick` (`app.py:4761`). In-memory dict.
  - `ponytail:` per-worker in-memory dict — a multi-worker gunicorn gives each worker its own
    counter (effective limit = 5 × workers per 60s). Acceptable; upgrade to a shared store only
    if that ceiling matters.
- **Reloader footgun** (`app.py:9746`): `debug = os.getenv("FLASK_DEBUG") == "1"`,
  `use_reloader=debug`. Systemd already sets `FLASK_DEBUG=0` → safe default.
- **Session cookies** (`app.py:30`): `SESSION_COOKIE_SECURE=True, HTTPONLY=True, SAMESITE="Lax"`.
- **`/healthz` CORS** (`app.py:562`): scope ACAO off `*` to the known peer origins.
- Login password itself is **kept** (not auto-randomized — the owner types it). Recommend
  lengthening from 5 chars; the rate-limit + constant-time compare mitigate online brute force.

### Group B — Host-service auth (C2)

`system/shell_ctl.py` (:7683) and `system/pty_ws.py` (:7686), both root:
- **Require `ARES_API_TOKEN`** on every request (shell_ctl HTTP header; pty_ws in the opening WS
  frame), compared with `hmac.compare_digest`. Reject otherwise.
- **Remove `Access-Control-Allow-Origin: *`** (`shell_ctl.py:78`); pty_ws validates the WS
  `Origin` against the dashboard host.
- **Drop root** — run the units as the terminal user, not root.
- **Caddy injects** the bearer on `/ctl` and `/shell` upstreams → the browser terminal is
  unchanged.
- **A&N ConsoleView** (talks to :7683 directly) sends `Authorization: Bearer <ARES_API_TOKEN>`
  (it already holds a bearer via `ARESAuth`) — small client change, one build.
- **Firewall** `192.168.20.0/24 → 100.77.42.110:7683,7686` so a compromised LXC can't reach the
  host services even with the token absent.

### Group C — App traversal & CORS (H3, H4)

- **`_journal_path(name)` helper**: reject null-byte/empty, `realpath`-confine to `JOURNALS_DIR`
  (mirror `system/files_api.py:safe_resolve`). Route the three journal routes through it:
  `/api/journals/highlight` (`app.py:2159`), `/api/journals/ocr/word-boxes` (`2285`),
  `/api/journals/ocr/page-text` (`2251`). Also route `/api/journals/<name>/page/<int>` (`2366`)
  and `/page-dates` (`2050`) through it (defense-in-depth).
- **`serve_media`** (`app.py:5664`): `os.path.realpath` instead of `abspath` (block symlink escape).
- **Minecraft CORS** (`app.py:3218, 3269`): exact-match `origin in {"https://mordor.vercel.app",
  "https://zainkhatri.github.io"}`; drop `.endswith(".vercel.app")` / `startswith("http://localhost")`.

### Group D — Caddy hardening (M6 + part of M1)

- Enforce tailnet-only for the main app by **adding** the existing `not remote_ip 100.64.0.0/10 …`
  gate to the catch-all `handle` block (currently only on `/shell`,`/ctl`,`/pty-ws`). Chosen over
  rebinding Caddy off `0.0.0.0` because it keeps the `:8443` TLS listener intact and reuses the
  proven gate pattern; the `/api/minecraft` funnel path must stay exempt from this gate.
- Global `header` block on both site blocks: `Strict-Transport-Security "max-age=31536000"`,
  `X-Frame-Options "DENY"`, `X-Content-Type-Options "nosniff"`, a `Content-Security-Policy`,
  `Referrer-Policy "no-referrer"`.
- Scrub `vt` and `t` query params from the access-log format.
- Mirror changes into repo `Caddyfile.proposed`.

### Group E — vt token quick-hardening (M1, no client change)

- Shorten the `?vt=` TTL from 3600s to 300s (`_vault_token_key`, `app.py:3821`).
- `Referrer-Policy: no-referrer` already covered by Group D globally (covers vault pages).
- **Full `vt`→header migration deferred** — it would re-touch the just-shipped A&N offline code;
  D+E close the log/referer leak without breaking iOS.

### Group F — H2 plaintext vault rsync (split)

- Add `.vault/` to the ARES→ZEUS rsync exclude so only `vault_enc/` ciphertext replicates; verify
  the exclusion in the backup script/unit.
- Code check: confirm every vault entry has ciphertext coverage in `vault_enc/`.
- **Owner deletes** the plaintext `.vault/` originals (Claude does not open/scan My-Eyes-Only
  media). Claude only fixes the rsync config + coverage check.

### Also (cheap): M3 VNC password

- Move `WINDOWS_VNC_PASSWORD` from the inline systemd `Environment=` into the `.env`
  EnvironmentFile. Full client-side prompt / server-side VNC proxy deferred.

## Components changed

- `app.py` — Groups A, C, E (secret loading, boot asserts, login compare+ratelimit, reloader,
  cookies, healthz CORS, `_journal_path`, serve_media realpath, minecraft CORS, vt TTL).
- `system/shell_ctl.py`, `system/pty_ws.py` — Group B (token auth, origin, no CORS `*`).
- `/etc/caddy/Caddyfile` + `Caddyfile.proposed` — Groups B (bearer inject), D (bind, headers, log
  scrub).
- ARES `.env`, ZEUS `.env` — rotated secrets, VNC password relocation.
- systemd units (host) — drop root on shell_ctl/pty_ws; firewall rule.
- A&N `ConsoleView` — send bearer to shell_ctl.

## Error handling / safety

- All secret/config asserts fail **closed** (app refuses to boot misconfigured).
- Secret rotation is coordinated ARES+ZEUS in one step to avoid SSO breakage window.
- Host-service token gate must not lock out the browser terminal (Caddy injects) or A&N (client
  update) — verify both before considering B done.
- `.vault` deletion stays with the owner; Claude never reads vault media.

## Testing / verification

After deploy:
1. Login works with a fresh session; wrong password is rate-limited after 5 tries.
2. App refuses to boot if `FLASK_SECRET`/`ARES_PASSWORD` unset (quick negative test in a scratch env).
3. Browser terminal (`/shell`,`/ctl`) works; A&N ConsoleView works; direct `:7683`/`:7686` without
   token is rejected; both services run as non-root (`ps`).
4. Photos + vault load; `/api/vault/*` still served (cookie path).
5. `/api/journals/ocr/word-boxes?pdf=../../etc/hosts` → 400/empty (not file contents).
6. `/api/minecraft` succeeds from `mordor.vercel.app`, rejected from `foo.vercel.app`.
7. Security headers present (`curl -I`); `?vt=`/`?t=` absent from Caddy access log.
8. ZEUS SSO jump (`/jump/hermes`) still works after shared-secret rotation.
9. `.vault/` excluded from the next rsync (dry-run).

## Non-goals / YAGNI

- Vault PIN/KDF overhaul (Argon2id, force-change, escalating lockout) — separate pass (M2).
- Full `vt`→header migration; full VNC client-side auth; WebAuthn PRF key derivation.
- ZEUS-side code audit.
- Shared-store login rate limiter (in-memory per-worker is enough for a single-user box).
