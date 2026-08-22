# ARES Security Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the confirmed Critical + High findings and cheap Medium web-hardening from the 2026-08-22 ARES security audit.

**Architecture:** Changes span the LXC-101 Flask app (`app.py`), two host root services (`system/shell_ctl.py`, `system/pty_ws.py`), Caddy config, ARES+ZEUS `.env` secrets, and one A&N iOS client change. Fixes are fail-closed. The host-service auth (C2) is sequenced so clients send the token BEFORE the services require it, to avoid locking out the terminal.

**Tech Stack:** Python 3 / Flask (gunicorn in LXC 101), stdlib `http.server`/websockets host services, Caddy, systemd, Swift (A&N app), rsync.

## Global Constraints

- Never add Co-Authored-By trailers; commit subject line only, no body (per CLAUDE.md).
- Zero-warnings; simple control flow; validate parameters; fail closed on misconfig.
- Claude never opens/scans My-Eyes-Only vault media (`.vault/` deletion is the owner's).
- Secret rotation must be applied to ARES and ZEUS together (shared `SSO_SECRET`).
- App code deploys via host bind-mount → `pct exec 101 -- systemctl restart ares`.
- Host services (`shell_ctl`, `pty_ws`) and Caddy run on the host, not the LXC.
- The `/api/minecraft` funnel path must remain reachable (exempt from the tailnet-only gate).
- No new dependencies; use stdlib (`secrets`, `hmac`, `os.path.realpath`).

---

### Task 1: Rotate secrets + fail-closed secret loading + relocate VNC password

**Files:**
- Modify: ARES `.env` (host path `/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/.env`)
- Modify: ZEUS `.env` (`ssh hermes`, its repo `.env`)
- Modify: `app.py:31`
- Modify: LXC-101 systemd unit for `ares` (remove inline `WINDOWS_VNC_PASSWORD`, rely on EnvironmentFile)

**Interfaces:**
- Produces: `app.secret_key` sourced strictly from env; `FLASK_SECRET`, `SSO_SECRET` are strong random and identical across ARES/ZEUS.

- [ ] **Step 1: Generate two new secrets**

Run: `python3 -c "import secrets;print('FLASK_SECRET='+secrets.token_hex(32));print('SSO_SECRET='+secrets.token_hex(32))"`
Copy both values.

- [ ] **Step 2: Set them in ARES `.env`**

Replace the `FLASK_SECRET=...` and `SSO_SECRET=...` lines in `/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/.env` with the new values. Also confirm `ARES_API_TOKEN` is set (non-empty) — Task 7 requires it; if empty, generate one: `python3 -c "import secrets;print('ARES_API_TOKEN='+secrets.token_hex(24))"` and add it.

- [ ] **Step 3: Set the SAME `SSO_SECRET` on ZEUS**

Run: `ssh hermes "grep -n SSO_SECRET /srv/mergerfs/PROMETHEUS/PROJECTS/*/.env"` to locate ZEUS's dashboard `.env`, then set `SSO_SECRET=` to the identical value from Step 1. (ZEUS keeps its own `FLASK_SECRET`.)

- [ ] **Step 4: Make secret loading fail-closed in app.py**

Replace `app.py:31`:
```python
app.secret_key = os.getenv("FLASK_SECRET", secrets.token_hex(32))
```
with:
```python
_flask_secret = os.environ.get("FLASK_SECRET", "")
assert _flask_secret and "change-in-prod" not in _flask_secret and "replace-me" not in _flask_secret, \
    "FLASK_SECRET must be set to a real random value (see .env)"
app.secret_key = _flask_secret
```

- [ ] **Step 5: Relocate the VNC password out of the inline systemd Environment=**

In the LXC-101 `ares` unit, delete any `Environment=WINDOWS_VNC_PASSWORD=...` line and ensure `WINDOWS_VNC_PASSWORD=` exists in the EnvironmentFile `.env` instead. Run: `pct exec 101 -- systemctl cat ares | grep -i vnc` to find it; edit the unit under `/etc/systemd/system/` in the LXC, then `pct exec 101 -- systemctl daemon-reload`.

- [ ] **Step 6: Restart both apps and verify boot + SSO**

Run: `pct exec 101 -- systemctl restart ares && sleep 3 && pct exec 101 -- systemctl is-active ares`
Expected: `active`. Then `ssh hermes "sudo systemctl restart <zeus-ares-unit>"` and confirm active.
Negative test: `pct exec 101 -- env -u FLASK_SECRET python3 -c "import app"` → Expected: AssertionError about FLASK_SECRET.

- [ ] **Step 7: Commit**
```bash
git add app.py
git commit -m "fix: load FLASK_SECRET fail-closed; reject placeholder secret"
```
(`.env` and systemd units are not in the repo — not committed.)

---

### Task 2: Login hardening — constant-time compare, boot-assert password, rate limit, reloader, cookies, healthz CORS

**Files:**
- Modify: `app.py:72` (password constant), `app.py:400-412` (login), `app.py:9746` (reloader), `app.py:31-33` area (cookie config), `app.py:562` (healthz CORS)

**Interfaces:**
- Consumes: `secrets`, `hmac` (already imported or stdlib).
- Produces: `_login_rate_ok(ip) -> bool` and `_login_rate_fail(ip)` helpers.

- [ ] **Step 1: Boot-assert ARES_PASSWORD is set and not the default**

Replace `app.py:72`:
```python
LOGIN_PASSWORD = os.getenv("ARES_PASSWORD", "prometheus")
```
with:
```python
LOGIN_PASSWORD = os.environ.get("ARES_PASSWORD", "")
assert LOGIN_PASSWORD and LOGIN_PASSWORD != "prometheus", \
    "ARES_PASSWORD must be set to a non-default value (see .env)"
```

- [ ] **Step 2: Add session-cookie hardening + a login rate limiter (near app.config, after app.py:33)**

Insert after `app.jinja_env.auto_reload = True` (app.py:33):
```python
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,   # Caddy terminates TLS on :8443
)

# ponytail: per-worker in-memory login throttle. Single-user box; effective limit
# is 5*workers/60s. Upgrade to a shared store only if that ceiling matters.
import time as _time
_login_fails = {}   # ip -> (count, first_ts)
def _login_rate_ok(ip):
    rec = _login_fails.get(ip)
    if not rec:
        return True
    count, first = rec
    if _time.time() - first > 60:
        _login_fails.pop(ip, None)
        return True
    return count < 5
def _login_rate_fail(ip):
    count, first = _login_fails.get(ip, (0, _time.time()))
    if _time.time() - first > 60:
        count, first = 0, _time.time()
    _login_fails[ip] = (count + 1, first)
```

- [ ] **Step 3: Make login constant-time and rate-limited**

Replace the body of `login()` (`app.py:402-412`):
```python
    data = request.json or {}
    username = data.get("username", "")
    password = data.get("password", "")
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()

    if not _login_rate_ok(ip):
        return jsonify({"success": False, "error": "Too many attempts — wait 60s."}), 429

    ok_pw = secrets.compare_digest(password, LOGIN_PASSWORD)
    ok_user = secrets.compare_digest(username, LOGIN_USER) if username else True
    if ok_pw and ok_user:
        session["authenticated"] = True
        session["username"] = "zain"
        session.permanent = False
        return jsonify({"success": True, "username": "zain"})
    _login_rate_fail(ip)
    return jsonify({"success": False, "error": "Authentication failed."}), 401
```
(Username stays optional — the web login form may not send it; only enforced if present.)

- [ ] **Step 4: Tie the reloader to FLASK_DEBUG**

Replace `app.py:9746` `debug=use_debug, use_reloader=True,` with:
```python
        debug=use_debug, use_reloader=use_debug,
```
(Systemd runs with `--no-debug` → `use_debug=False` → reloader off. Confirms the Werkzeug console can't be reached in prod.)

- [ ] **Step 5: Scope /healthz CORS off `*`**

At `app.py:562` (the `/healthz` ACAO), replace `"Access-Control-Allow-Origin": "*"` with the exact peer origins the dashboard uses, e.g.:
```python
        "Access-Control-Allow-Origin": "https://ares.tail3045df.ts.net",
```
If ZEUS also reads it cross-origin, use a small allowlist echo like the minecraft pattern in Task 4. Read the surrounding handler first to match its response-building style.

- [ ] **Step 6: Deploy + verify**

Run: `pct exec 101 -- systemctl restart ares && sleep 3`
Verify login: `curl -s -X POST http://192.168.20.213:8080/api/login -H 'Content-Type: application/json' -d '{"password":"<real>"}'` → `{"success":true,...}` with a `Set-Cookie` containing `HttpOnly; Secure`.
Verify rate limit: 6 rapid wrong-password POSTs → the 6th returns HTTP 429.

- [ ] **Step 7: Commit**
```bash
git add app.py
git commit -m "fix: constant-time login, rate limit, boot-assert password, cookie flags, reloader gate, scope healthz CORS"
```

---

### Task 3: Journal path-traversal confinement + serve_media symlink hardening

**Files:**
- Modify: `app.py` — add `_journal_path()` helper near the journals routes; use it at `app.py:2050, 2159, 2251, 2285, 2366`; change `serve_media` at `app.py:5664`.
- Test: `test_journal_path.py` (standalone, repo root)

**Interfaces:**
- Produces: `_journal_path(name: str) -> str | None` — returns a realpath confined to `JOURNALS_DIR`, or `None` if the name escapes / is invalid.

- [ ] **Step 1: Write the standalone failing test**

Create `test_journal_path.py`:
```python
import os, tempfile
# Minimal copy of the helper under test (kept in sync with app.py:_journal_path).
def _journal_path(name, JOURNALS_DIR):
    if not name or "\x00" in name:
        return None
    ap = os.path.realpath(os.path.join(JOURNALS_DIR, name))
    root = os.path.realpath(JOURNALS_DIR)
    if ap != root and not ap.startswith(root + os.sep):
        return None
    return ap

def test_confinement():
    d = tempfile.mkdtemp()
    open(os.path.join(d, "ok.pdf"), "w").close()
    assert _journal_path("ok.pdf", d) == os.path.realpath(os.path.join(d, "ok.pdf"))
    assert _journal_path("../../../../etc/passwd", d) is None
    assert _journal_path("../" + os.path.basename(d), d) is None
    assert _journal_path("", d) is None
    assert _journal_path("a\x00b", d) is None
    print("PASS")

if __name__ == "__main__":
    test_confinement()
```

- [ ] **Step 2: Run it — expect PASS (validates the logic before wiring in)**

Run: `python3 test_journal_path.py`
Expected: `PASS` (this proves the confinement logic; Step 4 wires the identical logic into app.py).

- [ ] **Step 3: Add the helper to app.py (immediately above the first journals route, near app.py:2050)**
```python
def _journal_path(name):
    """Confine a request-supplied journal filename to JOURNALS_DIR (realpath). None if it escapes."""
    if not name or "\x00" in name:
        return None
    ap = os.path.realpath(os.path.join(JOURNALS_DIR, name))
    root = os.path.realpath(JOURNALS_DIR)
    if ap != root and not ap.startswith(root + os.sep):
        return None
    return ap
```

- [ ] **Step 4: Replace each unsafe join**

At `app.py:2159` (`/api/journals/highlight`), `2285` (`ocr/word-boxes`), `2251` (`ocr/page-text`), `2366` (`<name>/page/<int>`), `2050` (`page-dates`): replace `path = os.path.join(JOURNALS_DIR, <name_var>)` with:
```python
    path = _journal_path(<name_var>)
    if not path:
        return jsonify({"error": "invalid path"}), 400
```
Read each route first to use its actual variable name (`pdf`, `pdf_name`, `name`) and its existing not-found response shape.

- [ ] **Step 5: Harden serve_media symlink escape**

At `app.py:5664`, change `os.path.abspath(` to `os.path.realpath(` in the confinement check so symlinks inside PHOTOS can't escape the prefix.

- [ ] **Step 6: Deploy + verify traversal is blocked**

Run: `pct exec 101 -- systemctl restart ares && sleep 3`
`curl -s -b <authed-cookie> "http://192.168.20.213:8080/api/journals/ocr/word-boxes?pdf=../../../../etc/hosts&page=0"` → Expected: `{"error":"invalid path"}` HTTP 400, NOT file contents.

- [ ] **Step 7: Commit**
```bash
git add app.py test_journal_path.py
git commit -m "fix: confine journal pdf paths to JOURNALS_DIR; realpath in serve_media"
```

---

### Task 4: Minecraft CORS exact-match + shorten vault token TTL

**Files:**
- Modify: `app.py:3219, 3266` (minecraft CORS), `app.py:3828` (vt TTL)

- [ ] **Step 1: Exact-match minecraft origins (both routes)**

At `app.py:3219` and `app.py:3266`, replace:
```python
    is_allowed = any(origin.startswith(o) for o in allowed_origins) or origin.endswith(".vercel.app")
```
with:
```python
    is_allowed = origin in {"https://mordor.vercel.app", "https://zainkhatri.github.io"}
```
Also drop `"http://localhost"` and `"http://127.0.0.1"` from the `allowed_origins` list at 3213/3260 (they were only used by the fuzzy `startswith`); keep `allowed_origins[0]` as the safe default `cors_origin`.

- [ ] **Step 2: Shorten the vt token max_age**

At `app.py:3828`, change `max_age=3600` to `max_age=300`.

- [ ] **Step 3: Deploy + verify**

Run: `pct exec 101 -- systemctl restart ares && sleep 3`
`curl -s -X POST http://192.168.20.213:8080/api/minecraft -H 'Origin: https://foo.vercel.app' -H 'Content-Type: application/json' -d '{"action":"status"}' -D- | grep -i access-control-allow-origin` → Expected: header value is `https://mordor.vercel.app` (the default), NOT `https://foo.vercel.app`.

- [ ] **Step 4: Commit**
```bash
git add app.py
git commit -m "fix: exact-match minecraft CORS origins; shorten vault token TTL to 300s"
```

---

### Task 5: Caddy injects the bearer on /ctl and /shell (prep for C2)

**Files:**
- Modify: `/etc/caddy/Caddyfile` (host) + repo `Caddyfile.proposed`

**Interfaces:**
- Produces: upstream requests to `:7683`/`:7681`(ttyd)/`:7686` carry `Authorization: Bearer <ARES_API_TOKEN>`. Must land BEFORE Task 7 enforces it.

- [ ] **Step 1: Add header_up to the /ctl and /shell and /pty-ws reverse_proxy blocks**

Read `/etc/caddy/Caddyfile`. In each `reverse_proxy` for `/ctl*` (→127.0.0.1:7683 or the host IP), `/shell*` (ttyd), and `/pty-ws*` (7686), add inside the proxy block:
```
        header_up Authorization "Bearer <ARES_API_TOKEN value>"
```
Use the actual token value from `.env`. (Caddy has no direct env-in-config here unless started with the env; simplest is the literal value in the root-owned Caddyfile.)

- [ ] **Step 2: Validate + reload Caddy**

Run: `caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy`
Expected: `Valid configuration` then clean reload.

- [ ] **Step 3: Verify the browser terminal still works**

Load the dashboard terminal in a browser (tailnet) — it should still drive tmux (services don't require the token yet, so this only confirms no regression).

- [ ] **Step 4: Mirror into repo + commit**

Copy the same edits into repo `Caddyfile.proposed`.
```bash
git add Caddyfile.proposed
git commit -m "chore: Caddy injects bearer on /ctl,/shell,/pty-ws upstreams"
```

---

### Task 6: A&N ConsoleView sends the bearer to shell_ctl (prep for C2)

**Files:**
- Modify: A&N `ARES/ConsoleView.swift` (repo `more projects/A&N`)

**Interfaces:**
- Consumes: `ARESAuth.shared` bearer/token, or the `ARES_API_TOKEN`.
- Produces: every request ConsoleView makes to `:7683` carries `Authorization: Bearer <token>`. Must ship BEFORE Task 7.

- [ ] **Step 1: Find where ConsoleView builds its shell_ctl requests**

Run: `grep -n "7683\|/ctl\|/text\|/capture\|URLRequest" "more projects/A&N/ARES/ConsoleView.swift"`
Identify the shared request-building spot (or each call site).

- [ ] **Step 2: Add the Authorization header**

At the request construction, add:
```swift
req.setValue("Bearer \(ARESAuth.shared.token)", forHTTPHeaderField: "Authorization")
```
Confirm `ARESAuth.shared` exposes the API token; if it only holds the cookie, add the `ARES_API_TOKEN` value the same way the app stores other secrets (match existing pattern). Read `ARESAuth` first.

- [ ] **Step 3: Build + deploy to phone**

Follow the build recipe (rsync → xcodegen → simulator compile-check → signed device build → install/launch). Verify the in-app terminal still works (service doesn't require token yet).

- [ ] **Step 4: Commit (A&N repo)**
```bash
cd "more projects/A&N" && git add ARES/ConsoleView.swift && git commit -m "feat: ConsoleView sends bearer to shell control service"
```

---

### Task 7: Enforce auth on host services + drop root + firewall (C2)

**Files:**
- Modify: `system/shell_ctl.py` (auth check, remove CORS `*`), `system/pty_ws.py` (token in first frame, Origin check)
- Modify: host systemd units for both services (run as non-root `User=`)
- Add: host firewall rule blocking `192.168.20.0/24 → :7683,:7686`

**Interfaces:**
- Consumes: `ARES_API_TOKEN` from the service environment (must be present in both units' env).

- [ ] **Step 1: Add a token gate to shell_ctl.py**

Near the top of `system/shell_ctl.py` add:
```python
import hmac
API_TOKEN = os.environ.get("ARES_API_TOKEN", "")
assert API_TOKEN, "ARES_API_TOKEN must be set for shell_ctl"
def _authed(headers):
    got = (headers.get("Authorization", "") or "").removeprefix("Bearer ").strip()
    return bool(got) and hmac.compare_digest(got, API_TOKEN)
```
In `do_POST` (after `app.py`-style entry, ~line 108) and `do_GET` (line 100), add as the first lines:
```python
        if not _authed(self.headers):
            self._json({"error": "unauthorized"}, 401); return
```
Change `_cors` (line 77-80) to NOT emit `*`:
```python
    def _cors(self):
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
```
(No `Access-Control-Allow-Origin` → browsers can't read cross-origin; same-origin via Caddy and native app with explicit header still work.)

- [ ] **Step 2: Add a token gate + Origin check to pty_ws.py**

Read `system/pty_ws.py` around the connection handler (~line 669-709). Require the client to send `{"token": "<ARES_API_TOKEN>"}` (or an `Authorization` header on the WS upgrade) in/with the first frame; compare with `hmac.compare_digest`; close the socket otherwise. Validate the WS `Origin` header (if present) against the dashboard host `ares.tail3045df.ts.net`; reject mismatches. Add `API_TOKEN = os.environ.get("ARES_API_TOKEN","")` with an assert like Step 1.

- [ ] **Step 3: Drop root on both host units**

Edit the host systemd units for `shell_ctl` and `pty_ws`: add `User=<terminal-user>` (the tmux/session owner), ensure `EnvironmentFile` provides `ARES_API_TOKEN`, then `systemctl daemon-reload`. Confirm the tmux socket is accessible to that user (the `web` session must be owned by it).

- [ ] **Step 4: Restart services and verify enforcement**

Run: `systemctl restart ares-shell-ctl pty_ws` (use the real unit names).
Verify unauthorized is rejected: `curl -s -o /dev/null -w '%{http_code}' -X POST http://100.77.42.110:7683/capture` → Expected: `401`.
Verify authorized works: same curl with `-H "Authorization: Bearer <token>"` → `200`.
Verify non-root: `ps -o user= -C python3 | sort -u` shows the services no longer as root (check the specific PIDs).
Verify browser terminal + A&N terminal still work (Tasks 5+6 supply the token).

- [ ] **Step 5: Firewall the LXC→host service ports**

Add an iptables/nft rule on the host dropping `192.168.20.0/24 → 100.77.42.110` dports `7683,7686` (leave the tailnet `100.64.0.0/10` allowed). Example (adapt to the host's firewall):
```bash
iptables -I INPUT -s 192.168.20.0/24 -p tcp -m multiport --dports 7683,7686 -j DROP
```
Persist it (netfilter-persistent / the host's firewall config).

- [ ] **Step 6: Commit**
```bash
git add system/shell_ctl.py system/pty_ws.py
git commit -m "fix: require ARES_API_TOKEN on host shell/pty services; drop CORS wildcard; validate WS origin"
```
(systemd units + firewall are host state, not in the repo.)

---

### Task 8: Caddy hardening — tailnet-only main app, security headers, log scrubbing

**Files:**
- Modify: `/etc/caddy/Caddyfile` + repo `Caddyfile.proposed`

- [ ] **Step 1: Add the tailnet gate to the catch-all app handler**

In the catch-all `handle` block that proxies to the Flask app, add the same guard already used for `/shell`,`/ctl` (e.g. `@nottailnet not remote_ip 100.64.0.0/10` → `respond @nottailnet 403`). Ensure the `/api/minecraft*` route is matched and proxied ABOVE/outside this gate so the funnel path stays reachable.

- [ ] **Step 2: Add a global security-header block to both site blocks**
```
        header {
            Strict-Transport-Security "max-age=31536000"
            X-Frame-Options "DENY"
            X-Content-Type-Options "nosniff"
            Referrer-Policy "no-referrer"
            Content-Security-Policy "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self' wss:"
        }
```
(Start CSP permissive-enough for the existing inline styles/scripts + xterm websocket; tighten later. Verify the dashboard + terminal still render after reload.)

- [ ] **Step 3: Scrub vt/t query params from the access log**

If the site uses `log`, add a `format` filter or route logging so `?vt=` and `?t=` are not recorded. If Caddy's log filtering is awkward, at minimum set the log to omit the query string for `/api/vault/*` and `/api/sso/*`. Document the chosen approach inline in the Caddyfile.

- [ ] **Step 4: Validate, reload, verify**

Run: `caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy`
Verify headers: `curl -sI https://ares.tail3045df.ts.net/ | grep -iE "strict-transport|x-frame|x-content|referrer"` → all present.
Verify dashboard + terminal still load in a browser; verify `/api/minecraft` still reachable.

- [ ] **Step 5: Mirror + commit**
```bash
git add Caddyfile.proposed
git commit -m "chore: Caddy tailnet-only app gate, security headers, vt/t log scrubbing"
```

---

### Task 9: Stop replicating plaintext vault + verify ciphertext coverage (H2, Claude's half)

**Files:**
- Modify: the ARES→ZEUS rsync script/unit (the backup source)
- Add: a one-shot coverage check (scratch script, not committed unless useful)

- [ ] **Step 1: Locate the rsync backup definition**

Run: `grep -rn "rsync" /etc/systemd/system/*ares* /etc/systemd/system/*backup* /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD 2>/dev/null | grep -i zeus` and check `system/` for a backup script. Identify the command that syncs `PHOTOS/` to ZEUS.

- [ ] **Step 2: Add the exclude**

Add `--exclude '.vault/'` (and confirm `vault_enc/` ciphertext is still included) to that rsync command. If it's a systemd unit, edit + `daemon-reload`.

- [ ] **Step 3: Dry-run verify the exclusion**

Run the rsync command with `--dry-run --itemize-changes` and confirm no `.vault/` paths appear in the output.

- [ ] **Step 4: Ciphertext-coverage check (informational for the owner)**

Write a scratch script that, for each entry in the vault state JSON, asserts a matching `vault_enc/<key>/orig.enc` exists; report any entry lacking ciphertext. Output the count. (This tells the owner it's safe to delete `.vault/`.) Do NOT open/decrypt any media.

- [ ] **Step 5: Report to owner**

Print the coverage result and remind the owner: "`.vault/` plaintext can be deleted; every entry has ciphertext" (or list the exceptions). The owner performs the deletion.

- [ ] **Step 6: Commit (if the backup script lives in the repo)**
```bash
git add <backup-script-if-tracked>
git commit -m "fix: exclude plaintext .vault from ZEUS rsync"
```

---

### Task 10: Full deploy + verification battery

**Files:** none (verification only)

- [ ] **Step 1: Restart everything**

`pct exec 101 -- systemctl restart ares`; `systemctl reload caddy`; `systemctl restart ares-shell-ctl pty_ws`; ZEUS ares restart. All `active`.

- [ ] **Step 2: Run the spec's 9-point verification checklist**

Walk the checklist in `docs/superpowers/specs/2026-08-22-ares-security-hardening-design.md` (login+ratelimit, fail-closed boot, browser+A&N terminal, direct :7683/:7686 rejected + non-root, photos+vault load, journal traversal 400, minecraft origin filtering, security headers + log scrub, ZEUS SSO jump, rsync dry-run). Record PASS/FAIL for each with the command output.

- [ ] **Step 3: Update memory**

Add/refresh a memory noting the hardening is applied and which items remain deferred (M2 vault KDF, full vt→header, ZEUS code audit), linking the spec.

- [ ] **Step 4: Final commit / branch wrap-up**

Ensure all app/system/Caddyfile.proposed changes are committed on `security/hardening-2026-08-22`. Report the full verification results to the owner and offer to open a PR or merge.
```

## Notes for the executor

- Tasks 5 and 6 MUST land before Task 7 (clients send the token before services require it), or the terminal locks out.
- Task 1's secret rotation logs out all active sessions and invalidates in-flight `?vt=`/SSO tokens — expected; do it when a brief logout is acceptable.
- Host-side changes (systemd units, firewall, Caddyfile, `.env`) are not in the repo; only `app.py`, `system/*.py`, `Caddyfile.proposed`, `test_journal_path.py`, and A&N Swift are committed.
- The A&N build recipe and device/keychain IDs are in the `promethon-app-server-pin` memory.
