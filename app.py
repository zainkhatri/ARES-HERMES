"""Flask server for ARES NAS Terminal AI Interface."""

import json
import os
import re
import secrets
import functools
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, session, redirect, url_for, send_file, abort, make_response, send_from_directory
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

load_dotenv()

from system.system_info import get_system_info
from ai import llm_interface
from ai import claude_interface
from ai.llm_interface import get_usage_stats
from system.recycling_bin import trash_file, list_trash, restore as restore_trash, TRASH_DIR as _TRASH_DIR
from photos import trash_review as _trash_review
from system import files_api
from system import files_index

app = Flask(__name__, static_folder=None)   # built-in /static served private photo tiers with NO auth — replaced by static_files() below
_flask_secret = os.environ.get("FLASK_SECRET", "")
assert _flask_secret and "change-in-prod" not in _flask_secret and "replace-me" not in _flask_secret, \
    "FLASK_SECRET must be set to a real random value (see .env)"
app.secret_key = _flask_secret
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # NOT Secure: the A&N iOS app authenticates over http://<tailscale-ip>:8080 (plain HTTP,
    # but WireGuard-encrypted by Tailscale) and relies on this cookie — Secure would drop it.
    # LAN plaintext exposure is closed by the Caddy tailnet-only gate (see hardening spec).
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

_GALLERY_TZ = ZoneInfo("America/Los_Angeles")


@app.after_request
def _gzip_json(resp):
    """Compress JSON/HTML responses. The photos endpoints ship 70-160KB of
    JSON per request, all of it text — gzip drops that to <15% of the
    original wire size. Skip already-encoded responses, streamed responses
    (where Content-Length is unknown), and binary payloads (thumbs, video)
    where compression is wasted CPU."""
    accept = request.headers.get("Accept-Encoding", "")
    if "gzip" not in accept.lower():
        return resp
    if resp.direct_passthrough or resp.is_streamed:
        return resp
    if resp.headers.get("Content-Encoding"):
        return resp
    ctype = (resp.content_type or "").split(";", 1)[0].strip().lower()
    compressible = (
        ctype in ("application/json", "text/html", "text/css",
                  "application/javascript", "text/javascript",
                  "image/svg+xml", "text/plain")
    )
    if not compressible:
        return resp
    body = resp.get_data()
    if len(body) < 512:
        return resp
    import gzip as _gzip
    compressed = _gzip.compress(body, compresslevel=6)
    resp.set_data(compressed)
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Content-Length"] = str(len(compressed))
    vary = resp.headers.get("Vary")
    resp.headers["Vary"] = "Accept-Encoding" if not vary else (vary + ", Accept-Encoding")
    return resp

LOGIN_PASSWORD = os.environ.get("ARES_PASSWORD", "")
assert LOGIN_PASSWORD and LOGIN_PASSWORD != "prometheus", \
    "ARES_PASSWORD must be set to a non-default value (see .env)"
LOGIN_USER = os.getenv("ARES_USER", "zainkhatri")
API_TOKEN = os.getenv("ARES_API_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024 * 1024  # 8 GB (journal PDFs stream to disk)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 86400 * 7

# Store conversation history per session (simple in-memory store)
conversations = {}

# Display-only history per session (role + text, no binary data)
session_display = {}

# ─── Thumbnail cache (RAM + lazy disk generation) ───
# Thumbnails are generated on first request from the original photo and cached
# to disk. Subsequent requests are served from RAM. No pre-generation needed.

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
# Flask static_folder is None (Caddy serves /static with auth), so code that needs
# the on-disk static root must use this, NOT app.static_folder (which is None → crashes).
_STATIC_DIR = os.path.join(_APP_DIR, "static")


@app.context_processor
def _asset_versions():
    """Cache-bust shared static assets by mtime. hud.css is served with a 7-day
    max-age, so without a version query CSS edits do not reach clients until the
    cache expires (this is why unstyled headers appeared after a hud.css change)."""
    def _mt(name):
        try:
            return int(os.path.getmtime(os.path.join(_STATIC_DIR, name)))
        except OSError:
            return 0
    return {"hud_v": _mt("hud.css"), "zeusgraph_v": _mt("zeus-graph.js")}

# ─── GPU loan flag ───
# Written by the host's gpu-swap.sh hookscript before VM 200/300 borrows the
# RTX 3080, removed when the VM stops (the hook restarts this service on both
# edges). Hiding CUDA *before* torch / ffmpeg ever initialise means this
# process runs CPU-only and never opens /dev/nvidia*, so the host can unbind
# the nvidia driver without racing a systemd-respawned GPU consumer.
# CLIP search and video transcode degrade to their CPU paths automatically.
GPU_LOAN_FLAG = os.path.join(_APP_DIR, ".gpu-on-loan")
# Manual, human-set "force CPU" override. Distinct from the VM-loan flag so the
# boot/timer reconciler (gpu-loan-reconcile) never deletes an intentional
# override -- it only ever manages .gpu-on-loan. app honors both.
GPU_FORCE_CPU_FLAG = os.path.join(_APP_DIR, ".gpu-force-cpu")
if os.path.exists(GPU_LOAN_FLAG) or os.path.exists(GPU_FORCE_CPU_FLAG):
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    print("[gpu] loan/force-cpu flag present — CPU-only mode (RTX 3080 not in use by ARES)")


def _gpu_on_loan():
    """True while the RTX 3080 is unavailable to ARES -- lent to a VM (gaming),
    or held CPU-only by a manual .gpu-force-cpu override. The borrowing VM gets
    strict priority: DEFER all CPU video transcode/HLS work until the GPU
    returns -- CPU x264 encodes on the host cores starve the VM's KVM emulator/
    IO threads and cause input stutter. Live file check (not the startup-time
    snapshot) so work auto-resumes the moment the flag clears, no restart
    needed. The Proxmox hookscript writes/removes .gpu-on-loan around VM
    start/stop; the reconciler clears a stale one."""
    return os.path.exists(GPU_LOAN_FLAG) or os.path.exists(GPU_FORCE_CPU_FLAG)

_THUMB_DIR = os.path.join(_APP_DIR, "static", "thumbs")
_THUMB_HQ_DIR = os.path.join(_APP_DIR, "static", "thumbs_hq")
_THUMB_PREVIEW_DIR = os.path.join(_APP_DIR, "static", "thumbs_preview")
# 2560px WebP @ q85 — peak-quality lightbox tier. Typically 200-500 KB vs
# the 2-5 MB JPEG original, decodes pixel-for-pixel on retina displays.
# Files at static/thumbs_max/<hash>.webp are served directly by Caddy
# (the @thumbs path matcher in the Caddyfile globs `thumbs*`).
_THUMB_MAX_DIR = os.path.join(_APP_DIR, "static", "thumbs_max")
SESSIONS_DIR = os.path.join(_APP_DIR, ".sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(_THUMB_DIR, exist_ok=True)
os.makedirs(_THUMB_HQ_DIR, exist_ok=True)
os.makedirs(_THUMB_PREVIEW_DIR, exist_ok=True)
os.makedirs(_THUMB_MAX_DIR, exist_ok=True)
_thumb_cache = {}
_thumb_cache_lock = threading.Lock()

_landscape_thumbs = set()   # thumb URLs known to be landscape (w > h)
_landscape_index_ready = False

# hash -> original file path (built from index, used for on-demand generation)
_hash_to_path = {}
_hash_index_lock = threading.Lock()


def _build_hash_index(items):
    """Rebuild hash->path reverse map from photo index."""
    mapping = {}
    for item in items:
        orig = item.get("path", "")
        if not orig:
            continue
        for url_key in ("thumb", "thumb_hq"):
            url = item.get(url_key, "")
            if url:
                name = url.rsplit("/", 1)[-1]
                mapping[name] = orig
    with _hash_index_lock:
        _hash_to_path.clear()
        _hash_to_path.update(mapping)


def _jpeg_dimensions(data):
    """Parse width/height from JPEG bytes by reading SOF marker. Returns (w, h) or None."""
    import struct
    if not data or len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return None
    pos = 2
    while pos < len(data) - 1:
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker == 0xC0 or marker == 0xC2:
            if pos + 9 < len(data):
                h = struct.unpack(">H", data[pos+5:pos+7])[0]
                w = struct.unpack(">H", data[pos+7:pos+9])[0]
                return (w, h)
            break
        if marker == 0xD9:
            break
        if pos + 3 < len(data):
            length = struct.unpack(">H", data[pos+2:pos+4])[0]
            pos += 2 + length
        else:
            break
    return None


def _build_landscape_index():
    """Scan all cached thumbnails and record which are landscape orientation."""
    global _landscape_index_ready
    count = 0
    with _thumb_cache_lock:
        keys = list(_thumb_cache.keys())
    for key in keys:
        data = _thumb_cache.get(key)
        if not data:
            continue
        dims = _jpeg_dimensions(data)
        if dims and dims[0] > dims[1]:
            name = key[1]  # key is (directory, filename)
            _landscape_thumbs.add(name)
            count += 1
    _landscape_index_ready = True
    print(f"[warmer] Landscape index built: {count} landscape out of {len(keys)} thumbs.")


def _read_thumb(directory, name):
    """Return thumbnail bytes from RAM cache, reading from disk on first access."""
    key = (directory, name)
    data = _thumb_cache.get(key)
    if data is not None:
        return data
    path = os.path.join(directory, name)
    try:
        with open(path, "rb") as f:
            data = f.read()
        with _thumb_cache_lock:
            _thumb_cache[key] = data
        return data
    except OSError:
        return None


def _preload_thumb_batch(items):
    """No-op: thumbnails are now lazy. Kept for API compatibility."""
    pass


@app.before_request
def _serve_thumb_on_demand():
    """Intercept thumbnail requests. Serve from RAM, disk, or generate on demand."""
    path = request.path
    if path.startswith("/static/thumbs_preview/"):
        directory = _THUMB_PREVIEW_DIR
        tier = "preview"
    elif path.startswith("/static/thumbs_hq/"):
        directory = _THUMB_HQ_DIR
        tier = "hq"
    elif path.startswith("/static/thumbs/"):
        directory = _THUMB_DIR
        tier = "thumb"
    else:
        return None

    if not session.get("authenticated") and not check_bearer_token():
        abort(401)

    name = secure_filename(path.rsplit("/", 1)[-1])

    # 1. RAM cache hit (skip RAM cache for large previews to save memory)
    if tier != "preview":
        data = _read_thumb(directory, name)
        if data is not None:
            return Response(data, mimetype="image/jpeg", headers={
                "Cache-Control": "public, max-age=604800, immutable",
            })

    # 2. Disk hit for preview tier
    if tier == "preview":
        disk_path = os.path.join(directory, name)
        if os.path.exists(disk_path):
            return send_file(disk_path, mimetype="image/jpeg",
                             max_age=604800, conditional=True)

    # 3. Generate from original photo — SKIP for videos (ffmpeg blocks request).
    # Videos get their thumbs via the background backfill; missing = 404,
    # frontend shows a dark placeholder with play icon.
    orig_path = _hash_to_path.get(name)
    if orig_path and os.path.isfile(orig_path):
        ext = os.path.splitext(orig_path)[1].lower()
        is_video = ext in VIDEO_EXTS
        if is_video:
            abort(404)
        if tier == "preview":
            size, quality = 2048, 1
        elif tier == "hq":
            size, quality = 800, 2
        else:
            size, quality = 475, 3
        out_path = os.path.join(directory, name)
        if gen_thumb(orig_path, out_path, size, quality, is_video):
            if tier == "preview":
                return send_file(out_path, mimetype="image/jpeg",
                                 max_age=604800, conditional=True)
            data = _read_thumb(directory, name)

    if tier == "preview":
        abort(404)

    if data is None:
        abort(404)

    return Response(data, mimetype="image/jpeg", headers={
        "Cache-Control": "public, max-age=604800, immutable",
    })


# ─── Session persistence ───

def _strip_images_from_history(history):
    """Replace base64 image blocks with a text placeholder to keep sessions small."""
    clean = []
    for msg in history:
        content = msg.get("content")
        if isinstance(content, list):
            new_content = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image":
                    new_content.append({"type": "text", "text": "[image]"})
                else:
                    new_content.append(block)
            clean.append({**msg, "content": new_content})
        else:
            clean.append(msg)
    return clean


def _save_session(session_id, history, model, display):
    """Write session to .sessions/{id}.json."""
    title = "[session]"
    for item in display:
        if item["role"] == "user" and item["text"] != "[image]":
            title = item["text"][:60]
            break
    data = {
        "id": session_id,
        "title": title,
        "updated_at": time.time(),
        "updated_at_str": datetime.now().strftime("%b %d %H:%M"),
        "model": model,
        "history": _strip_images_from_history(history),
        "display": display,
    }
    path = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def _load_session(session_id):
    """Load session JSON from disk. Returns dict or None."""
    path = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _list_sessions(limit=20):
    """Return list of session summaries sorted by updated_at desc."""
    sessions = []
    try:
        filenames = os.listdir(SESSIONS_DIR)
    except OSError:
        return []
    for fname in filenames:
        if not fname.endswith(".json"):
            continue
        path = os.path.join(SESSIONS_DIR, fname)
        try:
            with open(path) as f:
                data = json.load(f)
            sessions.append({
                "id": data["id"],
                "title": data.get("title", "[untitled]"),
                "updated_at": data.get("updated_at", 0),
                "updated_at_str": data.get("updated_at_str", ""),
                "model": data.get("model", "claude"),
                "message_count": len(data.get("display", [])),
            })
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    sessions.sort(key=lambda s: s["updated_at"], reverse=True)
    return sessions[:limit]


def check_bearer_token():
    """Check if request has a valid bearer token."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer ") and API_TOKEN:
        return secrets.compare_digest(auth_header[7:], API_TOKEN)
    return False


def require_auth(f):
    """Decorator to require session login or bearer token."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if session.get("authenticated") or check_bearer_token():
            return f(*args, **kwargs)
        if request.is_json or request.path.startswith("/api/"):
            return jsonify({"error": "Not authenticated"}), 401
        return redirect(url_for("login_page"))
    return decorated


# Flask's built-in /static route served the private photo tiers (thumbs/faces/hls/…) with NO
# auth, bypassing the Caddy forward_auth gate — any tailnet peer or the LXC could pull the whole
# gallery incl. 2048px previews and face crops. static_folder=None disables it; this replacement
# GATES the private photo dirs behind session/bearer while keeping the app-shell (js/css/vendor/
# favicons/manifest) public so the login page still renders pre-auth. (On the web path Caddy
# already serves thumbs directly behind forward_auth — this closes the direct-to-Flask hole.)
_STATIC_DIR = os.path.join(_APP_DIR, "static")
_PRIVATE_STATIC = ("thumbs", "thumbs_hq", "thumbs_max", "thumbs_preview",
                   "faces", "face_avatars", "hls", "video_cache")

@app.route("/static/<path:filename>")
def static_files(filename):
    top = filename.split("/", 1)[0]
    if top in _PRIVATE_STATIC and not (session.get("authenticated") or check_bearer_token()):
        return ("", 401)
    return send_from_directory(_STATIC_DIR, filename)   # send_from_directory blocks ../ traversal


@app.route("/login", methods=["GET"])
def login_page():
    if session.get("authenticated"):
        return redirect(url_for("home"))
    is_zeus = os.getenv("HOST_BRAND", "").upper() == "ZEUS"
    return render_template(
        "login.html",
        brand_name="ZEUS" if is_zeus else "ARES",
        brand_tag="Mid-NAS" if is_zeus else "Super-NAS",
        sister_name="ARES" if is_zeus else "ZEUS",
        sister_href="http://100.77.42.110:8080/" if is_zeus else "/jump/zeus",
    )


@app.route("/api/login", methods=["POST"])
def login():
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


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True})


@app.route("/_authz")
def _authz():
    """Auth gate for Caddy forward_auth on static media tiers it serves from
    disk. 204 = allow, 401 = deny. No body — the only cost is the cookie check,
    so Caddy can serve the bytes itself without streaming them through Python.
    Mirrors the auth in _serve_thumb_on_demand for thumbs/thumbs_hq."""
    if session.get("authenticated") or check_bearer_token():
        return ("", 204)
    return ("", 401)


# ─── Cross-node SSO handoff ─────────────────────────────────────────────────
# ARES and ZEUS are twin apps with a shared SSO_SECRET. Clicking the other
# node's tab issues a short-lived signed token, which the target node validates
# and uses to bootstrap its own session. No second login prompt.
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from urllib.parse import urlparse

SSO_SECRET = os.getenv("SSO_SECRET", "").strip()
SSO_MAX_AGE = 60  # seconds — the handoff token is one-shot, tight window

# Hostnames we're willing to redirect to after consuming a token. Anything else
# gets rejected — prevents open-redirect abuse of the SSO endpoint.
SSO_ALLOWED_HOSTS = {
    "ares.tail3045df.ts.net",
    "pve.tail3045df.ts.net",
    "zeus.tail3045df.ts.net",
    "cronos.tail3045df.ts.net",
    "192.168.20.213",
    "ares.local",
    "100.100.29.36",
    "prometheon.tail3045df.ts.net",
}


def _sso_serializer():
    if not SSO_SECRET:
        return None
    return URLSafeTimedSerializer(SSO_SECRET, salt="cross-node-sso")


@app.route("/api/sso/issue")
@require_auth
def sso_issue():
    """Issue a signed handoff URL for the other node."""
    target = request.args.get("for", "").strip()
    if not target:
        return jsonify({"error": "missing `for` parameter"}), 400
    parsed = urlparse(target)
    if parsed.hostname not in SSO_ALLOWED_HOSTS:
        return jsonify({"error": "target host not allowed"}), 400
    s = _sso_serializer()
    if s is None:
        # No shared secret configured — fall back to a plain link.
        return jsonify({"redirect": target})
    token = s.dumps({"u": session.get("username", "zain")})
    base = target.rstrip("/")
    return jsonify({"redirect": f"{base}/api/sso/consume?t={token}"})


@app.route("/api/sso/consume")
def sso_consume():
    """Validate an incoming handoff token and start a local session."""
    token = request.args.get("t", "")
    s = _sso_serializer()
    if not token or s is None:
        return redirect(url_for("login_page"))
    try:
        data = s.loads(token, max_age=SSO_MAX_AGE)
    except SignatureExpired:
        return redirect(url_for("login_page") + "?err=expired")
    except BadSignature:
        return redirect(url_for("login_page") + "?err=badsig")
    session["authenticated"] = True
    session["username"] = data.get("u", "zain")
    session.permanent = False
    return redirect(url_for("home"))


@app.route("/jump/zeus")
@require_auth
def jump_zeus():
    """Hand off to ZEUS with a one-shot SSO token, skipping its login.
    Symmetric with ZEUS→ARES: both jumps use the tailnet IP (private, no funnel,
    no 'hermes' name). Route path stays /jump/zeus (internal); target is the ZEUS box IP."""
    target_base = "http://100.100.29.36:8890"
    s = _sso_serializer()
    if s is None:
        return redirect(target_base)
    token = s.dumps({"u": session.get("username", "zain")})
    return redirect(f"{target_base}/api/sso/consume?t={token}")


@app.route("/girlfriend")
def girlfriend_day():
    """Public — National Girlfriend Day page for Fiza."""
    return send_from_directory("websites/friends/girlfriend", "index.html")


@app.route("/")
@require_auth
def home():
    # Inline the first system-info snapshot so the page paints with real vitals
    # instead of skeletons that pop in after the client-side fetch. Cached (~35ms).
    return render_template("home.html", boot=get_system_info())


# EROS/ZEUS box dashboards retired 2026-09-11 — one ARES dashboard now; EROS data
# lives in the ARES knowledge graph + the Array-map storage rows. Old URLs redirect home.
@app.route("/zeus")
@app.route("/eros")
@require_auth
def _retired_box_view():
    return redirect(url_for("home"))


@app.route("/healthz")
def healthz():
    """Unauthenticated liveness + drift probe (no secrets). Reports brand, the
    capability profile, and the deploy stamp (git SHA written by
    deploy/sync-to-cronos.sh) so a deploy can assert the remote box is running
    the code it just pushed — turning silent drift into a loud check."""
    from system.system_info import _capabilities, get_system_info
    stamp = "dev"
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".deploy_stamp")) as f:
            stamp = f.read().strip() or "dev"
    except OSError:
        pass
    # Light, non-secret vitals summary (counts only, no names) for the peer's
    # sister-node card. Cached under the hood.
    summary = None
    try:
        info = get_system_info()
        conts = info.get("containers", []) or []
        crons = info.get("crons", []) or []
        summary = {
            "cpu": info.get("cpu_percent"),
            "mem": info.get("memory_percent"),
            "containers_total": len(conts),
            "containers_down": sum(1 for c in conts if not c.get("ok")),
            "jobs_failed": sum(1 for j in crons if not j.get("ok") and not j.get("running")),
        }
    except Exception:
        pass
    resp = jsonify({"ok": True, "brand": os.getenv("HOST_BRAND", ""),
                    "caps": _capabilities(), "stamp": stamp, "summary": summary})
    # Non-secret, tailnet-only. Allow cross-origin reads ONLY from tailnet origins so the
    # sister-node card on the OTHER box can read it from the browser — not from arbitrary sites.
    _o = request.headers.get("Origin", "")
    if _o.endswith(".ts.net"):
        resp.headers["Access-Control-Allow-Origin"] = _o
        resp.headers["Vary"] = "Origin"
    return resp


_CRON_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".host_cron_logs")
# Allowlist, not a sanitize-and-hope: mirrors ops/cron-status.py's JOBS units
# exactly, plus zeus-horcrux (which has no local systemd unit/log on ARES).
_CRON_LOG_UNITS = {"ares-facescan", "ares-elite-picks", "journal-pull", "ares-autofix-watcher", "ares-autofix-audit", "zeus-horcrux"}


@app.route("/api/cron-log/<unit>")
@require_auth
def cron_log(unit):
    if unit not in _CRON_LOG_UNITS:
        return jsonify({"error": "unknown job"}), 404
    if unit == "zeus-horcrux":
        return jsonify({"error": "this job runs on ZEUS, not ARES -- no local log to show"}), 404
    path = os.path.join(_CRON_LOG_DIR, f"{unit}.log")
    try:
        with open(path, errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return jsonify({"error": "no log yet for this job"}), 404
    return jsonify({"log": "".join(lines[-400:])})


_CRON_LOG_LABELS = {"ares-facescan": "Facial Scan", "ares-elite-picks": "Elite's Stocks",
                    "journal-pull": "Journal pull", "ares-autofix-watcher": "Autofix watcher",
                    "ares-autofix-audit": "Autofix audit", "zeus-horcrux": "Backup → Zeus"}


@app.route("/logs")
@require_auth
def logs_index_page():
    """PR-list-style overview of every autofix incident: title, status,
    fix summary. Linked from the Scheduled Jobs panel title."""
    from system.system_info import _read_autofix_incidents
    incidents = _read_autofix_incidents()
    # hide noise by default: rejected duplicates and triage-skipped blips (?all=1 shows everything)
    if request.args.get("all") != "1":
        incidents = [i for i in incidents if i.get("status") not in ("rejected", "triaged_skip")]
    for inc in incidents:
        ts = inc.get("updated_ts")
        inc["updated_ts_human"] = datetime.fromtimestamp(ts, tz=_GALLERY_TZ).strftime("%b %-d, %Y %-I:%M %p") if ts else ""
    # three sections: one-click Merge fixes, human-action items, already-merged.
    # only council_approved has a real Merge button behind it.
    _recent = lambda i: -i.get("updated_ts", 0)
    merge_ready = sorted((i for i in incidents if i.get("status") == "council_approved"), key=_recent)
    merged = sorted((i for i in incidents if i.get("status") == "resolved"), key=_recent)
    needs_you = sorted((i for i in incidents if i.get("status") not in ("council_approved", "resolved")), key=_recent)
    return render_template("logs_index.html", boot=get_system_info(),
                           merge_ready=merge_ready, needs_you=needs_you, merged=merged,
                           total=len(incidents))


@app.route("/logs/job/<unit>")
@require_auth
def job_log_page(unit):
    from system.system_info import _read_host_crons
    label = _CRON_LOG_LABELS.get(unit, unit)
    if unit not in _CRON_LOG_UNITS:
        return render_template("job_log.html", boot=get_system_info(), title=label, unit=unit,
                                job_ok=True, job_header=None, log_lines=None, log_empty="unknown job")
    if unit == "zeus-horcrux":
        content, empty = None, "this job runs on ZEUS, not ARES -- no local log to show"
    else:
        path = os.path.join(_CRON_LOG_DIR, f"{unit}.log")
        try:
            with open(path, errors="replace") as f:
                content = "".join(f.readlines()[-800:])
            empty = None
        except OSError:
            content, empty = None, "no log yet for this job"

    job = next((j for j in _read_host_crons() if j.get("unit") == unit), None)
    job_header = None
    job_ok = True
    if job:
        job_ok = bool(job.get("ok"))
        job_header = [
            {"label": "Status", "value": "OK" if job_ok else "FAILED", "cls": "ok" if job_ok else "bad"},
            {"label": "Last run", "value": datetime.fromtimestamp(job["last"], tz=_GALLERY_TZ).strftime("%b %-d, %-I:%M %p") if job.get("last") else "—"},
            {"label": "Next run", "value": datetime.fromtimestamp(job["next"], tz=_GALLERY_TZ).strftime("%b %-d, %-I:%M %p") if job.get("next") else "—"},
        ]

    # Per-line parse for the trace panel: journalctl short format is
    # "Sep 14 03:00:01 host unit[pid]: message" -- split into ts / service /
    # message columns; classify failures red and clean completions green.
    log_lines = None
    if content:
        log_lines = []
        line_re = re.compile(r"^([A-Z][a-z]{2}\s+\d+\s\d{2}:\d{2}:\d{2})\s+\S+\s+(\S+?:)\s?(.*)$")
        for line in content.splitlines():
            low = line.lower()
            if any(k in low for k in ("failed", "failure", "error", "traceback")):
                cls = "err"
            elif any(k in low for k in ("finished", "deactivated successfully", "succeeded")):
                cls = "fine"
            else:
                cls = ""
            m = line_re.match(line)
            if m:
                log_lines.append({"ts": m.group(1), "svc": m.group(2), "msg": m.group(3), "cls": cls})
            else:
                log_lines.append({"ts": "", "svc": "", "msg": line, "cls": cls})

    return render_template("job_log.html", boot=get_system_info(), title=label, unit=unit,
                            job_header=job_header, job_ok=job_ok,
                            log_lines=log_lines, log_empty=empty)


_AUTOFIX_APPLY_URL = "http://192.168.20.51:7684"
_AUTOFIX_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops", "autofix", ".apply-token")


def _autofix_token():
    """Reads the apply.py auth token via the shared host<->LXC bind mount --
    never ares-shell-ctl's token, a separate secret entirely (spec 2026-09-13)."""
    with open(_AUTOFIX_TOKEN_FILE) as f:
        return f.read().strip()


_AUTOFIX_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops", "autofix", "logs")


@app.route("/api/autofix/log/<incident_id>")
@require_auth
def autofix_log(incident_id):
    """Persisted headless-Claude session log for one incident (written by
    ops/autofix/finalize.py). Bounded read -- these can be long-running
    sessions with a lot of tool-call output."""
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", incident_id)
    path = os.path.join(_AUTOFIX_LOG_DIR, f"{safe_id}.log")
    try:
        with open(path, errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return jsonify({"error": "no log for this incident"}), 404
    return jsonify({"log": "".join(lines[-500:])})


def _paragraphize(text, sentences_per_para=2):
    """Diagnosis reasoning often comes back from the LLM as one dense
    run-on paragraph (no blank lines). Group every N sentences into a
    paragraph so it actually reads as prose instead of a wall of text."""
    text = (text or "").strip()
    if not text:
        return []
    if "\n\n" in text:  # already has real paragraph breaks -- respect them
        return [p.strip() for p in text.split("\n\n") if p.strip()]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return [" ".join(sentences[i:i + sentences_per_para]) for i in range(0, len(sentences), sentences_per_para)]


def _parse_manual_steps(text):
    """Splits 'Run on EROS...: \\n 1. Label: command' into a real list --
    returns (intro: str, items: list[{"label", "command"}]). Each item's
    first colon separates the human label from the actual command, if any.
    Falls back to a single unlabeled item if nothing looks numbered."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    intro, raw_items = "", []
    for line in lines:
        m = re.match(r"^\d+[.)]\s*(.+)$", line)
        if m:
            raw_items.append(m.group(1))
        elif not raw_items:
            intro = (intro + " " + line).strip()
    if not raw_items and lines:
        raw_items = lines
        intro = ""
    items = []
    for raw in raw_items:
        m = re.match(r"^([^:]{1,60}):\s*(.+)$", raw)
        items.append({"label": m.group(1), "command": m.group(2)} if m else {"label": "", "command": raw})
    return intro, items


def _diff_lines(diff_text):
    out = []
    for line in (diff_text or "").splitlines():
        if line.startswith("@@"):
            cls = "hunk"
        elif line.startswith("+") and not line.startswith("+++"):
            cls = "add"
        elif line.startswith("-") and not line.startswith("---"):
            cls = "del"
        else:
            cls = ""
        out.append({"text": line, "cls": cls})
    return out


@app.route("/logs/incident/<incident_id>")
@require_auth
def incident_log_page(incident_id):
    from system.system_info import _read_autofix_incidents
    incidents = _read_autofix_incidents()
    inc = next((i for i in incidents if i["id"] == incident_id), None)
    if inc is None:
        return render_template("log_view.html", boot=get_system_info(), title="incident not found",
                                subtitle="", status_pill="Not found", status_pill_cls="bad",
                                what_it_does="", why_paragraphs=[], council_votes=[], council_summary={},
                                council_verdict="", code_section=None, log_content=None,
                                box="—", when="—", show_approve_reject=False, incident_id=incident_id), 404

    diag = inc.get("diagnosis", {})
    status = inc.get("status", "new")
    status_cls = "ok" if status == "resolved" else (
        "bad" if status in ("council_held", "stale_diff_needs_human", "revert_failed_needs_human", "diagnosis_timeout")
        else "pending")
    status_label = {
        "resolved": "Resolved", "council_approved": "Ready to apply",
        "recommendation_ready": "Recommendation", "council_held": "Held by council",
        "rejected": "Rejected", "diagnosis_timeout": "Diagnosis failed",
        "stale_diff_needs_human": "Stale, needs review", "revert_failed_needs_human": "Revert failed",
        "command_execution_failed": "Execution failed",
    }.get(status, status)

    when = datetime.fromtimestamp(inc["updated_ts"], tz=_GALLERY_TZ).strftime("%b %-d, %Y %-I:%M %p") if inc.get("updated_ts") else "—"

    # Simple layout: PROBLEM + SOLUTION, both plain English. Prefer the
    # council synthesis's jargon-free fields; only fall back to the raw
    # technical reasoning if the synthesis never produced them.
    summary = diag.get("council_summary") or {}
    problem = summary.get("problem") or " ".join(_paragraphize(diag.get("reasoning"))) or diag.get("triage_reason", "")
    solution = summary.get("solution") or summary.get("simple_explanation") or summary.get("what_happens") or diag.get("fix_title", "")

    return render_template("log_view.html", boot=get_system_info(),
                            title=diag.get("fix_title") or inc.get("title", incident_id),
                            subtitle=inc.get("title", "") if diag.get("fix_title") else "",
                            status_pill=status_label, status_pill_cls=status_cls,
                            problem=problem, solution=solution,
                            council_votes=diag.get("council_votes", []),
                            council_discussion=diag.get("council_discussion", []),
                            council_verdict=diag.get("council_verdict", ""),
                            box=diag.get("box", "ARES"), when=when,
                            show_approve_reject=(status == "council_approved"), incident_id=incident_id)


@app.route("/api/autofix/approve/<incident_id>", methods=["POST"])
@require_auth
def autofix_approve(incident_id):
    import requests as _rq
    try:
        token = _autofix_token()
    except OSError:
        return jsonify({"error": "autofix apply service not installed"}), 503
    try:
        r = _rq.post(f"{_AUTOFIX_APPLY_URL}/apply/{incident_id}",
                     headers={"Authorization": f"Bearer {token}"}, timeout=60)
        return jsonify(r.json()), r.status_code
    except _rq.exceptions.RequestException as e:
        return jsonify({"error": f"apply service unreachable: {e}"}), 502


@app.route("/api/autofix/reject/<incident_id>", methods=["POST"])
@require_auth
def autofix_reject(incident_id):
    import requests as _rq
    try:
        token = _autofix_token()
    except OSError:
        return jsonify({"error": "autofix apply service not installed"}), 503
    try:
        r = _rq.post(f"{_AUTOFIX_APPLY_URL}/reject/{incident_id}",
                     headers={"Authorization": f"Bearer {token}"}, timeout=15)
        return jsonify(r.json()), r.status_code
    except _rq.exceptions.RequestException as e:
        return jsonify({"error": f"apply service unreachable: {e}"}), 502


@app.route("/api/peer")
@require_auth
def api_peer():
    """Returns the sister box's config (name/tag/url) from env. The browser then
    fetches PEER_URL/healthz itself (client-side) — the ARES Flask is in an LXC
    with no route to the peer's tailscale IP, but the user's browser is on the
    tailnet and reaches both boxes. {configured:false} when no PEER_URL is set."""
    url = os.getenv("PEER_URL", "").strip()
    if not url:
        return jsonify({"configured": False})
    return jsonify({"configured": True, "name": os.getenv("PEER_NAME", "Sister"),
                    "tag": os.getenv("PEER_TAG", ""), "url": url})


# Snapshot ports for the FAI outreach apps (ZEUS). Override with OUTREACH_SNAPSHOTS.
_OUTREACH_DEFAULT = ("http://localhost:8080/snapshot.json,"
                     "http://localhost:8090/snapshot.json,"
                     "http://127.0.0.1:8093/snapshot.json")

# The snapshot's own weekly buckets are miscounted (they've shown impossible
# figures like "15 replies this week" for a client with 7 lifetime replies).
# The real send ledger is the multi-tenant `sends` table — query it directly for
# a trustworthy "sent this week" per tenant. Cached; node/pg/DATABASE_URL live in
# the FAI bdr dir, so we borrow its resolution by running there.
_OUTREACH_BDR = os.getenv(
    "OUTREACH_BDR_DIR",
    "/srv/mergerfs/PROMETHEUS/BUSINESS/AUTOMATION-IBT/FAMILYCARESF/bdr")
_LEDGER_JS = (
    "import('dotenv/config').then(async()=>{const{Pool}=await import('pg');"
    "const p=new Pool({connectionString:process.env.DATABASE_URL});"
    "const r=await p.query(\"SELECT t.slug, count(s.*)::int wk_sent FROM tenants t "
    "LEFT JOIN sends s ON s.tenant_id=t.id AND s.sent_at > now()-interval '7 days' "
    "GROUP BY t.slug\");console.log(JSON.stringify(r.rows));await p.end();})"
    ".catch(e=>{console.error(e.message);process.exit(1);})")
_ledger_cache = {"ts": 0.0, "by_slug": {}}


def _outreach_ledger():
    """{slug: sent_last_7_days} from the real `sends` ledger. Cached 5 min; {} if
    the DB or node is unreachable (caller falls back to snapshot figures)."""
    import subprocess
    import time
    if time.time() - _ledger_cache["ts"] < 300:
        return _ledger_cache["by_slug"]
    by_slug = {}
    try:
        r = subprocess.run(["node", "-e", _LEDGER_JS], cwd=_OUTREACH_BDR,
                           capture_output=True, text=True, timeout=15)
        for row in json.loads(r.stdout or "[]"):
            by_slug[str(row.get("slug", "")).lower()] = int(row.get("wk_sent") or 0)
    except Exception:
        by_slug = _ledger_cache["by_slug"]        # keep last-good on a transient failure
    _ledger_cache.update(ts=time.time(), by_slug=by_slug)
    return by_slug


def _ledger_wk_sent(ledger, company):
    """Match a snapshot company name to a tenant slug (e.g. 'Ibtakar Labs'->'ibtakar')."""
    norm = "".join(ch for ch in (company or "").lower() if ch.isalnum())
    for slug, n in ledger.items():
        if norm and (norm.startswith(slug) or slug.startswith(norm)):
            return n
    return None


@app.route("/api/outreach")
@require_auth
def api_outreach():
    """ZEUS business-outreach roll-up: reads the FAI apps' local snapshot.json and
    returns per-client headline metrics (sent / reply-rate / meetings / needs-reply
    / opens). [] on any box that isn't ZEUS or has no snapshots reachable."""
    if os.getenv("HOST_BRAND", "").upper() != "ZEUS":
        return jsonify([])
    import requests
    ledger = _outreach_ledger()
    out = []
    for u in os.getenv("OUTREACH_SNAPSHOTS", _OUTREACH_DEFAULT).split(","):
        u = u.strip()
        if not u:
            continue
        try:
            d = requests.get(u, timeout=4).json()
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        s = d.get("summary") or {}
        contacted = s.get("contacted") or 0
        replied = s.get("replied") or 0
        nr = d.get("needsReply")
        nr = nr if isinstance(nr, list) else []
        opens = d.get("opens") or {}
        wk = d.get("weekly")
        wk = wk if isinstance(wk, list) else []
        company = d.get("company") or "?"
        # "Sent this week" comes from the real ledger, not the miscounted snapshot
        # bucket; fall back to the snapshot only if the ledger is unreachable.
        led = _ledger_wk_sent(ledger, company)
        wk_sent = led if led is not None else ((wk[-1] if wk else {}).get("sent") or 0)
        out.append({
            "company": company,
            "sent": s.get("totalSends") or contacted or 0,
            "contacted": contacted,
            "replied": replied,
            "reply_rate": round(100 * replied / contacted, 1) if contacted else 0,
            # sent this week is ledger-true; reply metrics stay lifetime (accurate)
            "week": {"sent": wk_sent},
            "meetings": s.get("meetings") or 0,
            "warm": s.get("warm") or 0,
            "needs_reply": len(nr),
            "opens": opens.get("total") or 0,
            "opens_unique": opens.get("unique") or 0,
            "updated": d.get("updatedAt"),
            # reply queue: who to get back to, top 6, high-priority first
            "needs": [{"name": c.get("name") or c.get("company") or "?",
                       "company": company, "priority": bool(c.get("highPriority"))}
                      for c in sorted(nr, key=lambda c: not c.get("highPriority"))[:6]],
            # weekly trend: last 9 weeks of {wk, sent, replied}
            "weekly": [{"wk": w.get("wk"), "sent": w.get("sent") or 0, "replied": w.get("replied") or 0}
                       for w in wk[-9:] if isinstance(w, dict)],
        })
    return jsonify(out)


@app.route("/drives")
@require_auth
def drives_page():
    return render_template("drives.html")

@app.route("/sigma")
@require_auth
def sigma_page():
    return render_template("sigma.html")

@app.route("/panel")
@require_auth
def panel_page():
    return render_template("panel.html")

@app.route("/final")
@require_auth
def final_page():
    return render_template("final.html")

@app.route("/adam")
@require_auth
def adam_page():
    return render_template("adam.html")

@app.route("/kriti")
@require_auth
def kriti_page():
    return render_template("kriti.html")

@app.route("/script")
@require_auth
def script_page():
    return render_template("script.html")

@app.route("/se-course")
@require_auth
def se_course_page():
    return render_template("se_course.html")

@app.route("/api/se-course/eval", methods=["POST"])
@require_auth
def se_course_eval():
    data = request.get_json(force=True) or {}
    scenario = (data.get("scenario") or "").strip()
    prompt   = (data.get("prompt")   or "").strip()
    model_ans= (data.get("model")    or "").strip()
    user_ans = (data.get("answer")   or "").strip()
    if not user_ans:
        return jsonify({"feedback": "Write something first."}), 400
    SIGMA_FACTS = (
        "SIGMA COMPUTING — VERIFIED PRODUCT FACTS (use these as ground truth, never contradict them):\n"
        "- Sigma is a cloud-native BI tool that runs entirely on top of Snowflake (or other CDWs). It does NOT store, cache, or copy user data.\n"
        "- Every Sigma action generates a SQL query that runs live against the customer's Snowflake warehouse. Sigma is a query layer, not a data layer.\n"
        "- Snowflake Query History (Activity > Query History in Snowflake UI) shows every SQL query Sigma sends — including generated SQL, execution time, compute used, request ID. This is the audit trail the data team uses.\n"
        "- Sigma also has a built-in 'View Query' button on individual workbook elements that shows the SQL for that specific element.\n"
        "- Input Tables: three types — Empty, CSV, Linked. Linked input tables write back to Snowflake with a SIGDS_ prefix. They cannot be queried directly — Sigma creates a warehouse view on top. The write shows up in Snowflake Query History as a DML statement.\n"
        "- Sigma connects to Snowflake via OAuth (most common), key-pair auth, or basic auth (deprecated). OAuth means Sigma inherits the user's Snowflake role and permissions — no shadow permission model in Sigma.\n"
        "- Sigma Assistant (AI): uses Snowflake Cortex Analyst. Takes natural language questions, generates SQL against the customer's Snowflake data, returns a chart or table. Has an 'Analysis Breakdown' showing which data source it used and why. SQL button proves it ran against real data.\n"
        "- Sigma is NOT read-only — it supports write-back via Input Tables. But it does not modify existing data in place; it writes to new tables with SIGDS_ prefix.\n"
        "- Workbooks: the main Sigma object. Contains pages. Five element types: Data (tables, charts, pivot), Input (input tables), Control (filters, date pickers), UI (text, images), Layout.\n"
        "- Sigma does NOT create derived tables or cache query results. Sigma does NOT extract data or store it outside the warehouse.\n"
        "- Permissions are managed in Snowflake, not Sigma. Whatever role the user has in Snowflake is what they can access in Sigma.\n"
    )
    sys_p = (
        "You are a fair SE coach evaluating a trainee's answer to a Sigma Computing sales engineering scenario.\n\n"
        + SIGMA_FACTS + "\n"
        "Your job: evaluate the trainee's answer against the MODEL ANSWER. The model answer is the source of truth for what matters — do not dock points for concepts not in the model answer, even if you think they're relevant.\n\n"
        "Score based on whether they hit the KEY CONCEPTS in the model answer — not polish, not phrasing. "
        "If they said the same thing in different words, that counts. Only dock for concepts genuinely absent or factually wrong per the Sigma facts above.\n\n"
        "Use plain text only — no markdown, no bold, no asterisks. Use plain dashes only if listing items.\n"
        "Format exactly:\n"
        "SCORE: X/10\n"
        "WHAT YOU GOT RIGHT: [concepts they nailed]\n"
        "WHAT YOU MISSED: [real gaps only. If nothing, say 'Nothing major.']\n"
        "VERDICT: [one honest sentence]\n"
        "10/10 ANSWER: [perfect version in their voice — what they'd say out loud in the panel]\n\n"
        "Scoring: 9-10 = nailed it. 7-8 = solid, minor gaps. 5-6 = core idea but missing something important. Below 5 = wrong direction."
    )
    user_p = (
        f"SCENARIO: {scenario}\n"
        f"QUESTION: {prompt}\n"
        f"MODEL ANSWER (reference only, never quote directly): {model_ans}\n"
        f"TRAINEE ANSWER: {user_ans}\n\n"
        "Before scoring, list every concept from the MODEL ANSWER. "
        "Then check each one: did the TRAINEE ANSWER mention it, even in different words? "
        "Only mark something as MISSED if it is genuinely absent from the trainee answer — not if they said it differently. "
        "Do not dock points for something the trainee said. Read carefully."
    )
    import requests as _rq
    try:
        r = _rq.post("http://192.168.20.51:7690", json={"prompt": sys_p + "\n\n" + user_p}, timeout=60)
        feedback = r.json().get("text") or "No response."
    except Exception as e:
        feedback = _ollama_complete(sys_p, user_p)
    return jsonify({"feedback": feedback})

@app.route("/tech")
@require_auth
def tech_page():
    return render_template("tech.html")

@app.route("/kayla")
@require_auth
def kayla_page():
    return render_template("kayla.html")

@app.route("/josh")
@require_auth
def josh_page():
    return render_template("josh.html")

@app.route("/yc")
@require_auth
def yc_page():
    return render_template("yc.html")

@app.route("/demo")
@require_auth
def demo_page():
    return render_template("sigma_demo.html")


@app.route("/terminal")
@require_auth
def terminal():
    # Terminal tab = a real shell on the ARES host (pve), persistent tmux,
    # served tailnet-only via ttyd (iframe -> http://100.77.42.110:7681/).
    # no-store so the mobile control panel is never served stale from cache.
    resp = make_response(render_template("shell.html", username=session.get("username", LOGIN_USER)))
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@app.route("/breakdown")
@require_auth
def breakdown_page():
    # no-store so the SW/browser never pins a stale copy of this dynamic page
    resp = make_response(render_template("breakdown.html"))
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


# ───────────────────────────────────────────────────────────────────────
# BUSINESS / VENTURES DASHBOARD
# Aggregates the JSON dumps the various FAI tooling already writes:
#   • icp_workflow_victims.json   — HubSpot snapshot
#   • linkedout/data/*.json       — LinkedIn outreach daemon state
#   • bdr/data/activity_log.json  — Gmail BDR sender activity
#   • business/<venture>/         — folder of ventures
# Reads on every request — files are tiny (<1MB) and the daemon writes
# them continuously, so we want fresh-on-every-paint, no cron.
# ───────────────────────────────────────────────────────────────────────

# Candidate roots where the FAI/business data tree might live. Each box
# has it in a slightly different place — ARES sees /mnt/data/PROMETHEUS/WORK,
# ZEUS sees the mergerfs union plus a backup dir. We probe these in order
# and use the first path that exists per-file, so the same module works
# on every box without environment-specific config.
_WORK_CANDIDATES = [
    os.environ.get("WORK_ROOT"),
    "/mnt/data/PROMETHEUS/WORK",
    "/srv/mergerfs/PROMETHEUS",
    "/srv/dev-disk-by-uuid-de676cab-cff5-4143-a3fa-174e88f13b4a/PROMETHEUS_BACKUP/ares/WORK",
    "/Volumes/PROMETHEUS/WORK",
]


def _find(*parts):
    """First existing path across _WORK_CANDIDATES for the given suffix."""
    for r in _WORK_CANDIDATES:
        if not r:
            continue
        p = os.path.join(r, *parts)
        if os.path.exists(p):
            return p
    return None


def _safe_load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _file_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


# ── Time-window helpers ─────────────────────────────────────────────────
# All linkedout/bdr logs use ISO-8601 with trailing Z. Parse once, bucket
# by today / week / month / total. Windows are UTC-anchored — the daemon
# writes UTC timestamps so this matches the underlying truth.
import datetime as _dt

def _parse_ts(s):
    if not s or not isinstance(s, str):
        return None
    try:
        # Strip 'Z' and parse, treat as UTC-naive
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return _dt.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _window_bounds():
    """Return dict of {label: cutoff_datetime_utc}. Events newer than the
    cutoff count for that window. None means no cutoff (total)."""
    now = _dt.datetime.now(_dt.timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "today": midnight,
        "week": now - _dt.timedelta(days=7),
        "month": now - _dt.timedelta(days=30),
        "total": None,
    }


def _bucket_events(events, ts_field):
    """Count events per window. `events` is a list of dicts; each must
    have a parseable timestamp under ts_field."""
    bounds = _window_bounds()
    counts = {k: 0 for k in bounds}
    for ev in events:
        t = _parse_ts(ev.get(ts_field) if isinstance(ev, dict) else None)
        if not t:
            continue
        for k, cutoff in bounds.items():
            if cutoff is None or t >= cutoff:
                counts[k] += 1
    return counts


def _fai_summary():
    p = _find("FAI", "icp_workflow_victims.json")
    raw = _safe_load_json(p, {}) if p else {}
    if not raw:
        return {"available": False, "reason": "No icp_workflow_victims.json yet"}
    summary = raw.get("summary", {})
    sources = summary.get("by_source", {}) or {}
    top_sources = sorted(sources.items(), key=lambda kv: -kv[1])[:8]
    statuses = summary.get("by_status", {}) or {}
    status_total = sum(statuses.values()) or 1
    status_rows = sorted(
        [{"label": k, "count": v, "pct": round(100 * v / status_total, 1)}
         for k, v in statuses.items()],
        key=lambda r: -r["count"],
    )
    return {
        "available": True,
        "run_at": raw.get("run_at"),
        "total": raw.get("total", 0),
        "by_stage": summary.get("by_stage", {}),
        "by_tier": summary.get("by_tier", {}),
        "has_deal": summary.get("has_deal", 0),
        "no_deal": summary.get("no_deal", 0),
        "status_rows": status_rows,
        "top_sources": [{"name": k or "(blank)", "count": v} for k, v in top_sources],
    }


def _linkedout_summary():
    tracker_p = _find("FAI", "linkedout", "data", "outreach-tracker.json")
    actions_p = _find("FAI", "linkedout", "data", "action-log.json")
    daemon_p = _find("FAI", "linkedout", "data", "daemon-state.json")
    tracker = _safe_load_json(tracker_p, {}) if tracker_p else {}
    actions = _safe_load_json(actions_p, []) if actions_p else []
    daemon = _safe_load_json(daemon_p, {}) if daemon_p else {}
    if not tracker and not actions:
        return {"available": False, "reason": "No outreach data yet"}

    contacts = list(tracker.values()) if isinstance(tracker, dict) else []
    by_status = {}
    for c in contacts:
        s = c.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1
    li_msgs = sum(c.get("li_msgs", 0) for c in contacts)
    li_followups = sum(c.get("li_followups", 0) for c in contacts)

    # Most-engaged contacts (by li_msgs sent)
    top_contacts = sorted(
        [c for c in contacts if c.get("li_msgs", 0) > 0],
        key=lambda c: -c.get("li_msgs", 0),
    )[:6]
    top_contacts = [{
        "name": c.get("name", "Unknown"),
        "li_msgs": c.get("li_msgs", 0),
        "status": c.get("status", "unknown"),
        "last_ts": c.get("last_li_msg_ts") or c.get("first_contact_ts"),
    } for c in top_contacts]

    # Action-log: connection requests over time
    action_types = {}
    for a in actions:
        t = a.get("type", "unknown")
        action_types[t] = action_types.get(t, 0) + 1

    return {
        "available": True,
        "contacts": len(contacts),
        "by_status": by_status,
        "li_msgs": li_msgs,
        "li_followups": li_followups,
        "top_contacts": top_contacts,
        "action_count": len(actions),
        "action_types": action_types,
        "daemon": {
            "date": daemon.get("date"),
            "actions": daemon.get("actions", 0),
            "messaged_today": daemon.get("messagedToday", []),
            "connected_today": daemon.get("connectedToday", []),
        },
        "last_updated": _file_mtime(tracker_p) if tracker_p else None,
    }


def _bdr_summary():
    p = _find("FAI", "bdr", "data", "activity_log.json")
    if not p:
        return {"available": False, "reason": "BDR engine has not been authorized yet."}
    log = _safe_load_json(p, [])
    if not isinstance(log, list) or not log:
        return {"available": False, "reason": "BDR engine authorized — no sends yet."}
    sent = [e for e in log if e.get("type") == "send" or e.get("action") == "send"]
    drafts = [e for e in log if e.get("type") == "draft" or e.get("action") == "draft"]
    return {
        "available": True,
        "total_events": len(log),
        "sent": len(sent),
        "drafts": len(drafts),
        "last_event": log[-1] if log else None,
    }


def _ventures_list():
    root = _find("business")
    if not root or not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        try:
            files = [f for f in os.listdir(d) if not f.startswith(".")]
        except OSError:
            files = []
        out.append({
            "id": name.lower(),
            "name": name,
            "file_count": len(files),
            "mtime": _file_mtime(d),
        })
    return out


def _business_payload():
    return {
        "fai": _fai_summary(),
        "linkedout": _linkedout_summary(),
        "bdr": _bdr_summary(),
        "ventures": _ventures_list(),
    }


# ── Per-venture detail (Finder-style sidebar pattern) ──────────────────

def _fai_venture():
    """Aggregate FAI metrics across LinkedIn outreach, BDR email, and the
    HubSpot ICP snapshot. Returns: status, four time-window metric sets,
    and a chronological activity log."""
    connect_log = _safe_load_json(_find("FAI", "linkedout", "data", "connect-log.json") or "", []) or []
    message_log = _safe_load_json(_find("FAI", "linkedout", "data", "message-log.json") or "", []) or []
    followup_log = _safe_load_json(_find("FAI", "linkedout", "data", "followup-log.json") or "", []) or []
    tracker = _safe_load_json(_find("FAI", "linkedout", "data", "outreach-tracker.json") or "", {}) or {}
    bdr_log = _safe_load_json(_find("FAI", "bdr", "data", "activity_log.json") or "", []) or []
    icp = _safe_load_json(_find("FAI", "icp_workflow_victims.json") or "", {}) or {}

    # Per-window event counts
    connects = _bucket_events(connect_log, "sent_at")
    messages = _bucket_events(message_log, "sent_at") if isinstance(message_log, list) else {"today":0,"week":0,"month":0,"total":0}
    followups = _bucket_events(followup_log, "time")
    bdr_sends = _bucket_events([e for e in bdr_log if (e.get("type") == "send" or e.get("action") == "send")], "timestamp") if bdr_log else {"today":0,"week":0,"month":0,"total":0}

    # Email sequences set up: count tracker contacts where email_sequenced
    # is true AND first_contact_ts falls in window.
    bounds = _window_bounds()
    seq_counts = {k: 0 for k in bounds}
    contacts = list(tracker.values()) if isinstance(tracker, dict) else []
    for c in contacts:
        if not c.get("email_sequenced"):
            continue
        t = _parse_ts(c.get("first_contact_ts"))
        if not t:
            continue
        for k, cutoff in bounds.items():
            if cutoff is None or t >= cutoff:
                seq_counts[k] += 1

    # Reply rate. Without explicit reply events we approximate: contacts
    # whose status is "messaged" and have li_msgs > 1 (a reply usually
    # produces a back-and-forth) divided by total messaged. Mark as
    # estimate so the UI can label it.
    messaged = [c for c in contacts if c.get("status") == "messaged"]
    replied = [c for c in messaged if c.get("li_msgs", 0) > 1]
    reply_rate = round(100 * len(replied) / len(messaged), 1) if messaged else None

    metrics = {}
    for k in bounds:
        metrics[k] = {
            "connects": connects[k],
            "messages": messages[k],
            "followups": followups[k],
            "email_sequences": seq_counts[k],
            "emails_sent": bdr_sends[k],
            "reply_rate": reply_rate if k == "total" else None,  # only meaningful overall
            "meetings": 0,  # not yet wired
        }

    # Build a unified activity log: connects + followups + bdr sends.
    activity = []
    for e in connect_log:
        if e.get("sent_at"):
            activity.append({
                "ts": e["sent_at"],
                "kind": "connect",
                "name": e.get("name", "—"),
                "detail": e.get("search_query", ""),
            })
    for e in followup_log:
        if e.get("time"):
            activity.append({
                "ts": e["time"],
                "kind": "followup",
                "name": e.get("name", "—"),
                "detail": "followup #" + str(e.get("followup_num", "?")),
            })
    for e in bdr_log:
        ts = e.get("timestamp") or e.get("sent_at") or e.get("ts")
        if ts:
            activity.append({
                "ts": ts,
                "kind": "email",
                "name": e.get("to") or e.get("recipient") or "—",
                "detail": e.get("subject") or e.get("type", ""),
            })
    activity.sort(key=lambda x: x["ts"], reverse=True)
    activity = activity[:30]

    return {
        "id": "fai",
        "name": "FAI",
        "subtitle": "Insurance · FurtherAI",
        "status": "active",
        "available": True,
        "metrics": metrics,
        "activity": activity,
        "icp": {
            "total": icp.get("total", 0),
            "run_at": icp.get("run_at"),
            "by_stage": (icp.get("summary") or {}).get("by_stage", {}),
            "by_tier": (icp.get("summary") or {}).get("by_tier", {}),
            "has_deal": (icp.get("summary") or {}).get("has_deal", 0),
            "no_deal": (icp.get("summary") or {}).get("no_deal", 0),
        },
        "totals": {
            "contacts": len(contacts),
            "li_msgs_lifetime": sum(c.get("li_msgs", 0) for c in contacts),
            "connected": sum(1 for c in contacts if c.get("status") == "connected"),
            "messaged": sum(1 for c in contacts if c.get("status") == "messaged"),
            "email_sequenced": sum(1 for c in contacts if c.get("email_sequenced")),
        },
    }


def _placeholder_venture(name, subtitle, status="setup", reason=None, file_count=0):
    """Used for ventures we have a folder for but no instrumented data."""
    blank = {"connects": 0, "messages": 0, "followups": 0, "email_sequences": 0,
             "emails_sent": 0, "reply_rate": None, "meetings": 0}
    return {
        "id": name.lower().replace(" ", "-"),
        "name": name,
        "subtitle": subtitle,
        "status": status,
        "available": False,
        "reason": reason or "No instrumented data yet — drop a daemon + activity log into the venture folder and this card lights up.",
        "metrics": {"today": dict(blank), "week": dict(blank), "month": dict(blank), "total": dict(blank)},
        "activity": [],
        "file_count": file_count,
    }


def _all_ventures_payload():
    """Returns sidebar + per-venture detail for everything we know about."""
    ventures = []

    # Active: FAI is the main one
    ventures.append(_fai_venture())

    # FAMILYCARESF — has folder, no daemon
    fcsf_root = _find("business", "FAMILYCARESF")
    fc_files = 0
    if fcsf_root and os.path.isdir(fcsf_root):
        try:
            fc_files = len([f for f in os.listdir(fcsf_root) if not f.startswith(".")])
        except OSError:
            pass
    ventures.append(_placeholder_venture(
        "Family Care SF", "Home Care · San Francisco",
        status="setup", file_count=fc_files,
        reason="Contract on file. Wire a daemon at WORK/business/FAMILYCARESF/ to start metrics.",
    ))

    # Other portfolio dirs become inactive ventures
    portfolio_root = _find("business")
    if portfolio_root and os.path.isdir(portfolio_root):
        for name in sorted(os.listdir(portfolio_root)):
            if name.upper() == "FAMILYCARESF":
                continue
            d = os.path.join(portfolio_root, name)
            if not os.path.isdir(d):
                continue
            try:
                files = [f for f in os.listdir(d) if not f.startswith(".")]
            except OSError:
                files = []
            ventures.append(_placeholder_venture(
                name.replace("-", " ").title(),
                "Portfolio",
                status="idle", file_count=len(files),
            ))

    return {"ventures": ventures, "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}


@app.route("/business")
@require_auth
def business_page():
    return render_template("business.html", data=_all_ventures_payload())


@app.route("/api/business/summary")
@require_auth
def api_business_summary():
    return jsonify(_business_payload())


@app.route("/api/business/all")
@require_auth
def api_business_all():
    return jsonify(_all_ventures_payload())


# ── Elite Picks — what superinvestors & institutions are buying ──
# Replaces the old personal-portfolio page. Engine lives in elite_picks.py
# (standalone, no Flask/CLIP deps) so the weekly refresh cron can import it
# cheaply. See ops/refresh-elite-picks.sh + ares-elite-picks.timer (Mon 06:00).
from elite_picks import compute_elite_picks


@app.route("/api/elite-picks")
@require_auth
def elite_picks_api():
    return jsonify(compute_elite_picks(force=request.args.get("refresh") == "1"))


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


CRYPTO_MAP = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "DOGE": "dogecoin"}

@app.route("/api/stock-quote/<symbol>")
@require_auth
def stock_quote(symbol):
    import requests as req
    sym = symbol.upper().replace("-USD", "")

    # Crypto — use CoinGecko free API
    if sym in CRYPTO_MAP:
        try:
            r = req.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": CRYPTO_MAP[sym], "vs_currencies": "usd", "include_24hr_change": "true"},
                timeout=5,
            )
            data = r.json().get(CRYPTO_MAP[sym], {})
            price = data.get("usd", 0)
            change_pct = data.get("usd_24h_change", 0)
            return jsonify({"c": price, "dp": change_pct, "d": 0})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # Stocks — Yahoo chart API (free, no key). Google Finance scrape died (302s).
    _UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            r = req.get(
                f"https://{host}/v8/finance/chart/{sym}",
                params={"interval": "1d", "range": "1d"},
                headers={"User-Agent": _UA},
                timeout=5,
            )
            assert r.status_code == 200
            meta = r.json()["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice")
            assert price is not None
            prev = meta.get("chartPreviousClose") or meta.get("previousClose") or 0
            change = round(price - prev, 2) if prev else 0
            change_pct = round((change / prev * 100), 2) if prev else 0
            return jsonify({"c": price, "d": change, "dp": change_pct})
        except Exception:
            continue
    return jsonify({"error": f"No quote found for {sym}"}), 404


# ── Journals ──

# Prefer POOL_ROOT (PROMETHEUS storage root) so journals resolve to the real
# PERSONAL/journals regardless of where the app dir lives. dirname(_APP_DIR)
# only worked when the app sat directly under the storage root — on ARES it
# lives under PROJECTS/, which pointed JOURNALS_DIR at an empty folder.
JOURNALS_DIR = os.path.join(
    os.getenv("POOL_ROOT") or os.path.dirname(_APP_DIR), "PERSONAL", "journals"
)
GOODNOTES_DB = os.path.expanduser(
    "~/Library/Containers/com.goodnotesapp.x/Data/Library/Databases/projection.sqlite"
)
_GN_TEMPLATE_NAMES = {
    "Text Stamps", "Back To School", "Sticky Notes", "Everyday Stickers",
    "Mind Map Shapes", "Ruled Wide", "Squared Paper", "Ruled Narrow",
    "Bright", "Calligraphr-Template",
}
_GN_TEMPLATE_ROOTS = {
    "F6327919-7604-421F-9B60-C38A787F9F42",
    "14AC2082-C07A-4C4F-AF22-A23ACC3B8A5F",
}


def _sync_goodnotes_meta():
    """Read Goodnotes projection DB and write metadata JSON."""
    import sqlite3, shutil, tempfile
    if not os.path.exists(GOODNOTES_DB):
        return
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    try:
        shutil.copy2(GOODNOTES_DB, tmp.name)
        for ext in ["-wal", "-shm"]:
            src = GOODNOTES_DB + ext
            if os.path.exists(src):
                shutil.copy2(src, tmp.name + ext)
        conn = sqlite3.connect(tmp.name)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT d.id, d.name, d.updated_at, COUNT(p.id) as page_count
            FROM documents d
            LEFT JOIN pages p ON p.document_id = d.id AND p.deleted = 0
            WHERE d.deleted = 0 AND d.document_type = 0
            GROUP BY d.id HAVING page_count > 0
            ORDER BY d.updated_at DESC
        """).fetchall()
        folder_items = {
            r["item_id"]: r["root_folder_id"]
            for r in conn.execute(
                "SELECT item_id, root_folder_id FROM folder_to_folder_items WHERE deleted = 0 AND item_type = 1"
            ).fetchall()
        }
        # Fetch per-page created_at dates for each document (ordered by position = PDF order)
        _page_dates_by_doc = {}
        for row in rows:
            page_rows = conn.execute(
                "SELECT created_at FROM pages WHERE document_id = ? AND deleted = 0 ORDER BY position",
                (row["id"],)
            ).fetchall()
            _page_dates_by_doc[row["id"]] = [
                r[0] / 1000 if r[0] and r[0] > 0 else None for r in page_rows
            ]
        conn.close()
    except Exception:
        return
    finally:
        os.unlink(tmp.name)
        for ext in ["-wal", "-shm"]:
            p = tmp.name + ext
            if os.path.exists(p):
                os.unlink(p)

    # Check which have PDFs on disk
    pdfs = set()
    if os.path.isdir(JOURNALS_DIR):
        for f in os.listdir(JOURNALS_DIR):
            if f.lower().endswith(".pdf") and not f.startswith("."):
                pdfs.add(f.lower().replace(".pdf", "").replace("-pdf", ""))

    notebooks = []
    for row in rows:
        name = row["name"]
        if name in _GN_TEMPLATE_NAMES:
            continue
        if folder_items.get(row["id"], "") in _GN_TEMPLATE_ROOTS:
            continue
        ts = row["updated_at"]
        updated = time.strftime("%Y-%m-%d", time.gmtime(ts / 1000)) if ts and ts > 0 else None
        notebooks.append({
            "id": row["id"], "name": name, "pages": row["page_count"],
            "updated": updated, "has_pdf": name.lower() in pdfs,
            "page_dates": _page_dates_by_doc.get(row["id"], []),
        })

    # Try to sync Auto-Backup PDFs from iCloud Drive (via Finder for TCC bypass)
    _sync_autobackup_pdfs()

    # Re-check PDFs after potential auto-backup sync
    pdfs = set()
    if os.path.isdir(JOURNALS_DIR):
        for f in os.listdir(JOURNALS_DIR):
            if f.lower().endswith(".pdf") and not f.startswith("."):
                pdfs.add(f.lower().replace(".pdf", "").replace("-pdf", ""))
    for nb in notebooks:
        nb["has_pdf"] = nb["name"].lower() in pdfs

    os.makedirs(JOURNALS_DIR, exist_ok=True)
    meta_path = os.path.join(JOURNALS_DIR, "goodnotes_meta.json")
    with open(meta_path, "w") as f:
        json.dump({
            "synced_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "notebooks": notebooks,
        }, f, indent=2)


def _sync_autobackup_pdfs():
    """Collect exported PDFs from Goodnotes temp exports and iCloud Drive."""
    if sys.platform != "darwin":
        return
    os.makedirs(JOURNALS_DIR, exist_ok=True)

    # 1. Check Goodnotes export temp directory for any PDFs
    gn_exports = os.path.expanduser(
        "~/Library/Containers/com.goodnotesapp.x/Data/tmp/Exports"
    )
    if os.path.isdir(gn_exports):
        for dirpath, _dirs, files in os.walk(gn_exports):
            for f in files:
                if not f.lower().endswith(".pdf"):
                    continue
                src = os.path.join(dirpath, f)
                dest = os.path.join(JOURNALS_DIR, f)
                # Copy if newer or doesn't exist in journals
                if not os.path.exists(dest) or os.path.getmtime(src) > os.path.getmtime(dest):
                    try:
                        import shutil
                        shutil.copy2(src, dest)
                    except Exception:
                        pass

    # 2. Check iCloud Drive for Auto-Backup PDFs (via Finder for TCC bypass)
    icloud = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs")
    for folder_name in ["GoodNotes Auto Backup", "GoodNotes 5 Auto Backup", "Goodnotes Auto Backup", "GoodNotes"]:
        icloud_path = os.path.join(icloud, folder_name)
        script = f'''
        tell application "Finder"
            try
                set bf to folder (POSIX file "{icloud_path}" as alias)
                set pfs to every file of bf whose name extension is "pdf"
                set r to ""
                repeat with f in pfs
                    set r to r & (POSIX path of (f as alias)) & linefeed
                end repeat
                return r
            on error
                return ""
            end try
        end tell
        '''
        try:
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
            paths = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
            if not paths:
                continue
            for pdf_path in paths:
                dest = os.path.join(JOURNALS_DIR, os.path.basename(pdf_path))
                if not os.path.exists(dest) or os.path.getmtime(pdf_path) > os.path.getmtime(dest):
                    copy_script = f'''
                    tell application "Finder"
                        try
                            duplicate (POSIX file "{pdf_path}" as alias) to folder (POSIX file "{JOURNALS_DIR}" as alias) with replacing
                        end try
                    end tell
                    '''
                    subprocess.run(["osascript", "-e", copy_script], capture_output=True, timeout=120)
            # Don't return — check all folders for PDFs from different backup configs
        except Exception:
            continue


def _start_goodnotes_sync():
    """Sync Goodnotes metadata at startup and every 10 minutes."""
    if sys.platform != "darwin":
        return
    def _loop():
        time.sleep(5)  # wait for app to finish loading
        while True:
            try:
                _sync_goodnotes_meta()
            except Exception:
                pass
            time.sleep(600)  # re-sync every 10 minutes
    threading.Thread(target=_loop, daemon=True).start()


_start_goodnotes_sync()


def _prerender_journal_pages():
    """Background: pre-render journal thumbnails with adaptive scale to avoid OOM."""
    import fitz
    import gc
    MAX_PIXMAP_BYTES = 200 * 1024 * 1024  # cap pixmap at 200MB
    time.sleep(60)  # wait for all startup tasks to settle

    for fname in sorted(os.listdir(JOURNALS_DIR)):
        if not fname.lower().endswith(".pdf") or fname.startswith("."):
            continue
        path = os.path.join(JOURNALS_DIR, fname)
        pdf_mtime = int(os.path.getmtime(path))

        # First pass: find which pages need rendering (no PDF open)
        try:
            doc = fitz.open(path)
            total = doc.page_count
            doc.close()
            del doc
            gc.collect()
        except Exception:
            continue

        needed = []
        for pg_num in range(total):
            cache_key = f"{fname}_{pg_num}_hq4_{pdf_mtime}.jpg"
            cache_path = os.path.join(_JOURNAL_CACHE_DIR, cache_key)
            if not os.path.isfile(cache_path):
                needed.append((pg_num, cache_path))

        if not needed:
            print(f"[journals] {fname}: all {total} pages cached")
            continue

        print(f"[journals] {fname}: {len(needed)} of {total} pages need rendering")

        # Render one page at a time
        doc = fitz.open(path)
        rendered = 0
        for pg_num, cache_path in needed:
            try:
                pg = doc[pg_num]
                r = pg.rect
                base_bytes = r.width * r.height * 3
                scale = min(2.5, (MAX_PIXMAP_BYTES / base_bytes) ** 0.5) if base_bytes > 0 else 2.5
                pix = pg.get_pixmap(matrix=fitz.Matrix(scale, scale))
                pix.save(cache_path, output="jpeg", jpg_quality=90)
                pix = None
                rendered += 1
            except Exception:
                pass
            if rendered % 10 == 0:
                gc.collect()
                time.sleep(0.5)
        doc.close()
        gc.collect()
        print(f"[journals] {fname}: rendered {rendered}/{len(needed)} pages")


threading.Thread(target=_prerender_journal_pages, daemon=True).start()


@app.route("/journals")
@require_auth
def journals_page():
    return render_template("journals.html")


_journal_index_lock = threading.Lock()


def _rebuild_timeline():
    """Rebuild timeline.json (powers 'On this day') from the journal text index.
    Parses the date header on each entry's first page. Idempotent; runs after
    every reindex and once at watcher startup so the feature never goes stale."""
    import re
    import datetime as _dt
    try:
        idx_path = os.path.join(JOURNALS_DIR, "journal_text_index.json")
        with open(idx_path) as f:
            pages = json.load(f).get("pages", {})
        months = ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
                  "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"]
        mi = {m: i + 1 for i, m in enumerate(months)}
        pat = re.compile(r"(" + "|".join(months) + r")\s+(\d{1,2})\s*(?:ST|ND|RD|TH)?\s*,?\s*(\d{4})", re.I)
        this_year = _dt.date.today().year
        entries = []
        for k, v in pages.items():
            text = (v.get("text") or "")
            m = pat.search(text[:140])
            if not m:
                continue
            mon = mi[m.group(1).upper()]
            day = int(m.group(2))
            year = int(m.group(3))
            if not (1 <= day <= 31 and 2010 <= year <= this_year):
                continue
            snippet = re.sub(r"\s+", " ", text[m.end():]).strip()[:240]
            header = re.sub(r"\s+", " ", text[:m.end()]).strip()
            entries.append({
                "year": year, "month": mon, "day": day,
                "header": header, "snippet": snippet,
                "pdf": v.get("pdf"), "page": v.get("page", 0), "offset": 0,
            })
        entries.sort(key=lambda e: (e["year"], e["month"], e["day"], e["pdf"] or "", e["page"]))
        tp = os.path.join(JOURNALS_DIR, "timeline.json")
        tmp = tp + ".tmp"
        with open(tmp, "w") as f:
            json.dump(entries, f, ensure_ascii=False)
        os.replace(tmp, tp)
        print(f"[journals] rebuilt timeline: {len(entries)} entries")
    except Exception as e:
        print(f"[journals] timeline rebuild failed: {e}")


def _reindex_journal_pdf(name):
    """Refresh the full-text search index for one notebook PDF from its text
    layer. Runs in a background thread after an upload so search stays current."""
    try:
        import fitz
        idx = os.path.join(JOURNALS_DIR, "journal_text_index.json")
        path = os.path.join(JOURNALS_DIR, name)
        if not os.path.isfile(path):
            return
        doc = fitz.open(path)
        mt = os.path.getmtime(path)
        new_pages = {}
        for i in range(doc.page_count):
            new_pages[f"{name}:{i}"] = {
                "pdf": name, "page": i, "mtime": mt,
                "text": doc.load_page(i).get_text().strip(),
            }
        doc.close()
        with _journal_index_lock:
            try:
                with open(idx) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {"version": 3, "engine": "pdf-textlayer", "pages": {}}
            pages = data.setdefault("pages", {})
            for k in [k for k, v in pages.items() if v.get("pdf") == name]:
                pages.pop(k)
            pages.update(new_pages)
            data["built"] = time.time()
            tmp = idx + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, idx)
        print(f"[journals] reindexed {name}: {len(new_pages)} pages")
        _rebuild_timeline()
    except Exception as e:
        print(f"[journals] reindex failed for {name}: {e}")


_journal_watch_seen = {}


def _journal_watch_loop():
    """Auto-reindex notebooks that arrive/change via ANY path (Auto-Backup,
    rclone, SMB, scp) — not just the upload endpoint — so search stays current.
    Skips files that are still being written (size not yet stable)."""
    import time as _t
    # seed from the existing index so we don't reindex everything on boot
    try:
        with open(os.path.join(JOURNALS_DIR, "journal_text_index.json")) as f:
            for v in json.load(f).get("pages", {}).values():
                _journal_watch_seen[v["pdf"]] = v.get("mtime", 0)
    except Exception:
        pass
    _rebuild_timeline()
    print("[journals] watcher started (60s poll)")
    while True:
        try:
            for fn in os.listdir(JOURNALS_DIR):
                if not fn.lower().endswith(".pdf") or fn.startswith("._"):
                    continue
                p = os.path.join(JOURNALS_DIR, fn)
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    continue
                if abs(_journal_watch_seen.get(fn, 0) - mt) < 1:
                    continue
                # stability gate: skip if still copying (size changing)
                try:
                    sz1 = os.path.getsize(p)
                    _t.sleep(2)
                    if os.path.getsize(p) != sz1:
                        continue
                except OSError:
                    continue
                _reindex_journal_pdf(fn)
                _journal_watch_seen[fn] = mt
        except Exception as e:
            print(f"[journals] watch error: {e}")
        _t.sleep(60)


threading.Thread(target=_journal_watch_loop, daemon=True).start()


@app.route("/api/journals/upload", methods=["POST"])
@require_auth
def upload_journal():
    """Receive a notebook PDF (from the GoodNotes iOS Shortcut) onto the shelf.
    Accepts multipart 'file', or a raw PDF body with ?name=<notebook>.pdf.
    Auth: session cookie OR `Authorization: Bearer <ARES_API_TOKEN>`."""
    raw_name = request.args.get("name", "")
    fobj = request.files.get("file")
    if not raw_name and fobj:
        raw_name = fobj.filename
    name = secure_filename(raw_name) or "journal.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    os.makedirs(JOURNALS_DIR, exist_ok=True)
    dest = os.path.join(JOURNALS_DIR, name)
    tmp = dest + ".part"
    # Stream to disk in 1 MB chunks so multi-GB notebooks never buffer in RAM.
    src = fobj.stream if fobj else request.stream
    total = 0
    head = src.read(8192)
    if not head:
        return jsonify({"error": "empty upload"}), 400
    if not head[:5].startswith(b"%PDF"):
        return jsonify({"error": "not a PDF"}), 400
    try:
        with open(tmp, "wb") as fh:
            fh.write(head)
            total += len(head)
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
        os.replace(tmp, dest)
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        return jsonify({"error": str(e)}), 500
    # Keep full-text search current without blocking the response.
    threading.Thread(target=_reindex_journal_pdf, args=(name,), daemon=True).start()
    return jsonify({"ok": True, "name": name, "bytes": total})


@app.route("/api/journals/sync", methods=["POST"])
@require_auth
def sync_journals():
    """Trigger a manual Goodnotes sync. Pass ?export=1 to also re-export stale PDFs."""
    try:
        _sync_goodnotes_meta()
        if request.args.get("export"):
            exported = _auto_export_stale()
            if exported:
                _sync_goodnotes_meta()  # refresh metadata after export
            # Auto-OCR new pages in background after sync
            _trigger_background_ocr()
            return jsonify({"ok": True, "exported": exported})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _trigger_background_ocr():
    """Run OCR on any un-indexed journal pages in a background thread."""
    def _run():
        try:
            ocr_script = os.path.join(_APP_DIR, "scripts", "journal_ocr.py")
            if os.path.exists(ocr_script):
                subprocess.Popen(
                    [sys.executable, ocr_script],
                    stdout=open("/tmp/journal_ocr_auto.log", "a"),
                    stderr=subprocess.STDOUT,
                )
        except Exception as e:
            print(f"[OCR] Background trigger failed: {e}")
    threading.Thread(target=_run, daemon=True).start()


def _auto_export_stale():
    """Check for notebooks with stale PDFs and re-export via Goodnotes UI automation."""
    if sys.platform != "darwin":
        return []
    import shutil as _sh

    meta_path = os.path.join(JOURNALS_DIR, "goodnotes_meta.json")
    if not os.path.isfile(meta_path):
        return []
    with open(meta_path) as f:
        meta = json.load(f)

    exported = []
    import datetime as _dt
    for nb in meta.get("notebooks", []):
        name = nb["name"]
        pdf_path = os.path.join(JOURNALS_DIR, name + ".pdf")
        has_pdf = False
        if not os.path.isfile(pdf_path):
            # try alternate name
            for f in os.listdir(JOURNALS_DIR):
                if f.lower().replace("-pdf", "").replace(".pdf", "") == name.lower() and f.endswith(".pdf"):
                    pdf_path = os.path.join(JOURNALS_DIR, f)
                    has_pdf = True
                    break
        else:
            has_pdf = True

        if has_pdf:
            # Compare: Goodnotes updated_at vs PDF mtime
            gn_ts = nb.get("updated")  # "YYYY-MM-DD"
            if not gn_ts:
                continue
            gn_date = _dt.datetime.strptime(gn_ts, "%Y-%m-%d")
            pdf_date = _dt.datetime.fromtimestamp(os.path.getmtime(pdf_path))
            if gn_date.date() <= pdf_date.date():
                continue  # PDF is up to date

        # Need re-export — use UI automation
        try:
            ok = _goodnotes_export_via_ui(name)
            if ok:
                exported.append(name)
        except Exception:
            pass

    return exported


def _goodnotes_export_via_ui(name):
    """Trigger Goodnotes to export current notebook as PDF via UI automation.
    Returns True if a PDF was successfully captured from the exports temp dir."""
    import shutil as _sh

    gn_exports = os.path.expanduser(
        "~/Library/Containers/com.goodnotesapp.x/Data/tmp/Exports"
    )
    # Clear old exports
    if os.path.isdir(gn_exports):
        for d in os.listdir(gn_exports):
            p = os.path.join(gn_exports, d)
            if os.path.isdir(p):
                _sh.rmtree(p, ignore_errors=True)

    # Activate Goodnotes, select all, trigger export
    export_script = '''
    tell application "Goodnotes" to activate
    delay 1
    tell application "System Events"
        tell process "Goodnotes"
            keystroke "a" using command down
            delay 0.3
            click menu item "Export..." of menu 1 of menu bar item "File" of menu bar 1
            delay 2

            set s to sheet 1 of window 1
            set allElems to entire contents of s

            repeat with elem in allElems
                try
                    if class of elem is button then
                        set p to position of elem
                        if (item 2 of p) > 225 and (item 2 of p) < 300 and (item 1 of p) < 950 then
                            click elem
                            delay 0.3
                            exit repeat
                        end if
                    end if
                end try
            end repeat

            repeat with elem in allElems
                try
                    if class of elem is button and description of elem is "Export" then
                        click elem
                        exit repeat
                    end if
                end try
            end repeat

            -- Wait for export to finish
            repeat 90 times
                delay 2
                try
                    set s2 to sheet 1 of window 1
                    set elems2 to entire contents of s2
                    repeat with elem in elems2
                        try
                            if class of elem is pop up button then
                                keystroke "." using command down
                                delay 0.5
                                return "DONE"
                            end if
                            if (description of elem) contains "Save As" then
                                keystroke "." using command down
                                delay 0.5
                                return "DONE"
                            end if
                        end try
                    end repeat
                on error
                    return "DONE_NO_SHEET"
                end try
            end repeat
            return "TIMEOUT"
        end tell
    end tell
    '''
    result = subprocess.run(
        ["osascript", "-e", export_script],
        capture_output=True, text=True, timeout=300
    )

    # Collect exported PDF
    time.sleep(2)
    if os.path.isdir(gn_exports):
        for dirpath, _dirs, files in os.walk(gn_exports):
            for f in files:
                if f.lower().endswith(".pdf"):
                    src = os.path.join(dirpath, f)
                    dest = os.path.join(JOURNALS_DIR, f)
                    _sh.copy2(src, dest)
                    return True
    return False


@app.route("/api/journals")
@require_auth
def list_journals():
    import fitz
    # Read Goodnotes metadata if available
    meta_path = os.path.join(JOURNALS_DIR, "goodnotes_meta.json")
    gn_meta = {}
    gn_synced_at = None
    if os.path.isfile(meta_path):
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            gn_synced_at = meta.get("synced_at")
            for nb in meta.get("notebooks", []):
                gn_meta[nb["name"].lower()] = nb
        except Exception:
            pass

    # Page counts without opening every PDF on every request: the full-text index
    # already stores each page ("<pdf>:<n>") tagged with the PDF's mtime, so the
    # count is just how many pages that PDF has in the index. Opening multi-GB
    # notebooks per request (fitz.open) was the whole slow-load bottleneck. Fall
    # back to fitz only when a PDF is missing/stale in the index (mtime guard; the
    # 60s watcher self-heals a stale entry within a minute).
    idx_counts = {}   # pdf filename -> (page_count, indexed_mtime)
    try:
        with open(os.path.join(JOURNALS_DIR, "journal_text_index.json")) as fh:
            for v in json.load(fh).get("pages", {}).values():
                pdf = v.get("pdf")
                if not pdf:
                    continue
                cnt, _ = idx_counts.get(pdf, (0, v.get("mtime", 0)))
                idx_counts[pdf] = (cnt + 1, v.get("mtime", 0))
    except Exception:
        pass

    # Scan PDFs on disk
    pdf_info = {}
    if os.path.isdir(JOURNALS_DIR):
        for f in sorted(os.listdir(JOURNALS_DIR)):
            if f.startswith(".") or not f.lower().endswith(".pdf"):
                continue
            path = os.path.join(JOURNALS_DIR, f)
            hit = idx_counts.get(f)
            if hit and abs(hit[1] - os.path.getmtime(path)) < 1:
                pages = hit[0]
            else:
                try:
                    doc = fitz.open(path)
                    pages = doc.page_count
                    doc.close()
                except Exception:
                    pages = 0
            key = f.lower().replace(".pdf", "").replace("-pdf", "")
            pdf_info[key] = {"name": f, "pages": pages, "size": os.path.getsize(path),
                             "mtime": os.path.getmtime(path)}

    # Merge: Goodnotes metadata + PDF availability
    journals = []
    seen = set()
    for key, nb in gn_meta.items():
        pdf = pdf_info.get(key) or pdf_info.get(key.replace(" ", "-"))
        entry = {
            "name": pdf["name"] if pdf else nb["name"] + ".pdf",
            "display_name": nb["name"],
            "pages": pdf["pages"] if pdf else 0,
            "gn_pages": nb.get("pages", 0),
            "has_pdf": pdf is not None,
            "updated": nb.get("updated"),
            "gn_id": nb.get("id"),
        }
        if pdf:
            entry["size"] = pdf["size"]
        entry["mtime"] = pdf["mtime"] if pdf else 0
        journals.append(entry)
        seen.add(key)
        if pdf:
            seen.add(next((k for k, v in pdf_info.items() if v == pdf), ""))

    # Add any PDFs not in Goodnotes metadata
    for key, pdf in pdf_info.items():
        if key not in seen:
            journals.append({
                "name": pdf["name"],
                "display_name": pdf["name"].replace(".pdf", "").replace("-pdf", ""),
                "pages": pdf["pages"],
                "gn_pages": 0,
                "has_pdf": True,
                "size": pdf["size"],
                "mtime": pdf["mtime"],
            })

    # Newest-updated journal first (by PDF modified time)
    journals.sort(key=lambda j: j.get("mtime", 0), reverse=True)
    return jsonify({"journals": journals, "synced_at": gn_synced_at})


def _journal_path(name):
    """Confine a request-supplied journal filename to JOURNALS_DIR (realpath). None if it escapes."""
    if not name or "\x00" in name:
        return None
    ap = os.path.realpath(os.path.join(JOURNALS_DIR, name))
    root = os.path.realpath(JOURNALS_DIR)
    if ap != root and not ap.startswith(root + os.sep):
        return None
    return ap


@app.route("/api/journals/<name>/page-dates")
@require_auth
def journal_page_dates(name):
    """Return created_at dates for each page, from metadata JSON (synced from Goodnotes DB)."""
    display_name = name.replace(".pdf", "").replace("-pdf", "")
    meta_path = os.path.join(JOURNALS_DIR, "goodnotes_meta.json")
    if os.path.isfile(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)
        for nb in meta.get("notebooks", []):
            if nb["name"].lower() == display_name.lower():
                return jsonify({"dates": nb.get("page_dates", [])})
    return jsonify({"dates": []})


def _parse_journal_date(text):
    """First date header on a page -> (year, month, day), or None. Tolerant of
    OCR noise; rejects impossible/future dates."""
    import re
    import datetime as _dt
    months = ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
              "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"]
    mi = {m: i + 1 for i, m in enumerate(months)}
    m = re.search(r"(" + "|".join(months) + r")\s+(\d{1,2})\s*(?:ST|ND|RD|TH)?\s*,?\s*(\d{4})",
                  (text or "")[:140], re.I)
    if not m:
        return None
    mon = mi[m.group(1).upper()]
    day = int(m.group(2))
    year = int(m.group(3))
    if not (1 <= day <= 31 and 2010 <= year <= _dt.date.today().year):
        return None
    return (year, mon, day)


def _journal_page_dates(index):
    """Map every (pdf, page) to its chronological date by carrying the last seen
    date header forward across each notebook's pages."""
    from collections import defaultdict
    pages = index.get("pages", {})
    by_pdf = defaultdict(list)
    for v in pages.values():
        by_pdf[v["pdf"]].append(v["page"])
    out = {}
    for pdf, plist in by_pdf.items():
        last = None
        for p in sorted(plist):
            d = _parse_journal_date(pages.get(f"{pdf}:{p}", {}).get("text", ""))
            if d:
                last = d
            out[(pdf, p)] = last
    return out


@app.route("/api/journals/search")
@require_auth
def search_journals():
    """Full-text search across OCR'd journal pages."""
    q = request.args.get("q", "").strip().lower()
    if not q:
        return jsonify({"results": []})

    index_path = os.path.join(JOURNALS_DIR, "journal_text_index.json")
    if not os.path.isfile(index_path):
        return jsonify({"results": [], "error": "OCR index not built yet. Run: python scripts/journal_ocr.py"})

    with open(index_path) as f:
        index = json.load(f)

    pages_idx = index.get("pages", {})
    date_map = _journal_page_dates(index)
    month_abbr = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    results = []
    terms = q.split()
    for key, entry in pages_idx.items():
        text = (entry.get("text") or "").lower()
        if terms and all(t in text for t in terms):
            # Find a snippet around the first match
            pos = text.find(terms[0])
            start = max(0, pos - 60)
            end = min(len(text), pos + 120)
            snippet = entry["text"][start:end].replace("\n", " ").strip()
            if start > 0:
                snippet = "..." + snippet
            if end < len(text):
                snippet = snippet + "..."
            d = date_map.get((entry["pdf"], entry["page"]))
            results.append({
                "pdf": entry["pdf"],
                "page": entry["page"],
                "snippet": snippet,
                "date": d,
                "date_label": (f"{month_abbr[d[1]]} {d[2]}, {d[0]}" if d else ""),
                "year": (d[0] if d else None),
            })

    def _chrono(r):
        d = r.get("date")
        if d:
            return (0, d[0], d[1], d[2], r["pdf"], r["page"])
        return (1, 9999, 99, 99, r["pdf"], r["page"])
    results.sort(key=_chrono)  # oldest first; undated tail last
    return jsonify({"results": results[:120], "total": len(results), "query": q})


@app.route("/api/journals/highlight")
@require_auth
def journal_highlight():
    """Normalized term boxes (0-1, top-left origin) on a page, read straight from
    the GoodNotes text layer via PyMuPDF search_for. Fast and matches search."""
    pdf_name = request.args.get("pdf", "")
    page_num = request.args.get("page", "")
    q = request.args.get("q", "").strip()
    if not pdf_name or page_num == "" or not q:
        return jsonify({"rects": []})
    path = _journal_path(pdf_name)
    if not path or not os.path.isfile(path):
        return jsonify({"rects": []})
    try:
        page_num = int(page_num)
    except ValueError:
        return jsonify({"rects": []})
    doc = _get_pdf(path)
    if page_num < 0 or page_num >= doc.page_count:
        return jsonify({"rects": []})
    pg = doc[page_num]
    rect = pg.rect
    W = rect.width or 1.0
    H = rect.height or 1.0
    needles = [q]
    parts = q.split()
    if len(parts) > 1:
        needles += parts
    rects = []
    seen = set()
    for needle in needles:
        if len(needle) < 2:
            continue
        try:
            for rc in pg.search_for(needle):
                k = (round(rc.x0, 1), round(rc.y0, 1), round(rc.x1, 1), round(rc.y1, 1))
                if k in seen:
                    continue
                seen.add(k)
                rects.append({
                    "x": round(rc.x0 / W, 4),
                    "y": round(rc.y0 / H, 4),
                    "w": round((rc.x1 - rc.x0) / W, 4),
                    "h": round((rc.y1 - rc.y0) / H, 4),
                })
        except Exception:
            pass
    return jsonify({"rects": rects[:200]})


@app.route("/api/journals/on-this-day")
@require_auth
def journals_on_this_day():
    """Entries written on today's month/day in past years, from timeline.json."""
    import datetime as _dt

    month_names = {
        1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
        7: "July", 8: "August", 9: "September", 10: "October", 11: "November", 12: "December",
    }
    date_str = request.args.get("date", "")
    try:
        today = _dt.date.fromisoformat(date_str) if date_str else _dt.date.today()
    except ValueError:
        today = _dt.date.today()

    timeline_path = os.path.join(JOURNALS_DIR, "timeline.json")
    if not os.path.isfile(timeline_path):
        return jsonify({"entries": [], "month_day": "", "error": "timeline.json not built yet"})

    with open(timeline_path) as f:
        timeline = json.load(f)

    matches = [e for e in timeline if e.get("month") == today.month and e.get("day") == today.day]
    matches.sort(key=lambda e: e["year"])

    entries = []
    for e in matches:
        ago = today.year - e["year"]
        entries.append({
            "year": e["year"],
            "ago": "this year" if ago == 0 else f"{ago} year{'s' if ago != 1 else ''} ago",
            "snippet": e.get("snippet") or "",
            "pdf": e.get("pdf"),
            "page": e.get("page", 0),
        })

    return jsonify({
        "entries": entries,
        "month_day": f"{month_names[today.month]} {today.day}",
    })


@app.route("/api/journals/ocr/page-text")
@require_auth
def journal_ocr_page_text():
    """Return OCR text for a specific journal page."""
    pdf = request.args.get("pdf", "")
    page = request.args.get("page", "")
    if not pdf or page == "":
        return jsonify({"text": ""})
    index_path = os.path.join(JOURNALS_DIR, "journal_text_index.json")
    if not os.path.isfile(index_path):
        return jsonify({"text": ""})
    with open(index_path) as f:
        index = json.load(f)
    key = f"{pdf}:{page}"
    entry = index.get("pages", {}).get(key, {})
    return jsonify({"text": entry.get("text", "")})


_easyocr_reader = None
_easyocr_lock = threading.Lock()


def _get_easyocr():
    global _easyocr_reader
    if _easyocr_reader is None:
        with _easyocr_lock:
            if _easyocr_reader is None:
                import easyocr
                _easyocr_reader = easyocr.Reader(['en'], gpu=False, verbose=False)
    return _easyocr_reader


@app.route("/api/journals/ocr/word-boxes")
@require_auth
def journal_word_boxes():
    """Return word-level bounding boxes for a journal page using EasyOCR.
    Boxes are normalized 0-1 with origin at top-left.
    """
    pdf_name = request.args.get("pdf", "")
    page_num = request.args.get("page", "")
    if not pdf_name or page_num == "":
        return jsonify({"words": []})

    try:
        import fitz as _fitz

        pdf_path = _journal_path(pdf_name)
        if not pdf_path or not os.path.isfile(pdf_path):
            return jsonify({"words": []})

        page_num = int(page_num)
        doc = _fitz.open(pdf_path)
        if page_num >= doc.page_count:
            doc.close()
            return jsonify({"words": []})

        page = doc[page_num]
        pix = page.get_pixmap(matrix=_fitz.Matrix(1.5, 1.5))
        img_w, img_h = pix.width, pix.height
        img_bytes = pix.tobytes("png")
        doc.close()

        reader = _get_easyocr()
        results = reader.readtext(img_bytes)

        words = []
        for bbox, text, conf in results:
            # bbox = [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
            x1, y1 = bbox[0]
            x2, y2 = bbox[2]
            words.append({
                "text": text,
                "x": round(x1 / img_w, 4),
                "y": round(y1 / img_h, 4),
                "w": round((x2 - x1) / img_w, 4),
                "h": round((y2 - y1) / img_h, 4),
            })

        return jsonify({"words": words})
    except Exception as e:
        return jsonify({"words": [], "error": str(e)})


@app.route("/api/journals/ocr/status")
@require_auth
def journal_ocr_status():
    """Check OCR index status."""
    index_path = os.path.join(JOURNALS_DIR, "journal_text_index.json")
    if not os.path.isfile(index_path):
        return jsonify({"indexed": 0, "has_index": False})
    with open(index_path) as f:
        index = json.load(f)
    return jsonify({"indexed": len(index.get("pages", {})), "has_index": True})


_JOURNAL_CACHE_DIR = os.path.join(JOURNALS_DIR, ".cache")

# Cache persists across restarts — tag versioning (hq3/full3) handles staleness
os.makedirs(_JOURNAL_CACHE_DIR, exist_ok=True)

_open_pdfs = {}  # keep PDFs open in memory to avoid re-opening 3GB files
_open_pdfs_lock = threading.Lock()

def _get_pdf(path):
    """Keep PDF docs open in memory for fast page access."""
    import fitz
    mtime = os.path.getmtime(path)
    with _open_pdfs_lock:
        cached = _open_pdfs.get(path)
        if cached and cached[1] == mtime:
            return cached[0]
        # Close old doc if mtime changed
        if cached:
            try: cached[0].close()
            except: pass
        doc = fitz.open(path)
        _open_pdfs[path] = (doc, mtime)
        return doc

@app.route("/api/journals/<name>/page/<int:page>")
@require_auth
def journal_page_image(name, page):
    path = _journal_path(name)
    if not path or not os.path.isfile(path):
        abort(404)

    thumb = request.args.get("thumb")
    if thumb == "hq":
        scale, quality, tag = 2.5, 90, "hq4"
    elif thumb:
        scale, quality, tag = 1.5, 80, "lo4"
    else:
        scale, quality, tag = 5.0, 95, "full4"

    # Disk cache
    os.makedirs(_JOURNAL_CACHE_DIR, exist_ok=True)
    pdf_mtime = int(os.path.getmtime(path))
    cache_key = f"{name}_{page}_{tag}_{pdf_mtime}.jpg"
    cache_path = os.path.join(_JOURNAL_CACHE_DIR, cache_key)
    if os.path.isfile(cache_path):
        return send_file(cache_path, mimetype="image/jpeg",
                         max_age=604800, conditional=True)

    import fitz
    doc = _get_pdf(path)
    if page < 0 or page >= doc.page_count:
        abort(404)
    pg = doc[page]
    # Cap scale for huge pages to avoid OOM
    r = pg.rect
    base_bytes = r.width * r.height * 3
    MAX_PIX = 200 * 1024 * 1024
    if base_bytes > 0:
        capped = min(scale, (MAX_PIX / base_bytes) ** 0.5)
    else:
        capped = scale
    mat = fitz.Matrix(capped, capped)
    pix = pg.get_pixmap(matrix=mat)
    img_data = pix.tobytes("jpeg", quality)

    # Save to cache
    try:
        with open(cache_path, "wb") as cf:
            cf.write(img_data)
    except Exception:
        pass

    resp = make_response(img_data)
    resp.headers["Content-Type"] = "image/jpeg"
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/api/alerts")
@require_auth
def hermes_alerts():
    """ZEUS health, pulled by the hermes-watchdog cron on the PVE host
    (/usr/local/bin/hermes-watchdog.sh) every 2 min. A stale file means the
    watchdog/ARES side is dead, which is itself critical."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_data", "hermes_health.json")
    try:
        with open(path) as f:
            d = json.load(f)
    except Exception:
        return jsonify({"level": "red", "reasons": ["watchdog has never written health data"], "age_s": None})
    age = time.time() - d.get("ts", 0)
    level = ["ok"]
    reasons = []
    def worse(l):
        order = {"ok": 0, "amber": 1, "red": 2}
        if order[l] > order[level[0]]:
            level[0] = l
    if age > 300:
        worse("red"); reasons.append("watchdog stale (%ds old) — ARES probe not running" % int(age))
    if not d.get("ssh_ok"):
        worse("red"); reasons.append("ZEUS unreachable over SSH")
    else:
        l1 = d.get("load1") or 0
        if l1 > 20: worse("red"); reasons.append("load %s" % l1)
        elif l1 > 8: worse("amber"); reasons.append("load %s" % l1)
        ren = d.get("renderers") or 0
        if ren > 400: worse("red"); reasons.append("%d chrome renderers (leak)" % ren)
        down = [n for n, s in (d.get("containers") or {}).items() if s != "running"]
        if down: worse("red"); reasons.append("containers down: " + ", ".join(down))
        lo = d.get("linkedout") or {}
        if lo.get("oom_killed"): worse("red"); reasons.append("linkedout OOM-killed")
        elif (lo.get("restart_count") or 0) > 0: worse("amber"); reasons.append("linkedout restarts: %d" % lo["restart_count"])
    return jsonify({"level": level[0], "reasons": reasons, "age_s": int(age), "data": d})


@app.route("/api/eros/ask", methods=["POST"])
@require_auth
def eros_ask():
    """Interactive prompt to an Ollama model on EROS (via the host :11434 proxy).
    Streams tokens back as SSE. Model is chosen client-side from the live model list."""
    import requests as _rq
    d = request.json or {}
    model = (d.get("model") or "llama3.2:3b").strip()
    prompt = (d.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "empty prompt"}), 400
    host = os.getenv("OLLAMA_HOST") or "http://192.168.20.51:11434"
    def gen():
        try:
            with _rq.post(host + "/api/generate", stream=True, timeout=180,
                          json={"model": model, "prompt": prompt, "stream": True,
                                "options": {"num_predict": 400}}) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if o.get("response"):
                        yield "data: " + json.dumps({"t": o["response"]}) + "\n\n"
                    if o.get("done"):
                        yield "data: " + json.dumps({"done": True}) + "\n\n"
        except Exception as e:
            yield "data: " + json.dumps({"err": str(e)}) + "\n\n"
    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/fleet")
@require_auth
def api_fleet():
    """Cross-machine roll-up (EROS vitals + ARES/ZEUS backup health), written by
    the ares-fleet.timer collector on the PVE host every 60s. The LXC can't route
    to the peer IPs, so the host probes and drops a file here (same pattern as
    /api/alerts and /api/outreach). age_sec lets the UI grey out on a dead
    collector instead of showing ancient data as live."""
    path = os.path.join(_APP_DIR, "ai_data", "fleet.json")
    try:
        with open(path) as f:
            d = json.load(f)
    except Exception:
        return jsonify({"ok": False, "age_sec": None})
    d["age_sec"] = int(time.time() - d.get("ts", 0))
    d["stale"] = d["age_sec"] > 300
    d["ok"] = True
    return jsonify(d)


@app.route("/api/kg")
@require_auth
def api_kg():
    """Homelab knowledge graph overview for the ZEUS view (read-only)."""
    from system import kg_query
    db = kg_query.db_path()
    if not db:
        return jsonify({"ok": False, "err": "graph not found"}), 404
    try:
        limit = max(10, min(400, int(request.args.get("limit", 150))))
    except ValueError:
        limit = 150
    d = kg_query.overview(db, limit, request.args.get("box"), request.args.get("root"))
    d["ok"] = True
    return jsonify(d)


@app.route("/api/kg/search")
@require_auth
def api_kg_search():
    from system import kg_query
    db = kg_query.db_path()
    if not db:
        return jsonify({"ok": False, "results": []}), 404
    try:
        limit = max(1, min(50, int(request.args.get("limit", 15))))
    except ValueError:
        limit = 15
    r = kg_query.search(db, request.args.get("q", ""), limit)
    r["ok"] = True
    return jsonify(r)


@app.route("/api/kg/node")
@require_auth
def api_kg_node():
    from system import kg_query
    db = kg_query.db_path()
    if not db:
        return jsonify({"ok": False}), 404
    n = kg_query.node(db, request.args.get("id", ""))
    if not n:
        return jsonify({"ok": False}), 404
    n["ok"] = True
    return jsonify(n)


@app.route("/api/kg/children")
@require_auth
def api_kg_children():
    from system import kg_query
    db = kg_query.db_path()
    if not db:
        return jsonify({"ok": False, "nodes": [], "edges": []}), 404
    try:
        limit = max(1, min(500, int(request.args.get("limit", 200))))
    except ValueError:
        limit = 200
    c = kg_query.children(db, request.args.get("id", ""), limit)
    c["ok"] = True
    return jsonify(c)


@app.route("/api/system-info")
@require_auth
def system_info():
    info = get_system_info()
    info["api_usage"] = get_usage_stats()
    return jsonify(info)


@app.route("/api/drives/browse")
@require_auth
def drives_browse():
    """List contents of a folder on a drive."""
    import os as _os
    req_path = request.args.get("path", "")
    allowed_roots = [
        "/srv/dev-disk-by-uuid-27b8f17b-bc24-456f-852c-212358ed968e",
        "/srv/dev-disk-by-uuid-de676cab-cff5-4143-a3fa-174e88f13b4a",
    ]
    real = _os.path.realpath(req_path)
    ok = any(real.startswith(r) for r in allowed_roots)
    if not ok or not _os.path.isdir(real):
        return jsonify({"error": "Invalid path"}), 400
    items = []
    try:
        for name in sorted(_os.listdir(real)):
            full = _os.path.join(real, name)
            if name.startswith('.'):
                continue
            is_dir = _os.path.isdir(full)
            size = ""
            if not is_dir:
                try:
                    b = _os.path.getsize(full)
                    if b >= 1024**3:
                        size = f"{b/1024**3:.1f} GB"
                    elif b >= 1024**2:
                        size = f"{b/1024**2:.1f} MB"
                    elif b >= 1024:
                        size = f"{b/1024:.0f} KB"
                    else:
                        size = f"{b} B"
                except Exception:
                    pass
            items.append({"name": name, "is_dir": is_dir, "size": size})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(items)


@app.route("/api/drives")
@require_auth
def drives_api():
    """Return SMART data for all physical drives."""
    import subprocess as _sp
    info = get_system_info()
    disks = info.get("disks", [])

    scan_map = {
        "sdb": ("sntasmedia", "T9"),
        "sdc": ("sat", "BACKUP"),
    }

    results = []
    for dev, (dtype, name) in scan_map.items():
        entry = {"name": name, "device": "/dev/" + dev, "interface": "USB"}
        disk_info = next((d for d in disks if d["name"] == name or (name == "BACKUP" and d["name"] == "BACKUP")), None)
        if disk_info:
            entry["used"] = disk_info.get("used", "")
            entry["total"] = disk_info.get("total", "")
            entry["percent"] = disk_info.get("percent", 0)
        elif name == "BACKUP":
            import psutil as _psutil
            try:
                u = _psutil.disk_usage("/srv/dev-disk-by-uuid-de676cab-cff5-4143-a3fa-174e88f13b4a")
                entry["used"] = f"{u.used / (1024**3):.1f} GiB"
                entry["total"] = f"{u.total / (1024**3):.1f} GiB"
                entry["percent"] = round(u.percent, 1)
            except Exception:
                pass

        try:
            raw = _sp.check_output(["smartctl", "-a", "/dev/" + dev, "-d", dtype], stderr=_sp.DEVNULL, timeout=10).decode()
        except Exception:
            raw = ""

        entry["health"] = "PASSED" if "PASSED" in raw else "UNKNOWN"

        m = re.search(r"Model (?:Number|Family):\s*(.+)", raw)
        entry["model"] = m.group(1).strip() if m else ""
        if not entry["model"]:
            m = re.search(r"Device Model:\s*(.+)", raw)
            entry["model"] = m.group(1).strip() if m else name

        m = re.search(r"Serial Number:\s*(.+)", raw)
        entry["serial"] = m.group(1).strip() if m else ""

        m = re.search(r"Temperature:\s+(\d+)", raw)
        if not m:
            m = re.search(r"Airflow_Temperature_Cel.*?(\d+)\s*$", raw, re.MULTILINE)
        entry["temp"] = int(m.group(1)) if m else None

        m = re.search(r"Power On Hours:\s+([\d,]+)", raw)
        if not m:
            m = re.search(r"Power_On_Hours.*\s(\d+)\s*$", raw, re.MULTILINE)
        entry["power_on_hours"] = int(m.group(1).replace(",", "")) if m else None

        m = re.search(r"Percentage Used:\s+(\d+)", raw)
        entry["percentage_used"] = int(m.group(1)) if m else None

        m = re.search(r"Data Units Written:\s+([\d,]+)\s+\[([^\]]+)\]", raw)
        entry["data_written"] = m.group(2) if m else None
        if not entry["data_written"]:
            m = re.search(r"Total_LBAs_Written.*?(\d[\d,]*)\s*$", raw, re.MULTILINE)
            if m:
                lbas = int(m.group(1).replace(",", ""))
                tb = lbas * 512 / (1024**4)
                entry["data_written"] = f"{tb:.2f} TB"

        m = re.search(r"Data Units Read:\s+([\d,]+)\s+\[([^\]]+)\]", raw)
        entry["data_read"] = m.group(2) if m else None

        m = re.search(r"Unsafe Shutdowns:\s+([\d,]+)", raw)
        entry["unsafe_shutdowns"] = int(m.group(1).replace(",", "")) if m else None

        m = re.search(r"Reallocated_Sector_Ct.*?(\d+)\s*$", raw, re.MULTILINE)
        entry["reallocated"] = int(m.group(1)) if m else None

        # List folders on this drive
        mount_map = {
            "sdb": "/srv/dev-disk-by-uuid-27b8f17b-bc24-456f-852c-212358ed968e",
            "sdc": "/srv/dev-disk-by-uuid-de676cab-cff5-4143-a3fa-174e88f13b4a",
        }
        mount = mount_map.get(dev, "")
        folders = []
        if mount:
            import os as _os
            try:
                for fname in sorted(_os.listdir(mount)):
                    fpath = _os.path.join(mount, fname)
                    if _os.path.isdir(fpath) and not fname.startswith('.') and fname != 'lost+found':
                        folders.append({"name": fname, "size": ""})
            except Exception:
                pass
        entry["folders"] = folders
        results.append(entry)

    pool = next((d for d in disks if d["name"] == "PROMETHEUS"), None)
    if pool:
        pool_folders = []
        results.insert(0, {
            "name": "PROMETHEUS",
            "device": "mergerfs pool",
            "interface": "MergerFS",
            "model": "Virtual Pool (T9)",
            "used": pool.get("used", ""),
            "total": pool.get("total", ""),
            "percent": pool.get("percent", 0),
            "health": "PASSED",
            "serial": "—",
            "temp": None,
            "power_on_hours": None,
            "percentage_used": None,
            "data_written": None,
            "data_read": None,
            "unsafe_shutdowns": None,
            "reallocated": None,
            "folders": pool_folders,
        })

    return jsonify(results)


@app.route("/api/pve-stats")
@require_auth
def pve_stats_proxy():
    """Proxy PVE stats from the Proxmox host so browser doesn't need direct LAN access."""
    import requests as _req
    try:
        r = _req.get("http://192.168.20.51:9100/api/pve-stats", timeout=5)
        return r.json(), r.status_code
    except Exception:
        return jsonify({"error": "PVE unreachable"}), 502


# ─── Storage breakdown ──────────────────────────────────────────────────────
# What's eating space on each physical drive. For AIRDISK (Proxmox boot SSD)
# we SSH to the host and parse LVM thin-pool usage per-LV, then name each LV
# by the VM/LXC it belongs to. For EVO-970 (the data NVMe) we use the folder
# sizes that system_info already computes.

_storage_cache = {"ts": 0.0, "data": None}

@app.route("/api/storage-breakdown")
@require_auth
def storage_breakdown():
    import subprocess, time as _t, re

    now = _t.time()
    if _storage_cache["data"] and now - _storage_cache["ts"] < 30:
        return jsonify(_storage_cache["data"])

    # LV inventory from Proxmox host
    lvs_out = ""
    vms_out = ""
    cts_out = ""
    try:
        proc = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=2", "-o", "StrictHostKeyChecking=no",
             "-o", "BatchMode=yes", "-o", "LogLevel=ERROR",
             "root@192.168.20.51",
             "lvs --noheadings --units b --nosuffix -o lv_name,lv_size,data_percent pve 2>/dev/null; echo '===VMS==='; "
             "qm list 2>/dev/null | awk 'NR>1 {print $1\"|\"$2\"|\"$3}'; echo '===CTS==='; "
             "pct list 2>/dev/null | awk 'NR>1 {print $1\"|\"$3\"|\"$2}'; echo '===SDA==='; "
             "lsblk -b -d -n -o SIZE /dev/sda 2>/dev/null"],
            capture_output=True, text=True, timeout=4,
        )
        out = proc.stdout
        sections = re.split(r"===(?:VMS|CTS|SDA)===", out)
        lvs_out = sections[0] if len(sections) > 0 else ""
        vms_out = sections[1] if len(sections) > 1 else ""
        cts_out = sections[2] if len(sections) > 2 else ""
        sda_size = int(sections[3].strip()) if len(sections) > 3 and sections[3].strip().isdigit() else 0
    except Exception as _e:
        sda_size = 512110190592  # fallback: 512GB

    # Map vmid → name
    name_map = {}
    for line in vms_out.splitlines():
        parts = line.strip().split("|")
        if len(parts) >= 2 and parts[0].isdigit():
            name_map[parts[0]] = parts[1]
    for line in cts_out.splitlines():
        parts = line.strip().split("|")
        if len(parts) >= 2 and parts[0].isdigit():
            name_map[parts[0]] = parts[1]

    # Parse LVs
    consumers = []
    pool_used_bytes = 0
    pool_size_bytes = 0
    root_size = 0
    swap_size = 0
    for line in lvs_out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[0]
        try:
            size_b = int(parts[1])
        except ValueError:
            continue
        data_pct = 0.0
        if len(parts) >= 3 and parts[2] and parts[2] != "-":
            try: data_pct = float(parts[2])
            except ValueError: data_pct = 0.0

        if name == "data":
            pool_size_bytes = size_b
            pool_used_bytes = int(size_b * data_pct / 100)
        elif name == "root":
            root_size = size_b
        elif name == "swap":
            swap_size = size_b
        elif name.startswith("vm-") and "-disk-" in name:
            # vm-200-disk-2 → vmid 200
            m = re.match(r"vm-(\d+)-disk-\d+", name)
            if not m: continue
            vmid = m.group(1)
            actual = int(size_b * data_pct / 100)
            if actual < 4 * 1024 * 1024:
                continue  # ignore EFI/swap/cloudinit stubs
            consumers.append({
                "id": name,
                "vmid": vmid,
                "label": name_map.get(vmid, f"VM {vmid}"),
                "kind": "lxc" if vmid in cts_out else "vm",
                "used_bytes": actual,
                "allocated_bytes": size_b,
                "data_percent": data_pct,
            })

    # Aggregate consumers by vmid (sum disks) so vm-200-disk-2 and vm-200-disk-1 become one bar
    by_vmid = {}
    for c in consumers:
        k = c["vmid"]
        if k not in by_vmid:
            by_vmid[k] = {
                "name": c["label"],
                "kind": c["kind"],
                "used_bytes": 0,
                "allocated_bytes": 0,
            }
        by_vmid[k]["used_bytes"] += c["used_bytes"]
        by_vmid[k]["allocated_bytes"] += c["allocated_bytes"]

    # Try to measure pve-root actual usage (df /) via SSH
    root_used_bytes = 0
    try:
        proc = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=2", "-o", "StrictHostKeyChecking=no",
             "-o", "BatchMode=yes", "-o", "LogLevel=ERROR",
             "root@192.168.20.51",
             "df -B1 --output=used / | tail -1"],
            capture_output=True, text=True, timeout=3,
        )
        if proc.returncode == 0:
            root_used_bytes = int(proc.stdout.strip() or "0")
    except Exception:
        pass

    # Build the AIRDISK segments
    airdisk_segments = []
    total_accounted = 0
    # Order: big → small is nicer in the bar
    sorted_vms = sorted(by_vmid.items(), key=lambda x: -x[1]["used_bytes"])
    # ARES heat-map palette: yellow → orange → red → deep red.
    # Biggest/hottest at the top of the spectrum, smaller/quieter at the bottom.
    palette_vm = {
        "win11-gaming": "#fbbf24",   # amber 400 — brightest (biggest consumer)
        "ollama-llm":   "#f97316",   # orange 500 — active LLM work
        "ares":         "#ef4444",   # red 400 — the ARES container itself
    }
    for vmid, v in sorted_vms:
        airdisk_segments.append({
            "name": v["name"],
            "category": v["kind"].upper(),
            "bytes": v["used_bytes"],
            "allocated": v["allocated_bytes"],
            "color": palette_vm.get(v["name"], "#f59e0b"),
            "detail": f"{v['kind']} {vmid} · {int(v['used_bytes']/1024/1024/1024)} GiB of {int(v['allocated_bytes']/1024/1024/1024)} GiB allocated",
        })
        total_accounted += v["used_bytes"]

    if root_used_bytes > 0:
        airdisk_segments.append({
            "name": "pve-root",
            "category": "OS",
            "bytes": root_used_bytes,
            "color": "#b91c1c",  # red 600 — the quieter OS layer
            "detail": f"Proxmox root filesystem · {int(root_used_bytes/1024/1024/1024)} GiB used of {int(root_size/1024/1024/1024)} GiB",
        })
        total_accounted += root_used_bytes
    if swap_size > 0:
        airdisk_segments.append({
            "name": "swap",
            "category": "OS",
            "bytes": swap_size,
            "color": "#7f1d1d",  # red 800 — dormant, deep
            "detail": f"Linux swap · {int(swap_size/1024/1024/1024)} GiB",
        })
        total_accounted += swap_size

    free_bytes = max(0, sda_size - total_accounted)
    airdisk = {
        "name": "AIRDISK",
        "tag": "Samsung T7 · LVM-thin",
        "total_bytes": sda_size,
        "used_bytes": total_accounted,
        "segments": airdisk_segments,
        "free_bytes": free_bytes,
    }

    # ── EVO-970 breakdown from folder sizes ──
    from system.system_info import get_system_info
    info = get_system_info()
    evo_total = 0
    evo_used = 0
    evo_pool_mount = None
    for d in info.get("disks", []):
        if d["name"] == "EVO-970":
            # Re-parse "931.5 GiB" back to bytes for precision
            def _parse(s):
                try:
                    n, unit = s.split()
                    n = float(n)
                    mult = {"KiB":1024, "MiB":1024**2, "GiB":1024**3, "TiB":1024**4}.get(unit, 1)
                    return int(n * mult)
                except Exception:
                    return 0
            evo_total = _parse(d["total"])
            evo_used = _parse(d["used"])
            evo_pool_mount = d["mount"]

    # ARES heat-map for folders: yellow → orange → red → deep red.
    # PHOTOS is the big star, gets the hottest tone. MORDOR is dark, matches its name.
    folder_palette = {
        "PHOTOS":     "#fbbf24",  # amber 400
        "PROJECTS":   "#f97316",  # orange 500
        "PROMETHEON": "#ea580c",  # orange 600
        "PERSONAL":   "#ef4444",  # red 400 (ARES primary)
        "WORK":       "#b91c1c",  # red 700
        "MORDOR":     "#7f1d1d",  # red 800 — dark, on-theme
    }
    evo_segments = []
    total_folders = 0
    for f in info.get("folders", []):
        name = f["name"]
        size = int(f.get("size", 0))
        if size < 1024 * 1024:  # skip <1 MB
            continue
        evo_segments.append({
            "name": name,
            "category": "DATA",
            "bytes": size,
            "color": folder_palette.get(name, "#8ba3c0"),
            "detail": f"{name} · {f.get('display', '?')}",
        })
        total_folders += size
    # "Other" = used on disk but not in top-level folders we measured
    other = max(0, evo_used - total_folders)
    if other > 1024 * 1024 * 100:
        evo_segments.append({
            "name": "fs overhead",
            "category": "ext4",
            "bytes": other,
            "color": "#2d1010",
            "detail": "ext4 journal, inodes, block-rounding on many small files. Not user data — not cleanable.",
        })

    evo = {
        "name": "EVO-970",
        "tag": "Samsung 970 EVO Plus · NVMe",
        "total_bytes": evo_total,
        "used_bytes": evo_used,
        "segments": evo_segments,
        "free_bytes": max(0, evo_total - evo_used),
        "mount": evo_pool_mount,
    }

    payload = {"drives": [airdisk, evo]}
    _storage_cache["data"] = payload
    _storage_cache["ts"] = now
    return jsonify(payload)


def _oled_solo_after_start():
    """After VM 200 boots, disable the Virtual Display Driver so the desk OLED
    is the sole display (user wants one screen, not the VDD phantom, when they
    tap in via VNC/Moonlight). Reuses the VM's existing C:\\gamemode\\vdd-disable.ps1.

    GameModeDisplayRest re-enables the VDD on its LogonTrigger, so we wait for
    boot+logon to settle, then disable last to win the race. Disable-PnpDevice
    is a global op, so guest-exec (session 0) is enough; no trampoline needed.
    Runs in a daemon thread."""
    import subprocess as _sp
    host = "root@192.168.20.51"
    ssh_base = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5", host]
    ping = "qm agent 200 ping"
    disable = ("qm guest exec 200 --timeout 20 -- powershell -NoProfile "
               "-ExecutionPolicy Bypass -File C:\\gamemode\\vdd-disable.ps1")
    up = False
    for _ in range(40):                       # ceiling ~3.5 min waiting for guest agent
        try:
            _sp.check_output(ssh_base + [ping], timeout=8, stderr=_sp.DEVNULL)
            up = True
            break
        except Exception:
            time.sleep(5)
    if not up:
        return
    # ponytail: fixed 45s lets logon + GameModeDisplayRest finish before we
    # disable last. If the re-enable ever wins, poll its LastRunTime instead.
    time.sleep(45)
    for _ in range(2):                        # two idempotent fires, 15s apart
        try:
            _sp.run(ssh_base + [disable], timeout=30,
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        except Exception:
            pass
        time.sleep(15)


@app.route("/api/vm/<action>", methods=["POST"])
@require_auth
def vm_control(action):
    """Start or stop the Windows VM (ID 200) on PVE host."""
    import subprocess as _sp
    if action not in ("start", "stop", "status"):
        return jsonify({"error": "Invalid action"}), 400
    try:
        if action == "status":
            out = _sp.check_output(
                ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
                 "root@192.168.20.51", "qm status 200"],
                timeout=10, stderr=_sp.DEVNULL
            ).decode().strip()
            running = "running" in out
            streaming_ready = False
            gpu_home = None
            if running:
                # Sunshine answers 47984 only once Windows + the service are up.
                import socket as _socket
                try:
                    with _socket.create_connection(("192.168.20.215", 47984), timeout=1):
                        streaming_ready = True
                except OSError:
                    streaming_ready = False
            else:
                # Post-reclaim health: flag gone AND the 3080 visible in this CT.
                if os.path.exists(GPU_LOAN_FLAG):
                    gpu_home = False
                else:
                    try:
                        rc = _sp.run(["nvidia-smi", "-L"], capture_output=True,
                                     timeout=5).returncode
                        gpu_home = (rc == 0)
                    except Exception:
                        gpu_home = False
            return jsonify({"vm": "win11-gaming", "running": running, "raw": out,
                            "streaming_ready": streaming_ready, "gpu_home": gpu_home})
        else:
            # The GPU-swap hookscript restarts THIS service during both
            # pre-start and post-stop (flag+restart protocol), which kills a
            # foreground ssh and interrupts qm mid-handoff ("received
            # interrupt / broken pipe"). Detach qm on the host so the
            # boot/stop survives our own restart; the frontend already polls
            # /api/vm/status for the outcome.
            cmd = ("nohup qm " + action + " 200 >>/var/log/qm-" + action +
                   "-200.log 2>&1 & echo detached")
            _sp.check_output(
                ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
                 "root@192.168.20.51", cmd],
                timeout=15, stderr=_sp.DEVNULL
            )
            if action == "start":
                # Once Windows is up, drop the VDD so the OLED is the only screen.
                threading.Thread(target=_oled_solo_after_start, daemon=True).start()
            return jsonify({"ok": True, "action": action, "detached": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/files")
@require_auth
def files_view():
    return render_template("files.html", boot=get_system_info())


@app.route("/api/files/list")
@require_auth
def files_list():
    rel = request.args.get("path", "")
    ap = files_api.safe_resolve(rel)
    if not ap or not os.path.isdir(ap):
        abort(404)                                    # identical 404 for missing/forbidden
    return jsonify(files_api.list_dir(ap))


@app.route("/api/files/raw")
@require_auth
def files_raw():
    rel = request.args.get("path", "")
    force_dl = request.args.get("dl") == "1"
    ap = files_api.safe_resolve(rel)
    if not ap or not os.path.isfile(ap):
        abort(404)
    try:
        fd = files_api.open_checked(ap)               # reject symlink-swap / non-regular
        os.close(fd)
    except OSError:
        abort(404)
    mimetype, as_attachment = files_api.serve_mode(os.path.basename(ap), force_dl)
    resp = send_file(ap, mimetype=mimetype, as_attachment=as_attachment,
                     conditional=True, download_name=os.path.basename(ap))
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "sandbox"
    return resp


@app.route("/api/files/thumb")
@require_auth
def files_thumb():
    rel = request.args.get("path", "")
    ap = files_api.safe_resolve(rel)
    if not ap or not os.path.isfile(ap):
        abort(404)
    cache = files_api.make_thumb(ap)
    if not cache:
        abort(404)
    resp = send_file(cache, mimetype="image/jpeg", conditional=True, max_age=86400)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _ollama_complete(system_prompt, user_prompt):
    """One-shot grounded Ollama completion (no tool-calling). Returns text."""
    import requests as _rq
    from ai.llm_interface import OLLAMA_HOST, OLLAMA_MODEL
    try:
        r = _rq.post(OLLAMA_HOST + "/api/chat", timeout=60, json={
            "model": OLLAMA_MODEL, "stream": False,
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": user_prompt}]})
        return (r.json().get("message", {}).get("content") or "").strip() or "(no response)"
    except Exception as e:
        return "Assistant unavailable (%s)." % e


def _peek_content(rel):
    """Small bounded text peek at the top match so the assistant can actually
    explain it (dir -> its README, else a listing; small text/md -> head). ~2KB."""
    ap = files_api.safe_resolve(rel or "")
    if not ap:
        return None
    try:
        if os.path.isdir(ap):
            for cand in ("README.md", "readme.md", "README", "README.txt", "about.md"):
                p = os.path.join(ap, cand)
                if os.path.isfile(p):
                    with open(p, "r", errors="ignore") as fh:
                        return "%s/%s:\n%s" % (rel, cand, fh.read(2000))
            names = [e.name for e in os.scandir(ap) if not e.name.startswith(".")]
            return "%s/ (folder) contains: %s" % (rel, ", ".join(names[:25]))
        if files_api.kind_for(os.path.basename(ap)) in ("text", "md") \
                and os.path.getsize(ap) < 100_000:
            with open(ap, "r", errors="ignore") as fh:
                return "%s:\n%s" % (rel, fh.read(2000))
    except OSError:
        return None
    return None


@app.route("/api/files/reindex", methods=["POST"])
@require_auth
def files_reindex():
    started = files_index.start_reindex()
    return jsonify({"started": started, "status": files_index.index_status()})


@app.route("/api/files/index-status")
@require_auth
def files_index_status():
    return jsonify(files_index.index_status())


@app.route("/api/files/ask", methods=["POST"])
@require_auth
def files_ask():
    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "empty query"}), 400
    cands = files_index.search(query, limit=40)
    passages = files_index.content_search(query, limit=6)          # keyword inside-doc
    sem = files_index.semantic_search(query, k=6)                  # meaning-based
    listing = "\n".join(
        "- %s [%s] %s" % (c["path"], c["kind"],
                          time.strftime("%Y-%m-%d", time.localtime(c["mtime"])))
        for c in cands[:25]) or "(no candidates found)"
    peek = _peek_content(cands[0]["path"]) if cands else None
    sys_p = ("You are a file assistant for a personal home server. Answer DIRECTLY and "
             "briefly. PASSAGES (both 'FROM DOCUMENTS' keyword hits and 'RELEVANT "
             "PASSAGES (semantic)' meaning-based hits), when present, are real excerpts "
             "from the user's files — use them to answer and cite the doc path, quoting "
             "briefly. "
             "CONTENT OF TOP MATCH (when present) is the contents of the most relevant "
             "file/folder — use it to explain what that item IS, EVEN IF the user's exact "
             "word does not appear in it. Refer to paths from CANDIDATES/PASSAGES only; "
             "never invent one. For 'newest/latest' candidates are sorted newest-first. "
             "Only if there is truly no relevant match, say so and name the closest folder "
             "— never fabricate.")
    user_p = "CANDIDATES (path [kind] date):\n%s\n" % listing
    if passages:
        user_p += "\nPASSAGES FROM DOCUMENTS:\n%s\n" % "\n".join(
            "%s: %s" % (p["path"], p["snippet"][:400]) for p in passages)
    if sem:
        user_p += "\nRELEVANT PASSAGES (semantic):\n%s\n" % "\n".join(
            "%s: %s" % (s["path"], " ".join(s["chunk"].split())[:400]) for s in sem)
    if peek:
        user_p += "\nCONTENT OF TOP MATCH:\n%s\n" % peek[:2200]
    user_p += "\nQUESTION: %s" % query
    answer = _ollama_complete(sys_p, user_p)
    # small models sometimes echo a prompt label as the first line — strip it.
    for _lbl in ("RELEVANT PASSAGES", "PASSAGES FROM DOCUMENTS", "CONTENT OF TOP MATCH", "CANDIDATES"):
        while answer.lstrip().upper().startswith(_lbl):
            answer = answer.split("\n", 1)[1] if "\n" in answer else ""
    answer = answer.strip()
    # chips: doc-content hits (keyword + semantic) first, then name matches, deduped
    seen, locs = set(), []
    for p in passages:
        if p["path"] not in seen:
            seen.add(p["path"])
            locs.append({"name": p["path"].rsplit("/", 1)[-1], "path": p["path"],
                         "kind": p["kind"], "size": None, "mtime": 0, "via": "content"})
    for s in sem:
        if s["path"] not in seen:
            seen.add(s["path"])
            locs.append({"name": s["path"].rsplit("/", 1)[-1], "path": s["path"],
                         "kind": files_api.kind_for(s["path"].rsplit("/", 1)[-1]),
                         "size": None, "mtime": 0, "via": "semantic"})
    for c in cands:
        if c["path"] not in seen:
            seen.add(c["path"])
            locs.append(c)
    # Show ONE chip: the file the answer actually cites (validated against the real
    # candidate set, so never hallucinated); else fall back to the top match.
    cited = next((L for L in locs if L["path"] and L["path"] in answer), None)
    final = [cited] if cited else locs[:1]
    return jsonify({"answer": answer, "locations": final,
                    "indexed": files_index.index_status().get("count", 0)})


@app.route("/api/vm/vnc-ready", methods=["POST"])
@require_auth
def vm_vnc_ready():
    """Probe whether the Windows desktop is actually reachable.

    `qm status` reports "running" the instant QEMU starts — long before
    Windows boots and TightVNC begins accepting connections. The noVNC
    iframe is useless until the upstream VNC server (the same host:port
    websockify forwards 6080 to) is live, so the frontend gates the
    desktop on this TCP probe instead of on `qm status`.
    """
    import socket as _socket
    host = os.getenv("WINDOWS_VNC_TARGET_HOST", "192.168.20.215")
    try:
        port = int(os.getenv("WINDOWS_VNC_TARGET_PORT", "5900"))
    except (TypeError, ValueError):
        port = 5900
    ready = False
    try:
        with _socket.create_connection((host, port), timeout=2):
            ready = True
    except OSError:
        ready = False
    return jsonify({"ready": ready, "target": host + ":" + str(port)})


@app.route("/api/mordor", methods=["POST"])
@require_auth
def mordor_toggle():
    action = request.json.get("action") if request.is_json else None
    mordor_dir = os.path.join(
        "/srv/mergerfs/PROMETHEUS" if not sys.platform == "darwin" else "/Volumes/PROMETHEUS",
        "MORDOR"
    )
    manager = os.path.join(mordor_dir, "server_manager.sh")
    if action in ("start", "stop"):
        # Fix ownership first (ARES runs as root)
        subprocess.run(["chown", "-R", "zain:zain", mordor_dir], timeout=30)
        # Run as zain
        cmd = ["sudo", "-u", "zain", "bash", manager, action]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=35, cwd=mordor_dir)
            return jsonify({"ok": True, "status": action + "ing", "output": result.stdout.strip()})
        except subprocess.TimeoutExpired:
            return jsonify({"ok": True, "status": action + "ing", "output": "timed out waiting"})
    return jsonify({"ok": False, "error": "invalid action"}), 400


# ─── Public Minecraft Control (for GitHub Pages remote) ───

@app.route("/api/minecraft", methods=["POST", "OPTIONS"])
def minecraft_public():
    """Public Minecraft server control — open access, CORS-restricted."""
    origin = request.headers.get("Origin", "")
    allowed_origins = [
        "https://mordor.vercel.app",
        "https://zainkhatri.github.io",
        "http://localhost",
        "http://127.0.0.1",
    ]
    is_allowed = origin in {"https://mordor.vercel.app", "https://zainkhatri.github.io"}
    cors_origin = origin if is_allowed else allowed_origins[0]
    cors_headers = {
        "Access-Control-Allow-Origin": cors_origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }

    if request.method == "OPTIONS":
        return ("", 204, cors_headers)

    data = request.get_json(silent=True) or {}
    action = str(data.get("action", "")).strip().lower()

    if action not in ("start", "stop", "status"):
        return (jsonify({"error": "Invalid action"}), 400, cors_headers)

    from ai.safe_executor import minecraft_server
    mc_action = {"start": "on", "stop": "off", "status": "status"}[action]
    result = minecraft_server(mc_action)
    msg = result["stdout"] or result["stderr"]

    # Parse running state
    running = None
    if "already running" in msg.lower() or "is running" in msg.lower() or "rising" in msg.lower():
        running = True
    elif "stopped" in msg.lower() or "offline" in msg.lower() or "not running" in msg.lower() or "fallen" in msg.lower():
        running = False

    return (jsonify({
        "ok": True,
        "message": msg,
        "running": running,
        "address": "safety-melbourne.gl.joinmc.link",
    }), 200, cors_headers)


@app.route("/api/minecraft/players", methods=["POST", "OPTIONS"])
def minecraft_players():
    """Check online player count via mcstatus query."""
    origin = request.headers.get("Origin", "")
    allowed_origins = [
        "https://mordor.vercel.app",
        "https://zainkhatri.github.io",
        "http://localhost",
        "http://127.0.0.1",
    ]
    is_allowed = origin in {"https://mordor.vercel.app", "https://zainkhatri.github.io"}
    cors_origin = origin if is_allowed else allowed_origins[0]
    cors_headers = {
        "Access-Control-Allow-Origin": cors_origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }
    if request.method == "OPTIONS":
        return ("", 204, cors_headers)

    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(("127.0.0.1", 25565))
        # MC server list ping (modern protocol)
        import struct
        # Handshake packet
        host = b"127.0.0.1"
        port = 25565
        handshake = b"\x00"  # packet id
        handshake += b"\x05"  # protocol version (5 = 1.7.x)
        handshake += bytes([len(host)]) + host
        handshake += struct.pack(">H", port)
        handshake += b"\x01"  # next state = status
        packet = bytes([len(handshake)]) + handshake
        sock.sendall(packet)
        # Status request
        sock.sendall(b"\x01\x00")
        # Read response
        raw = sock.recv(4096)
        sock.close()
        # Parse: varint length, packet id 0x00, varint string length, JSON string
        idx = 0
        # skip packet length varint
        while idx < len(raw) and raw[idx] & 0x80:
            idx += 1
        idx += 1
        # skip packet id varint
        while idx < len(raw) and raw[idx] & 0x80:
            idx += 1
        idx += 1
        # read string length varint
        str_len = 0
        shift = 0
        while idx < len(raw):
            b = raw[idx]
            str_len |= (b & 0x7F) << shift
            idx += 1
            shift += 7
            if not (b & 0x80):
                break
        json_str = raw[idx:idx+str_len].decode("utf-8", errors="replace")
        status = json.loads(json_str)
        players = status.get("players", {})
        return (jsonify({
            "online": players.get("online", 0),
            "max": players.get("max", 0),
            "names": [p.get("name", "?") for p in players.get("sample", [])],
        }), 200, cors_headers)
    except Exception:
        return (jsonify({"online": -1, "max": 0, "names": [], "error": "Server unreachable"}), 200, cors_headers)


@app.route("/api/chat", methods=["POST"])
@require_auth
def chat_endpoint():
    data = request.json
    message = data.get("message", "").strip()
    session_id = data.get("session_id", "default")
    image_b64 = data.get("image", "")
    image_mime = data.get("image_mime", "image/jpeg")
    persona = (data.get("persona") or "").strip()
    style   = (data.get("style") or "").strip()

    if not message and not image_b64:
        return jsonify({"error": "Empty message"}), 400

    # Prepend any persona / style nudge to the message invisibly so the model
    # sees it as an updated directive without us mutating the stored system
    # prompt. This keeps per-turn overrides lightweight.
    _prefix_bits = []
    if persona: _prefix_bits.append(f"[persona active] {persona}")
    if style:   _prefix_bits.append(f"[style] {style}")
    if _prefix_bits and message:
        message = "\n".join(_prefix_bits) + "\n\n" + message

    # ─── Direct commands (bypass AI) ───
    from ai.safe_executor import minecraft_server
    cmd_lower = message.lower().strip()
    if cmd_lower in ("server on", "server off", "server status"):
        action = cmd_lower.split()[-1]
        result = minecraft_server(action)
        reply = result["stdout"] or result["stderr"]
        def direct_reply():
            yield f"data: {json.dumps({'type': 'text', 'content': reply})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return Response(
            stream_with_context(direct_reply()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if session_id not in conversations:
        conversations[session_id] = []

    history = conversations[session_id]

    def generate():
        display = session_display.setdefault(session_id, [])
        display.append({"role": "user", "text": message or "[image]"})
        full_text = ""

        # ARES uses the local Ollama on VM 300 by default. If ANTHROPIC_API_KEY is set,
        # Claude takes over (useful when the 3080 is claimed by Windows).
        if ANTHROPIC_API_KEY:
            stream = claude_interface.chat_stream(
                message, history, ANTHROPIC_API_KEY,
                image_b64 or None, image_mime
            )
            backend = "claude"
        else:
            stream = llm_interface.chat_stream(message, history)
            backend = "ollama"

        for event in stream:
            yield f"data: {json.dumps(event)}\n\n"
            if event["type"] == "text":
                full_text += event["content"]
            elif event["type"] == "done":
                if full_text:
                    display.append({"role": "assistant", "text": full_text})
                _save_session(session_id, history, backend, display)

        # Ollama backend doesn't emit its own 'done' — emit one so the client closes cleanly.
        if backend == "ollama":
            if full_text:
                display.append({"role": "assistant", "text": full_text})
            _save_session(session_id, history, backend, display)
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/sessions")
@require_auth
def api_sessions_list():
    return jsonify(_list_sessions())


@app.route("/api/sessions/<session_id>")
@require_auth
def api_session_load(session_id):
    data = _load_session(session_id)
    if data is None:
        return jsonify({"error": "Session not found"}), 404
    # Restore into memory so subsequent messages use this history
    conversations[session_id] = data.get("history", [])
    session_display[session_id] = data.get("display", [])
    return jsonify(data)


TRASH_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".ares-trash")


@app.route("/api/trash")
@require_auth
def trash_list():
    return jsonify(list_trash())


@app.route("/api/trash/preview/<trash_name>")
@require_auth
def trash_preview(trash_name):
    """Serve a trashed file for thumbnail preview."""
    safe_name = secure_filename(trash_name)
    filepath = os.path.join(TRASH_DIR, safe_name)
    if not os.path.isfile(filepath):
        abort(404)
    return send_file(filepath, conditional=True)


@app.route("/api/trash/restore", methods=["POST"])
@require_auth
def trash_restore():
    data = request.json
    trash_name = data.get("trash_name", "")
    if not trash_name:
        return jsonify({"error": "No trash_name provided"}), 400
    result = restore_trash(trash_name)
    return jsonify(result)


@app.route("/api/photos/trash", methods=["POST"])
@require_auth
def trash_photo():
    """Trash a single photo by its hash (thumbnail filename without .jpg)."""
    data = request.json or {}
    h = data.get("hash", "")
    if not h:
        return jsonify({"error": "No hash provided"}), 400
    name = h + ".jpg" if not h.endswith(".jpg") else h
    orig_path = _hash_to_path.get(name)
    if not orig_path:
        return jsonify({"error": "Photo not found"}), 404
    result = trash_file(orig_path)
    if result.get("success"):
        # Invalidate caches so the photo disappears
        _photo_cache["data"] = None
        _month_cache["data"] = None
        _month_json_cache["data"] = None
        # carry the live thumb so the Recycle Bin grid is instant (best-effort)
        try:
            import shutil
            tn = result.get("trash_name")
            live_thumb = os.path.join(_THUMB_DIR, name)  # name == "<hash>.jpg"
            if tn and os.path.exists(live_thumb):
                dst_dir = os.path.join(str(_TRASH_DIR), "_thumbs")
                os.makedirs(dst_dir, exist_ok=True)
                shutil.copy2(live_thumb, os.path.join(dst_dir, tn + ".jpg"))
        except Exception:
            pass  # non-fatal — the thumb route regenerates on demand
    return jsonify(result)


# ─── Photo Gallery ───


import photo_db


def _save_photo_index(items, force=False):
    """Persist the photo index via photo_db (SQLite, single transaction).

    photo_db.save_items carries the >50%-shrink guard that would have caught
    the Jun 2026 dedup run clobbering 46,906 items down to 5.
    """
    photo_db.save_items(items, force=force)
    # Bust cache
    _photo_cache["data"] = None; _photo_cache["mtime"] = 0
_photo_cache = {"data": None, "mtime": 0}
_summary_cache = {"data": None, "mtime": 0}
_month_cache = {"data": None, "mtime": 0}
_month_json_cache = {"data": None, "mtime": 0}


_NATKEY_RE = re.compile(r"(\d+)")

def _photo_sort_key(item):
    # Primary: newer date first. Tiebreaker: filename in natural (numeric-aware) order,
    # so IMG_2584 precedes IMG_2590 when they share an mtime.
    fn = item.get("path", "").rsplit("/", 1)[-1]
    parts = _NATKEY_RE.split(fn)
    natural = tuple((int(p), "") if p.isdigit() else (0, p) for p in parts)
    return (-item.get("date", 0), natural)


def load_photo_index():
    """Load photo index from photo_db with a version-stamp cache."""
    mtime = photo_db.version()
    if not mtime:
        return []
    if _photo_cache["data"] is not None and _photo_cache["mtime"] == mtime:
        return _photo_cache["data"]
    try:
        data = photo_db.load_items()
    except Exception as e:
        app.logger.error(f"[index] photo_index.db unreadable: {e} — serving cached")
        return _photo_cache["data"] or []
    data.sort(key=_photo_sort_key)
    _photo_cache["data"] = data
    _photo_cache["mtime"] = mtime
    # Rebuild reverse hash map for on-demand thumb generation
    threading.Thread(target=_build_hash_index, args=(_photo_cache["data"],), daemon=True).start()
    _month_cache["data"] = None
    _month_json_cache["data"] = None
    return _photo_cache["data"]


def load_month_index():
    """Return dict of month_key -> [items], cached alongside photo index."""
    mtime = photo_db.version()
    if not mtime:
        return {}
    if _month_cache["data"] is not None and _month_cache["mtime"] == mtime:
        return _month_cache["data"]
    items = load_photo_index()
    by_month = {}
    # load_photo_index already returns items sorted by (-date, natural filename);
    # preserve that order so same-mtime photos stay in IMG_#### sequence.
    for item in items:
        try:
            dt = datetime.fromtimestamp(item.get("date", 0), tz=_GALLERY_TZ)
            key = dt.strftime("%Y-%m")
        except Exception:
            continue
        if key not in by_month:
            by_month[key] = []
        by_month[key].append(item)
    _month_cache["data"] = by_month
    _month_cache["mtime"] = mtime
    _month_json_cache["data"] = None  # invalidate serialized cache
    return by_month


# Names hardcoded out of every photo surface (People, summary, month grids,
# search). Lowercased; matched case-insensitively against cluster.name.
# Adding/removing names here is the only knob — no UI on purpose.
_HARDCODED_HIDDEN_NAMES = {"goblin", "amnahraw"}


def _is_cluster_hidden(c):
    if c.get("hidden"):
        return True
    name = (c.get("name") or "").strip().lower()
    return bool(name) and name in _HARDCODED_HIDDEN_NAMES


def _get_hidden_hashes():
    """Return set of photo hashes belonging to hidden face clusters
    (either flagged via cluster.hidden or matched against the hardcoded
    name blocklist)."""
    clusters = _ai.get("face_clusters")
    if not clusters:
        return set()
    hidden = set()
    for c in clusters.values():
        if _is_cluster_hidden(c):
            hidden.update(c.get("photo_hashes", []))
    return hidden


def _get_screenshot_hashes():
    """Return set of photo hashes classified as screenshots/documents."""
    return _ai.get("screenshot_hashes") or set()


_dup_hashes_mtime = 0.0


def _get_duplicate_hashes():
    """Return set of photo hashes that are duplicates of a kept canonical copy.

    Reloads from disk when duplicate_hashes.json changes so the running
    process picks up new entries without a restart.
    """
    global _dup_hashes_mtime
    dup_path = os.path.join(_AI_DIR, "duplicate_hashes.json")
    try:
        mtime = os.path.getmtime(dup_path)
    except OSError:
        return _ai.get("duplicate_hashes") or set()
    if mtime != _dup_hashes_mtime:
        try:
            with open(dup_path) as _f:
                _ai["duplicate_hashes"] = set(json.load(_f))
            _dup_hashes_mtime = mtime
        except (OSError, ValueError):
            pass
    return _ai.get("duplicate_hashes") or set()


# ═══════════════════════════════════════════════════════════════════════════════
# MY EYES ONLY VAULT  (v2 — originals move to PHOTOS/.vault/)
# Architecture:
#   ai_data/vault.json      — {"items": {"<thumb_key>": {<full index entry>}, ...}}
#                             v1 compat: if "items" is a list, treated as keys-only
#   ai_data/vault_auth.json — {"salt": "<hex>", "hash": "<hex>", "fails": N,
#                               "lockout_until": 0.0}
#   Original files move:  PHOTOS_ROOT/<rel> → PHOTOS_ROOT/.vault/<rel>
#   Vault thumbs live in: vault_thumbs*/  vault_video_cache/  vault_hls/
#     (app-dir, NOT Caddy-served)
#   photo_index.json: entry REMOVED on vault, RESTORED on unvault.
#   Every listing endpoint excludes vault hashes via _get_vault_hashes().
# ═══════════════════════════════════════════════════════════════════════════════
import hashlib as _vault_hashlib
import hmac as _vault_hmac
from photos.photo_scanner import PHOTOS_ROOT as PHOTOS_ROOT

_VAULT_AUTH_PATH = os.path.join(_APP_DIR, "ai_data", "vault_auth.json")
_VAULT_PATH      = os.path.join(_APP_DIR, "ai_data", "vault.json")

# Original files vault dir — dot-dir inside PHOTOS_ROOT so:
#  (a) invisible to SMB/Finder  (b) stays on same fs → atomic rename
#  (c) inside PHOTOS tree → nightly rsync to ZEUS still backs it up
_VAULT_ORIGINALS_DIR = os.path.join(PHOTOS_ROOT, ".vault")
try:
    os.makedirs(_VAULT_ORIGINALS_DIR, exist_ok=True)
except OSError:
    # ponytail: non-ARES deployments (e.g. ZEUS, photos:0) have no writable
    # PHOTOS_ROOT — vault is unused there, so don't crash import. Vault ops on
    # ARES still create/verify this dir on first use.
    pass

# Vault thumb directories (NOT under static/ — Caddy cannot serve these)
_VAULT_THUMB_DIR      = os.path.join(_APP_DIR, "vault_thumbs")
_VAULT_THUMB_HQ_DIR   = os.path.join(_APP_DIR, "vault_thumbs_hq")
_VAULT_THUMB_PRV_DIR  = os.path.join(_APP_DIR, "vault_thumbs_preview")
_VAULT_THUMB_MAX_DIR  = os.path.join(_APP_DIR, "vault_thumbs_max")
_VAULT_VIDEO_DIR      = os.path.join(_APP_DIR, "vault_video_cache")
_VAULT_HLS_DIR        = os.path.join(_APP_DIR, "vault_hls")

for _vd in (_VAULT_THUMB_DIR, _VAULT_THUMB_HQ_DIR, _VAULT_THUMB_PRV_DIR,
            _VAULT_THUMB_MAX_DIR, _VAULT_VIDEO_DIR, _VAULT_HLS_DIR):
    os.makedirs(_vd, exist_ok=True)

_VAULT_PIN_FAILS    = 5   # max consecutive failures before lockout
_VAULT_LOCKOUT_SECS = 60  # lockout duration
_VAULT_SESSION_IDLE = 300 # 5-minute inactivity auto-lock (per-open; close always locks)

_vault_state = {"items": {}}  # thumb_key → full index entry dict
_vault_state_lock = threading.Lock()
_vault_mtime = 0.0


def _load_vault():
    """Load vault.json into _vault_state (reload on file change).

    v2 format: {"items": {"<thumb_key>": {<index entry dict>}, ...}}
    v1 compat:  {"items": ["key1", "key2", ...]} — migrated in-memory but NOT
                written back until next vault/unvault operation.
    """
    global _vault_mtime
    try:
        mtime = os.path.getmtime(_VAULT_PATH)
    except OSError:
        return
    if mtime == _vault_mtime:
        return
    try:
        with open(_VAULT_PATH) as f:
            data = json.load(f)
        raw = data.get("items", {})
        if isinstance(raw, list):
            # v1: list of keys; no stored entry yet — entries come from index
            mapping = {k: {} for k in raw if isinstance(k, str)}
        else:
            mapping = {k: v for k, v in raw.items() if isinstance(k, str)}
        with _vault_state_lock:
            _vault_state["items"] = mapping
        _vault_mtime = mtime
    except (OSError, ValueError):
        pass


def _save_vault():
    """Atomically write vault.json from _vault_state."""
    tmp = _VAULT_PATH + ".tmp"
    with _vault_state_lock:
        items_dict = dict(_vault_state["items"])
    with open(tmp, "w") as f:
        json.dump({"items": items_dict}, f, indent=2)
    os.replace(tmp, _VAULT_PATH)
    global _vault_mtime
    _vault_mtime = os.path.getmtime(_VAULT_PATH)
    # Bust summary cache so vault covers are excluded on next request
    _summary_cache["data"] = None
    _summary_cache["mtime"] = 0
    _month_json_cache["data"] = None
    _month_json_cache["mtime"] = 0


def _get_vault_hashes():
    """Return set of thumb-keys currently in the vault."""
    _load_vault()
    with _vault_state_lock:
        return set(_vault_state["items"].keys())


def _vault_hash_pin(pin, salt_hex):
    """Derive key from PIN using scrypt. Returns hex digest."""
    assert isinstance(pin, str) and len(pin) <= 64, "bad pin type"
    assert isinstance(salt_hex, str) and len(salt_hex) == 32, "bad salt"
    dk = _vault_hashlib.scrypt(
        pin.encode("utf-8"),
        salt=bytes.fromhex(salt_hex),
        n=2**14, r=8, p=1, dklen=32,
    )
    return dk.hex()


def _vault_auth_load():
    """Load vault_auth.json. Returns dict or None."""
    try:
        with open(_VAULT_AUTH_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _vault_auth_save(data):
    """Atomically write vault_auth.json."""
    tmp = _VAULT_AUTH_PATH + ".tmp"
    os.makedirs(os.path.dirname(_VAULT_AUTH_PATH), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _VAULT_AUTH_PATH)


def _vault_session_active():
    """True if the vault session is unlocked and not idle-expired."""
    unlocked_at = session.get("vault_unlocked_at", 0)
    last_active = session.get("vault_last_active", 0)
    if not unlocked_at:
        return False
    if time.time() - last_active > _VAULT_SESSION_IDLE:
        session.pop("vault_unlocked_at", None)
        session.pop("vault_last_active", None)
        return False
    return True


def _vault_touch():
    """Refresh vault inactivity timer."""
    session["vault_last_active"] = time.time()
    session.modified = True


# ── My Eyes Only: encryption at rest ────────────────────────────────────────
# New vault items are stored as AES-256-GCM ciphertext. The key is derived from
# the PIN (PBKDF2) at unlock and held ONLY in this worker's memory for the live
# session — never on disk, never in the signed-not-encrypted Flask cookie. The
# dashboard runs a single gunicorn worker, so this in-memory map is shared across
# its threads. Result: even filesystem/root access yields unreadable blobs.
from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC as _PBKDF2
from cryptography.hazmat.primitives import hashes as _cry_hashes
import secrets as _cry_secrets

_vault_keys = {}                       # kid -> 32-byte key (memory only)
_vault_keys_lock = threading.Lock()

def _vault_derive_key(pin, salt_hex):
    kdf = _PBKDF2(algorithm=_cry_hashes.SHA256(), length=32,
                  salt=bytes.fromhex(salt_hex), iterations=200_000)
    return kdf.derive(pin.encode())

def _vault_set_session_key(key):
    kid = _cry_secrets.token_hex(16)
    with _vault_keys_lock:
        _vault_keys[kid] = key
    session["vault_kid"] = kid
    return kid

def _vault_session_key():
    kid = session.get("vault_kid")
    if not kid:
        return None
    with _vault_keys_lock:
        return _vault_keys.get(kid)

def _vault_clear_session_key():
    kid = session.pop("vault_kid", None)
    if kid:
        with _vault_keys_lock:
            _vault_keys.pop(kid, None)

_vault_token_ser = None  # ponytail: lazy-init avoids import-time app.secret_key dependency

def _vault_token_key(vt):
    """Validate a signed vault token from ?vt= and return the decryption key, or None."""
    assert isinstance(vt, str) and len(vt) <= 512, "bad vt param"
    global _vault_token_ser
    if _vault_token_ser is None:
        _vault_token_ser = URLSafeTimedSerializer(app.secret_key, salt="vault-token")
    try:
        payload = _vault_token_ser.loads(vt, max_age=900)  # was 3600; shortened, but long enough for the A&N offline bulk sync
    except Exception:
        return None
    kid = payload.get("kid") if isinstance(payload, dict) else None
    if not kid:
        return None
    with _vault_keys_lock:
        return _vault_keys.get(kid)

def _vault_encrypt(key, plaintext):
    nonce = _cry_secrets.token_bytes(12)
    return nonce + _AESGCM(key).encrypt(nonce, plaintext, None)

def _vault_decrypt(key, blob):
    return _AESGCM(key).decrypt(blob[:12], blob[12:], None)

# Encrypted vault items are self-contained under vault_enc/<key>/ (orig.enc, thumb.enc,
# hq.enc) — kept separate from the legacy plaintext vault dirs.
_VAULT_ENC_DIR = os.path.join(_APP_DIR, "vault_enc")


def _vault_gen_video_thumb(data, write_enc_fn, thumb_key):
    """Extract a frame from video bytes and write thumb.enc + hq.enc. Returns True on success."""
    import subprocess, tempfile, os as _os
    tmp = tempfile.NamedTemporaryFile(suffix=".tmp", delete=False)
    try:
        tmp.write(data); tmp.flush(); tmp.close()
        for size, name in [(400, "thumb.enc"), (1600, "hq.enc")]:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", tmp.name,
                 "-vf", f"thumbnail=300,scale={size}:{size}:force_original_aspect_ratio=decrease",
                 "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                capture_output=True, timeout=30)
            if result.returncode == 0 and result.stdout:
                write_enc_fn(name, result.stdout)
        return True
    except Exception as e:
        app.logger.warning("[vault] video thumb gen failed %s: %s", thumb_key, e)
        return False
    finally:
        try: _os.unlink(tmp.name)
        except OSError: pass


@app.route("/api/vault/regen_thumbs")
@require_auth
def vault_regen_thumbs():
    """Backfill thumb.enc/hq.enc for existing vault items that lack them (videos mostly)."""
    vt_param = request.args.get("vt", "")
    if vt_param:
        assert len(vt_param) <= 512, "bad vt"
        key = _vault_token_key(vt_param)
        if key is None:
            return jsonify({"error": "vault_key_expired"}), 401
    else:
        if not _vault_session_active():
            abort(403)
        key = _vault_session_key()
        if key is None:
            abort(403)
        _vault_touch()
    _load_vault()
    with _vault_state_lock:
        keys = list(_vault_state["items"].keys())
    done = skipped = errors = 0
    for tk in keys:
        item_dir = os.path.join(_VAULT_ENC_DIR, tk)
        thumb_path = os.path.join(item_dir, "thumb.enc")
        orig_path = os.path.join(item_dir, "orig.enc")
        if os.path.exists(thumb_path) or not os.path.exists(orig_path):
            skipped += 1
            continue
        try:
            with open(orig_path, "rb") as fh:
                plain = _vault_decrypt(key, fh.read())
        except Exception:
            errors += 1
            continue
        def _write_enc(name, data_bytes, _dir=item_dir, _key=key):
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM
            import os as _os2
            tmp = _dir + "/" + name + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(_vault_encrypt(_key, data_bytes))
            _os2.replace(tmp, os.path.join(_dir, name))
        ok = _vault_gen_video_thumb(plain, _write_enc, tk)
        if ok:
            done += 1
            with _vault_state_lock:
                if tk in _vault_state["items"]:
                    _vault_state["items"][tk]["has_thumb"] = True
        else:
            errors += 1
    _save_vault()
    return jsonify({"done": done, "skipped": skipped, "errors": errors})


@app.route("/api/vault/upload", methods=["POST"])
@require_auth
def vault_upload():
    """Direct ENCRYPTED ingest into My Eyes Only.

    Photos go straight in as AES-GCM ciphertext, NEVER touching photo_db / the
    index — so they are never listed in the gallery and never seen by the face
    scan. Used to import the iPhone Hidden album. Requires an unlocked vault
    (holds the in-memory encryption key).
    """
    vt_param = request.args.get("vt", "")
    if vt_param:
        assert len(vt_param) <= 512, "bad vt"
        key = _vault_token_key(vt_param)
        if key is None:
            return jsonify({"error": "vault_key_expired"}), 401
    else:
        if not _vault_session_active():
            return jsonify({"error": "Vault locked"}), 403
        key = _vault_session_key()
        if key is None:
            return jsonify({"error": "No session key — re-unlock"}), 403
        _vault_touch()

    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "No file"}), 400
    data = f.read()
    if not data or len(data) > 1024 * 1024 * 1024:
        return jsonify({"error": "Bad file"}), 400

    import hashlib as _hl, io as _io, time as _t
    thumb_key = _hl.sha256(data).hexdigest()

    _load_vault()
    with _vault_state_lock:
        if thumb_key in _vault_state["items"]:
            return jsonify({"ok": True, "dup": True, "key": thumb_key})

    ext = (f.filename or "").lower().rsplit(".", 1)[-1]
    is_video = (f.mimetype or "").startswith("video") or ext in ("mov", "mp4", "m4v")

    item_dir = os.path.join(_VAULT_ENC_DIR, thumb_key)
    os.makedirs(item_dir, exist_ok=True)

    def _write_enc(name, plain):
        tmp = os.path.join(item_dir, name + ".tmp")
        with open(tmp, "wb") as out:
            out.write(_vault_encrypt(key, plain))
        os.replace(tmp, os.path.join(item_dir, name))

    _write_enc("orig.enc", data)          # original always (ciphertext)

    made_thumb = False
    if not is_video:
        try:
            from PIL import Image, ImageOps
            def _thumb(px):
                im = ImageOps.exif_transpose(Image.open(_io.BytesIO(data))).convert("RGB")
                im.thumbnail((px, px), Image.LANCZOS)
                buf = _io.BytesIO(); im.save(buf, "JPEG", quality=82); return buf.getvalue()
            _write_enc("thumb.enc", _thumb(400))
            _write_enc("hq.enc", _thumb(1600))
            made_thumb = True
        except Exception as e:
            app.logger.warning("[vault/upload] thumb gen failed %s: %s", thumb_key, e)
    else:
        made_thumb = _vault_gen_video_thumb(data, _write_enc, thumb_key)

    entry = {
        "enc": True,
        "path": f.filename or thumb_key,
        "thumb": f"/api/vault/thumb/{thumb_key}",
        "thumb_hq": f"/api/vault/thumb_hq/{thumb_key}",
        "date": float(request.form.get("date") or _t.time()),
        "type": "video" if is_video else "image",
        "has_thumb": made_thumb,
    }
    with _vault_state_lock:
        _vault_state["items"][thumb_key] = entry
    _save_vault()
    return jsonify({"ok": True, "key": thumb_key})


def _vault_thumb_dirs_for_key(thumb_key):
    """Return list of (public_dir, vault_dir, filename) tuples for a thumb key."""
    fname = thumb_key + ".jpg"
    pairs = [
        (_THUMB_DIR,         _VAULT_THUMB_DIR,     fname),
        (_THUMB_HQ_DIR,      _VAULT_THUMB_HQ_DIR,  fname),
        (_THUMB_PREVIEW_DIR, _VAULT_THUMB_PRV_DIR,  fname),
        (_THUMB_MAX_DIR,     _VAULT_THUMB_MAX_DIR,  thumb_key + ".webp"),
    ]
    return pairs


def _vault_move_thumbs(thumb_key, direction):
    """Move thumbnail files between public and vault directories.

    direction='in'  → public → vault  (hiding)
    direction='out' → vault → public  (restoring)
    Idempotent: missing source is skipped without error.
    """
    assert direction in ("in", "out"), "invalid direction"
    pairs = _vault_thumb_dirs_for_key(thumb_key)
    for pub_dir, vlt_dir, fname in pairs:
        if direction == "in":
            src = os.path.join(pub_dir, fname)
            dst = os.path.join(vlt_dir, fname)
        else:
            src = os.path.join(vlt_dir, fname)
            dst = os.path.join(pub_dir, fname)
        if os.path.exists(src):
            try:
                os.rename(src, dst)
            except OSError:
                pass  # cross-device: skip gracefully

    # Also move video_cache mp4 and HLS dir if they exist
    mp4_name = thumb_key + ".mp4"
    if direction == "in":
        mp4_src = os.path.join(_VIDEO_CACHE_DIR, mp4_name)
        mp4_dst = os.path.join(_VAULT_VIDEO_DIR, mp4_name)
        hls_src = os.path.join(_HLS_CACHE_DIR, thumb_key)
        hls_dst = os.path.join(_VAULT_HLS_DIR, thumb_key)
    else:
        mp4_src = os.path.join(_VAULT_VIDEO_DIR, mp4_name)
        mp4_dst = os.path.join(_VIDEO_CACHE_DIR, mp4_name)
        hls_src = os.path.join(_VAULT_HLS_DIR, thumb_key)
        hls_dst = os.path.join(_HLS_CACHE_DIR, thumb_key)

    if os.path.exists(mp4_src):
        try:
            os.rename(mp4_src, mp4_dst)
        except OSError:
            pass

    if os.path.isdir(hls_src):
        try:
            os.rename(hls_src, hls_dst)
        except OSError:
            pass


def _thumb_key_for_item(item):
    """Extract thumb key from a photo_index item."""
    thumb_url = item.get("thumb", "")
    if not thumb_url:
        return None
    return thumb_url.rsplit("/", 1)[-1].replace(".jpg", "")


def _item_for_thumb_key(thumb_key):
    """Find a photo_index item by its thumb key."""
    items = load_photo_index()
    for item in items:
        if _thumb_key_for_item(item) == thumb_key:
            return item
    return None


def _rel_from_index_path(index_path):
    """Derive PHOTOS_ROOT-relative path from a canonical index path.

    Strategy:
    1. If the file exists at index_path directly → relpath from PHOTOS_ROOT.
    2. Try _resolve_photo_path (handles legacy aliases) → relpath.
    3. Try known prefix rewrites as last resort (file might be in .vault).

    Returns rel (no leading slash) or None if it cannot be determined.
    MUST produce the same rel regardless of whether the file is in the
    library or already in .vault — so we always use the actual on-disk
    path when available, and fall through to a vault-side probe.
    """
    assert isinstance(index_path, str) and index_path, "index_path must be non-empty str"

    # 1. Direct path — file is at the index path itself (most common inside LXC)
    if os.path.isfile(index_path):
        try:
            rel = os.path.relpath(index_path, PHOTOS_ROOT)
            if not rel.startswith(".."):
                return rel
        except ValueError:
            pass

    # 2. _resolve_photo_path alias chain
    rp = _resolve_photo_path(index_path)
    if rp and os.path.isfile(rp):
        try:
            rel = os.path.relpath(rp, PHOTOS_ROOT)
            if not rel.startswith(".."):
                return rel
        except ValueError:
            pass

    # 3. File is in .vault already — probe by stripping known root prefixes.
    #    Try all plausible PHOTOS_ROOT values so this works on both host and LXC.
    root_candidates = [
        PHOTOS_ROOT,
        "/mnt/data/PHOTOS",
        "/mnt/data/PROMETHEUS/PHOTOS",
        "/mnt/nvme/PROMETHEUS/PHOTOS",
    ]
    for root in root_candidates:
        if not root:
            continue
        if index_path.startswith(root + "/"):
            rel_candidate = index_path[len(root) + 1:]
            # Check vault side: if PHOTOS_ROOT/.vault/<rel_candidate> exists
            vault_check = os.path.join(PHOTOS_ROOT, ".vault", rel_candidate)
            if os.path.isfile(vault_check):
                return rel_candidate

    # 4. Brute-force: strip the longest matching root prefix and accept
    #    even if we can't verify existence (needed for migration path checks)
    if index_path.startswith(PHOTOS_ROOT + "/"):
        rel = index_path[len(PHOTOS_ROOT) + 1:]
        if rel and not rel.startswith("."):
            return rel

    return None


def _resolve_original_for_vault(index_path):
    """Resolve an index path to its real on-disk library path.

    Returns (real_path, rel_path) where rel_path is relative to PHOTOS_ROOT,
    or (None, None) on failure.  real_path may be None when the file is
    already in .vault — callers use rel directly.
    """
    assert isinstance(index_path, str) and index_path, "index_path must be non-empty str"
    rel = _rel_from_index_path(index_path)
    if not rel or rel.startswith(".."):
        return None, None
    # Try to find the real on-disk library path
    rp = _resolve_photo_path(index_path)
    if not rp:
        candidate = os.path.join(PHOTOS_ROOT, rel)
        if os.path.isfile(candidate):
            rp = candidate
    return rp, rel


def _vault_move_original(item, direction):
    """Move the original file between library and vault storage.

    direction='in'  → PHOTOS_ROOT/<rel> → PHOTOS_ROOT/.vault/<rel>
    direction='out' → PHOTOS_ROOT/.vault/<rel> → PHOTOS_ROOT/<rel>

    Returns the destination path on success, None if source missing or error.
    Uses os.replace for atomicity (same filesystem guaranteed).
    """
    assert direction in ("in", "out"), "invalid direction"
    index_path = item.get("path", "")
    if not index_path:
        return None
    # Derive rel without requiring the file to exist at its library path
    rel = _rel_from_index_path(index_path)
    if not rel or rel.startswith(".."):
        return None

    if direction == "in":
        # File must exist at library path (or resolvable equivalent)
        real_path, _ = _resolve_original_for_vault(index_path)
        src = real_path if real_path else os.path.join(PHOTOS_ROOT, rel)
        dst = os.path.join(_VAULT_ORIGINALS_DIR, rel)
    else:
        # File is in vault dir; library path is the destination
        src = os.path.join(_VAULT_ORIGINALS_DIR, rel)
        dst = os.path.join(PHOTOS_ROOT, rel)

    if not os.path.isfile(src):
        return None

    dst_dir = os.path.dirname(dst)
    try:
        os.makedirs(dst_dir, exist_ok=True)
        os.replace(src, dst)
        return dst
    except OSError as e:
        app.logger.error("[vault] move original %s → %s failed: %s", src, dst, e)
        return None


# ─── Vault setup (called once; safe to re-call) ───────────────────────────────

def _vault_ensure_initialized():
    """Initialize vault_auth.json with scrypt-hashed default PIN if absent."""
    if os.path.exists(_VAULT_AUTH_PATH):
        return
    import secrets as _sec
    salt = _sec.token_hex(16)
    # PIN is stored only as a hash — the plaintext never touches disk
    pin_hash = _vault_hash_pin("2225", salt)
    _vault_auth_save({
        "salt": salt,
        "hash": pin_hash,
        "fails": 0,
        "lockout_until": 0.0,
    })
    app.logger.info("[vault] vault_auth.json initialized")


def _vault_migrate_v1():
    """Migrate v1 vault items (originals still in library) to v2 (originals in .vault/).

    For each key in vault.json whose stored entry dict is empty (v1 import),
    we find the item in photo_index.json, move the original to .vault/,
    remove the entry from the index, and populate the entry dict in vault.json.
    Safe to re-run: items already migrated have a non-empty entry dict.
    Called once at startup after _load_vault().
    """
    _load_vault()
    with _vault_state_lock:
        mapping = dict(_vault_state["items"])

    if not mapping:
        return

    # Identify keys that have no stored entry (v1) and need migration
    needs_migration = [k for k, v in mapping.items() if not v]
    if not needs_migration:
        return

    app.logger.info("[vault] Migrating %d v1 items to v2 (moving originals)...",
                    len(needs_migration))

    items = load_photo_index()
    key_set = set(needs_migration)
    index_by_key = {}
    for item in items:
        tk = _thumb_key_for_item(item)
        if tk and tk in key_set:
            index_by_key[tk] = item

    migrated_keys = []
    removed_paths = set()
    for tk in needs_migration:
        item = index_by_key.get(tk)
        if not item:
            app.logger.warning("[vault] v1 migration: no index entry for key %s, skipping", tk)
            continue
        dst = _vault_move_original(item, "in")
        if not dst:
            app.logger.warning("[vault] v1 migration: could not move original for key %s (%s)",
                               tk, item.get("path"))
            continue
        with _vault_state_lock:
            _vault_state["items"][tk] = dict(item)
        removed_paths.add(item.get("path", ""))
        migrated_keys.append(tk)
        app.logger.info("[vault] v1 migrated: %s", item.get("path"))

    if migrated_keys:
        # Remove migrated entries from photo_index
        new_items = [it for it in items if it.get("path", "") not in removed_paths]
        _save_photo_index(new_items)
        # Persist updated vault.json (now has full entry dicts)
        _save_vault()
        app.logger.info("[vault] Migration complete: %d items moved to .vault/", len(migrated_keys))


# Vault startup init deferred to after _resolve_photo_path is defined (see below).


# ─── Vault API routes ─────────────────────────────────────────────────────────

@app.route("/api/vault/status")
@require_auth
def vault_status():
    """Return vault lock state + item count. Does NOT require vault unlock."""
    _load_vault()
    with _vault_state_lock:
        count = len(_vault_state["items"])
    auth = _vault_auth_load() or {}
    locked_out = time.time() < auth.get("lockout_until", 0)
    wa_creds = auth.get("webauthn_credentials", [])
    wa_count = len(wa_creds) if isinstance(wa_creds, list) else 0
    return jsonify({
        "unlocked": _vault_session_active(),
        "item_count": count,
        "locked_out": locked_out,
        "lockout_remaining": max(0, auth.get("lockout_until", 0) - time.time()),
        "webauthn_credential_count": wa_count,
        "webauthn_available": _FIDO2_OK,
    })


@app.route("/api/vault/unlock", methods=["POST"])
@require_auth
def vault_unlock():
    """Unlock vault with PIN. Rate-limited."""
    auth = _vault_auth_load()
    if not auth:
        return jsonify({"error": "Vault not initialized"}), 500

    if time.time() < auth.get("lockout_until", 0):
        remaining = int(auth["lockout_until"] - time.time())
        return jsonify({"error": f"Too many failures. Retry in {remaining}s"}), 429

    data = request.get_json(silent=True) or {}
    pin = str(data.get("pin", ""))
    if not pin or len(pin) > 16:
        return jsonify({"error": "Bad request"}), 400

    expected = _vault_hash_pin(pin, auth["salt"])
    # Constant-time compare
    match = _vault_hmac.compare_digest(expected, auth["hash"])

    if match:
        auth["fails"] = 0
        _vault_auth_save(auth)
        session["vault_unlocked_at"] = time.time()
        session["vault_last_active"] = time.time()
        session.modified = True
        # Derive the at-rest encryption key from the PIN; hold it in memory only
        # for this session (enables encrypted upload + decrypt-on-view).
        key = _vault_derive_key(pin, auth["salt"])
        kid = _vault_set_session_key(key)
        token = URLSafeTimedSerializer(app.secret_key, salt="vault-token").dumps({"kid": kid})
        return jsonify({"success": True, "vault_token": token})
    else:
        auth["fails"] = auth.get("fails", 0) + 1
        if auth["fails"] >= _VAULT_PIN_FAILS:
            auth["lockout_until"] = time.time() + _VAULT_LOCKOUT_SECS
            auth["fails"] = 0
            _vault_auth_save(auth)
            return jsonify({"error": f"Too many failures. Locked for {_VAULT_LOCKOUT_SECS}s"}), 429
        _vault_auth_save(auth)
        remaining_tries = _VAULT_PIN_FAILS - auth["fails"]
        return jsonify({"error": f"Wrong PIN. {remaining_tries} attempt(s) left"}), 401


@app.route("/api/vault/lock", methods=["POST"])
@require_auth
def vault_lock():
    """Explicitly lock the vault."""
    session.pop("vault_unlocked_at", None)
    session.pop("vault_last_active", None)
    _vault_clear_session_key()          # drop the in-memory encryption key
    # Purge cached vault video transcodes on lock (defense-in-depth; they're vault-key-encrypted
    # at rest anyway, but nothing derived from vault content should linger past an explicit lock).
    try:
        import glob as _glob
        for _f in _glob.glob(os.path.join(_VAULT_VIDEO_DIR, "*.h264.enc")):
            try: os.remove(_f)
            except OSError: pass
    except Exception:
        pass
    session.modified = True
    return jsonify({"success": True})


# ─── WebAuthn / Biometric vault unlock ────────────────────────────────────────
# RP: pve.tail3045df.ts.net  (matches browser origin https://pve.tail3045df.ts.net)
# Credential store: ai_data/vault_auth.json under key "webauthn_credentials"
#   List of: {"id": "<b64url>", "public_key": "<b64url>", "sign_count": N,
#              "created": <epoch>, "label": "<ua snippet>"}
# Challenge state lives in Flask session only (single-use, short TTL via _WEBAUTHN_CHALLENGE_TTL).
# Auth failures are rate-limited via the same fails/lockout fields as PIN.

_WEBAUTHN_RP_ID     = "ares.tail3045df.ts.net"
_WEBAUTHN_ORIGIN    = "https://ares.tail3045df.ts.net"
_WEBAUTHN_CHALLENGE_TTL = 120  # seconds — challenge expires if not consumed

try:
    from fido2.server import Fido2Server
    from fido2.webauthn import (
        PublicKeyCredentialRpEntity,
        PublicKeyCredentialUserEntity,
        UserVerificationRequirement,
        AuthenticatorAttachment,
        PublicKeyCredentialDescriptor,
        PublicKeyCredentialType,
    )
    from fido2.cbor import decode as _fido2_cbor_decode
    import base64 as _b64

    _FIDO2_RP  = PublicKeyCredentialRpEntity(id=_WEBAUTHN_RP_ID, name="ARES Gallery")
    _FIDO2_SRV = Fido2Server(_FIDO2_RP)
    _FIDO2_OK  = True
except Exception as _fido2_import_err:
    app.logger.warning("[vault/webauthn] fido2 import failed: %s", _fido2_import_err)
    _FIDO2_OK  = False


def _b64url_decode(s):
    """Decode a base64url string (with or without padding) to bytes."""
    assert isinstance(s, str) and len(s) <= 4096, "bad b64url input"
    s = s.replace("-", "+").replace("_", "/")
    pad = (4 - len(s) % 4) % 4
    return _b64.b64decode(s + "=" * pad)


def _b64url_encode(b):
    """Encode bytes to base64url without padding."""
    assert isinstance(b, (bytes, bytearray)) and len(b) <= 65536, "bad bytes input"
    return _b64.urlsafe_b64encode(bytes(b)).rstrip(b"=").decode()


def _webauthn_creds_from_auth(auth):
    """Build list of AttestedCredentialData for Fido2Server.authenticate_complete.

    Storage format: each entry has "attested_cred_data" — base64url of the raw
    AttestedCredentialData bytes (AAGUID + credIdLen + credId + CBOR pubkey).
    """
    from fido2.webauthn import AttestedCredentialData
    raw_list = auth.get("webauthn_credentials", [])
    assert isinstance(raw_list, list), "webauthn_credentials must be list"
    result = []
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        acd = entry.get("attested_cred_data", "")
        if not acd:
            continue
        try:
            acd_bytes = _b64url_decode(acd)
            result.append(AttestedCredentialData(acd_bytes))
        except Exception:
            continue
    return result


def _webauthn_descriptors_from_auth(auth):
    """Build list of PublicKeyCredentialDescriptor for allowCredentials."""
    raw_list = auth.get("webauthn_credentials", [])
    assert isinstance(raw_list, list), "webauthn_credentials must be list"
    result = []
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("id", "")
        if not cid:
            continue
        try:
            cid_bytes = _b64url_decode(cid)
        except Exception:
            continue
        result.append(PublicKeyCredentialDescriptor(
            type=PublicKeyCredentialType.PUBLIC_KEY,
            id=cid_bytes,
        ))
    return result


def _webauthn_opts_to_dict(opts_public_key):
    """Convert fido2 options dict (with Enum values/bytes) to JSON-safe dict."""
    def _conv(obj):
        if isinstance(obj, dict):
            return {k: _conv(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_conv(v) for v in obj]
        if isinstance(obj, (bytes, bytearray)):
            return _b64url_encode(bytes(obj))
        if hasattr(obj, "value"):
            return obj.value
        return obj
    return _conv(opts_public_key)


@app.route("/api/vault/webauthn/register-options", methods=["POST"])
@require_auth
def vault_webauthn_register_options():
    """Return WebAuthn credential creation options. Requires active PIN-unlocked session."""
    if not _FIDO2_OK:
        return jsonify({"error": "WebAuthn not available"}), 503
    if not _vault_session_active():
        return jsonify({"error": "PIN unlock required before enrolling biometrics"}), 403

    auth = _vault_auth_load()
    if not auth:
        return jsonify({"error": "Vault not initialized"}), 500

    user = PublicKeyCredentialUserEntity(
        id=b"ares-vault-zain",
        name="zain",
        display_name="Zain",
    )
    existing = _webauthn_descriptors_from_auth(auth)
    opts, state = _FIDO2_SRV.register_begin(
        user=user,
        credentials=existing,
        user_verification=UserVerificationRequirement.REQUIRED,
        authenticator_attachment=AuthenticatorAttachment.PLATFORM,
    )

    session["webauthn_reg_state"]    = state
    session["webauthn_reg_state_ts"] = time.time()
    session.modified = True

    return jsonify({"publicKey": _webauthn_opts_to_dict(opts["publicKey"])})


@app.route("/api/vault/webauthn/register-verify", methods=["POST"])
@require_auth
def vault_webauthn_register_verify():
    """Verify and store a new WebAuthn credential. Requires active PIN-unlocked session."""
    if not _FIDO2_OK:
        return jsonify({"error": "WebAuthn not available"}), 503
    if not _vault_session_active():
        return jsonify({"error": "Vault not unlocked"}), 403

    state    = session.get("webauthn_reg_state")
    state_ts = session.get("webauthn_reg_state_ts", 0)
    if not state or (time.time() - state_ts) > _WEBAUTHN_CHALLENGE_TTL:
        return jsonify({"error": "Challenge expired or missing"}), 400

    session.pop("webauthn_reg_state", None)
    session.pop("webauthn_reg_state_ts", None)
    session.modified = True

    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "Bad request"}), 400

    # Build RegistrationResponse dict from client JSON
    # Client sends: id, rawId (b64url), response.clientDataJSON, response.attestationObject, type
    try:
        # fido2 2.x parses the WebAuthn JSON serialization: binary fields are
        # base64url STRINGS the library decodes itself. Pre-decoding to bytes
        # made its literal id-vs-rawId comparison fail (str != bytes).
        reg_response = {
            "id": data.get("id", ""),
            "rawId": data.get("rawId", ""),
            "response": {
                "clientDataJSON": data.get("response", {}).get("clientDataJSON", ""),
                "attestationObject": data.get("response", {}).get("attestationObject", ""),
            },
            "type": data.get("type", "public-key"),
        }
    except Exception as e:
        app.logger.warning("[vault/webauthn] register decode error: %s", e)
        return jsonify({"error": "Malformed credential response"}), 400

    try:
        auth_data = _FIDO2_SRV.register_complete(state, reg_response)
    except Exception as e:
        app.logger.warning("[vault/webauthn] register_complete failed: %s", e)
        return jsonify({"error": str(e)}), 400

    # Store the new credential
    # AttestedCredentialData is a bytes subclass — store the raw bytes (AAGUID+credId+pubkey)
    cred = auth_data.credential_data
    assert cred is not None, "No credential data in auth_data"

    cred_id_b64  = _b64url_encode(bytes(cred.credential_id))
    acd_b64      = _b64url_encode(bytes(cred))   # full AttestedCredentialData bytes
    sign_count   = auth_data.counter if hasattr(auth_data, "counter") else 0
    ua_label     = request.headers.get("User-Agent", "")[:80]

    vault_auth = _vault_auth_load() or {}
    creds_list = vault_auth.get("webauthn_credentials", [])
    assert isinstance(creds_list, list), "corrupted creds list"

    # Reject duplicate credential ID
    for existing in creds_list:
        if existing.get("id") == cred_id_b64:
            return jsonify({"error": "Credential already registered"}), 409

    new_entry = {
        "id":                 cred_id_b64,
        "attested_cred_data": acd_b64,
        "sign_count":         sign_count,
        "created":            int(time.time()),
        "label":              ua_label,
    }
    creds_list.append(new_entry)
    vault_auth["webauthn_credentials"] = creds_list
    _vault_auth_save(vault_auth)

    app.logger.info("[vault/webauthn] Credential enrolled; total=%d", len(creds_list))
    _vault_touch()
    return jsonify({"success": True, "credential_count": len(creds_list)})


@app.route("/api/vault/webauthn/auth-options", methods=["POST"])
@require_auth
def vault_webauthn_auth_options():
    """Return WebAuthn authentication challenge. No session required."""
    if not _FIDO2_OK:
        return jsonify({"error": "WebAuthn not available"}), 503

    auth = _vault_auth_load()
    if not auth:
        return jsonify({"error": "Vault not initialized"}), 500

    descriptors = _webauthn_descriptors_from_auth(auth)
    if not descriptors:
        return jsonify({"error": "No credentials enrolled"}), 404

    # Pass descriptors (not full AttestedCredentialData) to authenticate_begin so
    # the allowCredentials list is populated without needing to parse CBOR pubkeys.
    # The full credentials are only needed at authenticate_complete time.
    opts, state = _FIDO2_SRV.authenticate_begin(
        credentials=descriptors,
        user_verification=UserVerificationRequirement.REQUIRED,
    )

    session["webauthn_auth_state"]    = state
    session["webauthn_auth_state_ts"] = time.time()
    session.modified = True

    return jsonify({"publicKey": _webauthn_opts_to_dict(opts["publicKey"])})


@app.route("/api/vault/webauthn/auth-verify", methods=["POST"])
@require_auth
def vault_webauthn_auth_verify():
    """Verify WebAuthn assertion and create vault session. Rate-limited."""
    if not _FIDO2_OK:
        return jsonify({"error": "WebAuthn not available"}), 503

    auth = _vault_auth_load()
    if not auth:
        return jsonify({"error": "Vault not initialized"}), 500

    # Rate limiting — reuse the PIN lockout fields
    if time.time() < auth.get("lockout_until", 0):
        remaining = int(auth["lockout_until"] - time.time())
        return jsonify({"error": f"Too many failures. Retry in {remaining}s"}), 429

    state    = session.get("webauthn_auth_state")
    state_ts = session.get("webauthn_auth_state_ts", 0)
    if not state or (time.time() - state_ts) > _WEBAUTHN_CHALLENGE_TTL:
        session.pop("webauthn_auth_state", None)
        session.pop("webauthn_auth_state_ts", None)
        session.modified = True
        return jsonify({"error": "Challenge expired — request new options"}), 400

    session.pop("webauthn_auth_state", None)
    session.pop("webauthn_auth_state_ts", None)
    session.modified = True

    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "Bad request"}), 400

    try:
        # Same as register-verify: pass base64url STRINGS through — the fido2
        # JSON parser decodes binary fields itself.
        _r = data.get("response", {})
        auth_response = {
            "id":    data.get("id", ""),
            "rawId": data.get("rawId", ""),
            "response": {
                "clientDataJSON":    _r.get("clientDataJSON", ""),
                "authenticatorData": _r.get("authenticatorData", ""),
                "signature":         _r.get("signature", ""),
            },
            "type": data.get("type", "public-key"),
        }
        if _r.get("userHandle"):
            auth_response["response"]["userHandle"] = _r.get("userHandle")
    except Exception as e:
        app.logger.warning("[vault/webauthn] auth decode error: %s", e)
        _vault_auth_fail_tick(auth)
        return jsonify({"error": "Malformed assertion"}), 400

    creds = _webauthn_creds_from_auth(auth)
    if not creds:
        return jsonify({"error": "No credentials enrolled"}), 404

    try:
        matched_cred = _FIDO2_SRV.authenticate_complete(state, creds, auth_response)
    except Exception as e:
        app.logger.warning("[vault/webauthn] authenticate_complete failed: %s", e)
        _vault_auth_fail_tick(auth)
        return jsonify({"error": "Biometric verification failed"}), 401

    # authenticate_complete returns the matched AttestedCredentialData.
    # Extract the counter from the assertion's authenticatorData (sign_count tracking).
    # The authenticatorData bytes are in auth_response["response"]["authenticatorData"].
    try:
        from fido2.webauthn import AuthenticatorData as _AuthData
        _adata = _AuthData(auth_response["response"]["authenticatorData"])
        new_sign_count = _adata.counter
    except Exception:
        new_sign_count = 0

    matched_id = _b64url_encode(bytes(matched_cred.credential_id))
    creds_list = auth.get("webauthn_credentials", [])
    for entry in creds_list:
        if entry.get("id") == matched_id:
            old_count = entry.get("sign_count", 0)
            if new_sign_count < old_count and new_sign_count != 0:
                app.logger.warning("[vault/webauthn] sign_count regression for %s", matched_id[:16])
            entry["sign_count"] = new_sign_count
    auth["webauthn_credentials"] = creds_list
    auth["fails"] = 0
    _vault_auth_save(auth)

    # Grant vault session
    session["vault_unlocked_at"] = time.time()
    session["vault_last_active"]  = time.time()
    session.modified = True

    app.logger.info("[vault/webauthn] Biometric unlock OK, cred=%s", matched_id[:16])
    return jsonify({"success": True})


@app.route("/api/vault/webauthn/credentials", methods=["GET"])
@require_auth
def vault_webauthn_list_credentials():
    """List enrolled WebAuthn credentials (no secrets returned). Requires vault session."""
    if not _vault_session_active():
        return jsonify({"error": "Vault locked"}), 403
    _vault_touch()
    auth = _vault_auth_load() or {}
    creds_list = auth.get("webauthn_credentials", [])
    safe = []
    for i, entry in enumerate(creds_list):
        if not isinstance(entry, dict):
            continue
        safe.append({
            "index":   i,
            "id":      entry.get("id", "")[:16] + "...",
            "created": entry.get("created", 0),
            "label":   entry.get("label", "")[:80],
        })
    return jsonify({"credentials": safe})


@app.route("/api/vault/webauthn/credentials/<int:idx>", methods=["DELETE"])
@require_auth
def vault_webauthn_delete_credential(idx):
    """Remove a WebAuthn credential by index. Requires vault session."""
    if not _vault_session_active():
        return jsonify({"error": "Vault locked"}), 403
    _vault_touch()
    assert 0 <= idx < 100, "bad index"
    auth = _vault_auth_load()
    if not auth:
        return jsonify({"error": "Vault not initialized"}), 500
    creds_list = auth.get("webauthn_credentials", [])
    assert isinstance(creds_list, list), "corrupted creds list"
    if idx >= len(creds_list):
        return jsonify({"error": "Index out of range"}), 404
    del creds_list[idx]
    auth["webauthn_credentials"] = creds_list
    _vault_auth_save(auth)
    app.logger.info("[vault/webauthn] Credential %d removed; remaining=%d", idx, len(creds_list))
    return jsonify({"success": True, "credential_count": len(creds_list)})


def _vault_auth_fail_tick(auth):
    """Increment fail counter and apply lockout if threshold reached."""
    assert isinstance(auth, dict), "bad auth dict"
    auth["fails"] = auth.get("fails", 0) + 1
    if auth["fails"] >= _VAULT_PIN_FAILS:
        auth["lockout_until"] = time.time() + _VAULT_LOCKOUT_SECS
        auth["fails"] = 0
    _vault_auth_save(auth)


# ─── End WebAuthn block ────────────────────────────────────────────────────────


@app.route("/api/vault/items")
@require_auth
def vault_items():
    """Return vault contents. Requires vault session or ?vt= token."""
    vt_param = request.args.get("vt", "")
    if vt_param:
        assert len(vt_param) <= 512, "bad vt"
        if _vault_token_key(vt_param) is None:
            return jsonify({"error": "vault_key_expired"}), 401
    else:
        if not _vault_session_active():
            return jsonify({"error": "Vault locked"}), 403
        _vault_touch()

    _load_vault()
    with _vault_state_lock:
        mapping = dict(_vault_state["items"])

    result = []
    for tk, entry in mapping.items():
        if not entry:
            continue  # v1 not-yet-migrated; skip (migration runs at startup)
        out = dict(entry)
        out["key"]      = tk
        out["thumb"]    = f"/api/vault/thumb/{tk}"
        out["thumb_hq"] = f"/api/vault/thumb_hq/{tk}"
        out["_in_vault"] = True
        result.append(out)

    result.sort(key=lambda x: -x.get("date", 0))
    return jsonify(result)


@app.route("/api/vault/add", methods=["POST"])
@require_auth
def vault_add():
    """Move one or more items (by thumb_key list) into the vault.

    v2: moves original file to PHOTOS_ROOT/.vault/<rel>, removes index entry.
    """
    if not _vault_session_active():
        return jsonify({"error": "Vault locked"}), 403
    _vault_touch()

    data = request.get_json(silent=True) or {}
    keys = data.get("thumb_keys", [])
    if not keys or not isinstance(keys, list) or len(keys) > 500:
        return jsonify({"error": "Bad request"}), 400

    _load_vault()
    added = []
    failed = []

    for tk in keys:
        # Validate with an explicit check, not assert (asserts are stripped under `python -O`,
        # which would let a bad key flow into path building; and a bad key should skip, not 500).
        if not (isinstance(tk, str) and len(tk) <= 64 and tk.replace("-", "").isalnum()):
            failed.append(tk)
            continue
        with _vault_state_lock:
            if tk in _vault_state["items"]:
                continue

        # Find item in index before we remove it
        item = _item_for_thumb_key(tk)
        if not item:
            app.logger.warning("[vault/add] no index entry for key %s", tk)
            failed.append(tk)
            continue

        # Move original to .vault/
        dst = _vault_move_original(item, "in")
        if not dst:
            app.logger.error("[vault/add] could not move original for %s", item.get("path"))
            failed.append(tk)
            continue

        # Move thumbs to vault thumb dirs
        _vault_move_thumbs(tk, "in")

        # Remove from photo_index (load fresh to avoid races)
        cur_items = load_photo_index()
        item_path = item.get("path", "")
        new_items = [it for it in cur_items if it.get("path", "") != item_path]
        _save_photo_index(new_items)

        # Verify removal (retry once if auto-scan clobbered the write)
        verify = load_photo_index()
        if any(it.get("path", "") == item_path for it in verify):
            new_items2 = [it for it in verify if it.get("path", "") != item_path]
            _save_photo_index(new_items2)

        # Record in vault state with full entry copy
        with _vault_state_lock:
            _vault_state["items"][tk] = dict(item)

        added.append(tk)

    if added:
        _save_vault()

    return jsonify({"added": added, "failed": failed, "count": len(added)})


@app.route("/api/vault/remove", methods=["POST"])
@require_auth
def vault_remove():
    """Move items out of the vault back to the normal gallery.

    v2: moves original file from PHOTOS_ROOT/.vault/<rel> back to library,
    re-inserts the saved index entry.
    """
    if not _vault_session_active():
        return jsonify({"error": "Vault locked"}), 403
    _vault_touch()

    data = request.get_json(silent=True) or {}
    keys = data.get("thumb_keys", [])
    if not keys or not isinstance(keys, list) or len(keys) > 500:
        return jsonify({"error": "Bad request"}), 400

    _load_vault()
    removed = []
    failed = []

    for tk in keys:
        if not (isinstance(tk, str) and len(tk) <= 64 and tk.replace("-", "").isalnum()):
            failed.append(tk)
            continue
        with _vault_state_lock:
            if tk not in _vault_state["items"]:
                continue
            stored_entry = _vault_state["items"][tk]

        if not stored_entry:
            app.logger.warning("[vault/remove] no stored entry for key %s, cannot restore", tk)
            failed.append(tk)
            continue

        # Move original back to library
        dst = _vault_move_original(stored_entry, "out")
        if not dst:
            app.logger.error("[vault/remove] could not move original back for key %s", tk)
            failed.append(tk)
            continue

        # Move thumbs back to public dirs
        _vault_move_thumbs(tk, "out")

        # Re-insert into photo_index (load fresh to avoid races)
        cur_items = load_photo_index()
        item_path = stored_entry.get("path", "")
        # Only insert if not already present (idempotency)
        if not any(it.get("path", "") == item_path for it in cur_items):
            cur_items.append(dict(stored_entry))
            cur_items.sort(key=_photo_sort_key)
            _save_photo_index(cur_items)
            # Verify re-insertion (retry once)
            verify = load_photo_index()
            if not any(it.get("path", "") == item_path for it in verify):
                verify.append(dict(stored_entry))
                verify.sort(key=_photo_sort_key)
                _save_photo_index(verify)

        # Remove from vault state
        with _vault_state_lock:
            _vault_state["items"].pop(tk, None)

        removed.append(tk)

    if removed:
        _save_vault()

    return jsonify({"removed": removed, "failed": failed, "count": len(removed)})


@app.route("/api/vault/delete", methods=["POST"])
@require_auth
def vault_delete():
    """Permanently delete vault items — remove from state + delete enc blob + thumbs.
    Unlike /api/vault/remove (which restores the original to the library), this is gone-forever."""
    if not _vault_session_active():
        return jsonify({"error": "Vault locked"}), 403
    _vault_touch()
    data = request.get_json(silent=True) or {}
    keys = data.get("thumb_keys", [])
    if not keys or not isinstance(keys, list) or len(keys) > 500:
        return jsonify({"error": "Bad request"}), 400
    import shutil, glob as _glob
    deleted = 0
    for tk in keys:
        if not isinstance(tk, str) or not tk or len(tk) > 64 or "/" in tk:
            continue
        with _vault_state_lock:
            existed = tk in _vault_state["items"]
            _vault_state["items"].pop(tk, None)
        shutil.rmtree(os.path.join(_VAULT_ENC_DIR, tk), ignore_errors=True)
        for p in (os.path.join(_VAULT_THUMB_DIR, tk + ".jpg"),
                  os.path.join(_VAULT_THUMB_HQ_DIR, tk + ".jpg"),
                  os.path.join(_VAULT_THUMB_PRV_DIR, tk + ".jpg"),
                  os.path.join(_VAULT_THUMB_MAX_DIR, tk + ".webp")):
            try: os.remove(p)
            except OSError: pass
        for base in (_VAULT_VIDEO_DIR, _VAULT_HLS_DIR):
            for p in _glob.glob(os.path.join(base, tk + "*")):
                try:
                    shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
                except OSError: pass
        if existed: deleted += 1
    _save_vault()
    return jsonify({"deleted": deleted})


def _serve_vault_thumb(tier, thumb_key):
    """Generic vault thumb server — requires vault session or ?vt= token."""
    vt_param = request.args.get("vt", "")
    if vt_param:
        assert len(vt_param) <= 512, "bad vt"
        enc_key = _vault_token_key(vt_param)
        if enc_key is None:
            return jsonify({"error": "vault_key_expired"}), 401
    else:
        if not _vault_session_active():
            return jsonify({"error": "vault_key_expired"}), 401
        _vault_touch()
        enc_key = _vault_session_key()
        if enc_key is None:
            return jsonify({"error": "vault_key_expired"}), 401

    if not thumb_key or len(thumb_key) > 64:
        abort(400)

    _load_vault()
    with _vault_state_lock:
        entry = _vault_state["items"].get(thumb_key)
    if not entry:
        abort(404)

    # Encrypted items: decrypt the requested tier in memory with session or token key.
    if entry.get("enc"):
        fmap = {"thumb": "thumb.enc", "thumb_hq": "hq.enc",
                "thumb_preview": "thumb.enc", "thumb_max": "hq.enc"}
        encfile = os.path.join(_VAULT_ENC_DIR, thumb_key, fmap.get(tier, "thumb.enc"))
        if not os.path.exists(encfile):
            abort(404)
        try:
            with open(encfile, "rb") as fh:
                plain = _vault_decrypt(enc_key, fh.read())
        except Exception:
            abort(403)
        import io as _io2
        return send_file(_io2.BytesIO(plain), mimetype="image/jpeg", max_age=0)

    if tier == "thumb":
        path = os.path.join(_VAULT_THUMB_DIR, thumb_key + ".jpg")
        fallback = os.path.join(_THUMB_DIR, thumb_key + ".jpg")
    elif tier == "thumb_hq":
        path = os.path.join(_VAULT_THUMB_HQ_DIR, thumb_key + ".jpg")
        fallback = os.path.join(_THUMB_HQ_DIR, thumb_key + ".jpg")
    elif tier == "thumb_preview":
        path = os.path.join(_VAULT_THUMB_PRV_DIR, thumb_key + ".jpg")
        fallback = os.path.join(_THUMB_PREVIEW_DIR, thumb_key + ".jpg")
    elif tier == "thumb_max":
        path = os.path.join(_VAULT_THUMB_MAX_DIR, thumb_key + ".webp")
        fallback = os.path.join(_THUMB_MAX_DIR, thumb_key + ".webp")
    else:
        abort(400)

    serve_path = path if os.path.exists(path) else (fallback if os.path.exists(fallback) else None)
    if not serve_path:
        abort(404)

    mime = "image/webp" if serve_path.endswith(".webp") else "image/jpeg"
    return send_file(serve_path, mimetype=mime, max_age=0)


@app.route("/api/vault/thumb/<thumb_key>")
@require_auth
def vault_thumb(thumb_key):
    """Serve base thumbnail for a vaulted item."""
    return _serve_vault_thumb("thumb", thumb_key)


@app.route("/api/vault/thumb_hq/<thumb_key>")
@require_auth
def vault_thumb_hq(thumb_key):
    """Serve HQ thumbnail for a vaulted item."""
    return _serve_vault_thumb("thumb_hq", thumb_key)


@app.route("/api/vault/thumb_preview/<thumb_key>")
@require_auth
def vault_thumb_preview(thumb_key):
    """Serve preview thumbnail for a vaulted item."""
    return _serve_vault_thumb("thumb_preview", thumb_key)


@app.route("/api/vault/thumb_max/<thumb_key>")
@require_auth
def vault_thumb_max(thumb_key):
    """Serve max-quality thumbnail for a vaulted item."""
    return _serve_vault_thumb("thumb_max", thumb_key)


def _vault_resolve_real_path(raw_path):
    """Resolve a client-supplied path to the vault's on-disk location.

    v2: originals live at PHOTOS_ROOT/.vault/<rel>.  The caller supplies the
    canonical index path (/mnt/data/PHOTOS/PHOTOS/...) as stored in vault.json.

    Returns (thumb_key, real_vault_path) or (None, None) if not vaulted.
    """
    assert isinstance(raw_path, str) and raw_path, "raw_path must be non-empty"
    _load_vault()
    with _vault_state_lock:
        mapping = dict(_vault_state["items"])

    for tk, entry in mapping.items():
        if not entry:
            continue
        if entry.get("path", "") == raw_path:
            # Build vault-side real path
            _, rel = _resolve_original_for_vault(raw_path)
            if not rel:
                return None, None
            real_path = os.path.join(_VAULT_ORIGINALS_DIR, rel)
            if os.path.isfile(real_path):
                return tk, real_path
            return None, None

    return None, None


@app.route("/api/vault/download")
@require_auth
def vault_download():
    """Serve original file of a vaulted item (v2: file is in .vault/)."""
    if not _vault_session_active():
        abort(403)
    _vault_touch()

    raw_path = request.args.get("p", "").strip()
    if not raw_path:
        abort(400)

    tk, real_path = _vault_resolve_real_path(raw_path)
    if not tk or not real_path:
        abort(403)

    force_dl = request.args.get("dl") == "1"
    ext = real_path.rsplit(".", 1)[-1].lower()
    if ext in _VIDEO_MIMES and not force_dl:
        resp = send_file(real_path, mimetype=_VIDEO_MIMES[ext], as_attachment=False,
                         conditional=True, download_name=os.path.basename(real_path))
        resp.headers["Accept-Ranges"] = "bytes"
        return resp
    return send_file(real_path, as_attachment=True, download_name=os.path.basename(real_path))


@app.route("/api/vault/video")
@require_auth
def vault_video():
    """Serve video for a vaulted item (v2: file is in .vault/)."""
    if not _vault_session_active():
        abort(403)
    _vault_touch()

    raw_path = request.args.get("p", "").strip()
    if not raw_path:
        abort(400)

    tk, real_path = _vault_resolve_real_path(raw_path)
    if not tk or not real_path:
        abort(403)

    ext = real_path.rsplit(".", 1)[-1].lower()
    if ext not in _VIDEO_MIMES:
        abort(400)

    # Look for cached transcode in vault dir first
    key = _video_cache_key(real_path)
    vault_cached = os.path.join(_VAULT_VIDEO_DIR, key + ".mp4")
    pub_cached = os.path.join(_VIDEO_CACHE_DIR, key + ".mp4")

    if os.path.exists(vault_cached):
        resp = send_file(vault_cached, mimetype="video/mp4", as_attachment=False,
                         conditional=True)
        resp.headers["Accept-Ranges"] = "bytes"
        return resp

    if os.path.exists(pub_cached):
        resp = send_file(pub_cached, mimetype="video/mp4", as_attachment=False,
                         conditional=True)
        resp.headers["Accept-Ranges"] = "bytes"
        return resp

    # Fall back to original in vault dir
    mime = _VIDEO_MIMES.get(ext, "video/mp4")
    resp = send_file(real_path, mimetype=mime, as_attachment=False, conditional=True,
                     download_name=os.path.basename(real_path))
    resp.headers["Accept-Ranges"] = "bytes"
    return resp


def _vault_video_h264(key, thumb_key, plain_original, src_ext):
    """H264/mp4 bytes for a vault video. Transcodes on first request (reusing the hardened
    _transcode_to_h264 — rotation baked, stream-validated, libx264 fallback) so vault videos
    play regardless of source codec, exactly like the regular gallery. Privacy:
      • plaintext (the decrypted original + the H264 output) lives ONLY in RAM (/dev/shm),
        never on persistent disk, and is deleted immediately;
      • the transcode is cached ENCRYPTED with the vault key (vault_video_cache/<key>.h264.enc),
        so nothing decrypted ever persists. Returns None if the source can't be transcoded
        (caller then falls back to streaming the raw original)."""
    cache = os.path.join(_VAULT_VIDEO_DIR, thumb_key + ".h264.enc")
    if os.path.isfile(cache):
        try:
            with open(cache, "rb") as fh:
                return _vault_decrypt(key, fh.read())
        except Exception:
            try: os.remove(cache)
            except OSError: pass
    shm = "/dev/shm" if os.path.isdir("/dev/shm") else _VAULT_VIDEO_DIR
    src = os.path.join(shm, f"vt-{thumb_key}-src.{(src_ext or 'mov')}")
    out = os.path.join(shm, f"vt-{thumb_key}-out.mp4")
    try:
        with open(src, "wb") as fh: fh.write(plain_original)
        if not _transcode_to_h264(src, out) or not os.path.isfile(out):
            return None
        with open(out, "rb") as fh: h264 = fh.read()
        try:
            os.makedirs(_VAULT_VIDEO_DIR, exist_ok=True)
            tmpc = cache + ".part"
            with open(tmpc, "wb") as fh: fh.write(_vault_encrypt(key, h264))
            os.replace(tmpc, cache)
        except Exception:
            pass
        return h264
    except Exception:
        return None
    finally:
        for p in (src, out, out + ".part"):
            try: os.remove(p)
            except OSError: pass


@app.route("/api/vault/stream/<thumb_key>")
@require_auth
def vault_stream(thumb_key):
    """Decrypt and stream an encrypted vault video by thumb_key."""
    vt_param = request.args.get("vt", "")
    if vt_param:
        assert len(vt_param) <= 512, "bad vt"
        key = _vault_token_key(vt_param)
        if key is None:
            return jsonify({"error": "vault_key_expired"}), 401
    else:
        if not _vault_session_active():
            return jsonify({"error": "vault_key_expired"}), 401
        _vault_touch()
        key = _vault_session_key()
        if key is None:
            return jsonify({"error": "vault_key_expired"}), 401
    if not thumb_key or len(thumb_key) > 64:
        abort(400)
    _load_vault()
    with _vault_state_lock:
        entry = _vault_state["items"].get(thumb_key)
    if not entry or not entry.get("enc"):
        abort(404)
    enc_path = os.path.join(_VAULT_ENC_DIR, thumb_key, "orig.enc")
    if not os.path.isfile(enc_path):
        abort(404)
    try:
        with open(enc_path, "rb") as fh:
            plain = _vault_decrypt(key, fh.read())
    except Exception:
        abort(403)
    ext = (entry.get("path") or "").rsplit(".", 1)[-1].lower()
    dl_name = os.path.basename(entry.get("path") or thumb_key)
    mime = _VIDEO_MIMES.get(ext, "video/mp4")
    # Transcode to H264 so codecs AVPlayer can't decode (HEVC variants, VP9, etc.) still play —
    # cached encrypted, plaintext only ever in RAM. Fall back to the raw original if it can't.
    if ext in _VIDEO_MIMES:
        h264 = _vault_video_h264(key, thumb_key, plain, ext)
        if h264 is not None:
            plain = h264
            mime = "video/mp4"
            dl_name = os.path.splitext(dl_name)[0] + ".mp4"
    import io as _io
    resp = send_file(_io.BytesIO(plain), mimetype=mime, as_attachment=False,
                     conditional=True, download_name=dl_name)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = len(plain)
    return resp


# ─── End Vault ────────────────────────────────────────────────────────────────


@app.route("/api/photos/thumb-bundle")
@require_auth
def thumb_bundle():
    """Stream all thumbnails as one binary blob for the service worker to cache.
    Format per entry: [2B url_len][url bytes][4B data_len][jpeg bytes]
    `?tier=hq` returns the 800px retina tier instead of the 475px base.
    One request replaces ~30k individual thumbnail requests."""
    import struct
    tier = (request.args.get("tier") or "").strip().lower()
    if tier == "hq":
        url_key = "thumb_hq"
        src_dir = _THUMB_HQ_DIR
    else:
        url_key = "thumb"
        src_dir = _THUMB_DIR
    exclude = _get_hidden_hashes() | _get_screenshot_hashes() | _get_duplicate_hashes() | _get_vault_hashes()
    all_items = load_photo_index()
    if exclude:
        all_items = [i for i in all_items if i.get("thumb", "").rsplit("/", 1)[-1].replace(".jpg", "") not in exclude]
    images = [item for item in all_items if item.get(url_key) and item.get("type") != "video"]
    total = len(images)

    def generate():
        for item in images:
            thumb_url = item[url_key]
            name = secure_filename(thumb_url.rsplit("/", 1)[-1])
            data = _read_thumb(src_dir, name)
            if data is None:
                continue
            url_bytes = thumb_url.encode("utf-8")
            yield struct.pack(">H", len(url_bytes)) + url_bytes + struct.pack(">I", len(data)) + data

    return Response(
        stream_with_context(generate()),
        mimetype="application/octet-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Thumb-Count": str(total),
            "X-Thumb-Tier": tier or "base",
        },
    )


@app.route("/sw.js")
def service_worker():
    """Serve SW from root so it has scope over /static/thumbs/*."""
    resp = make_response(send_file(os.path.join(_APP_DIR, "static", "sw.js")))
    resp.headers["Content-Type"] = "application/javascript"
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache, no-store"
    return resp


@app.route("/api/photos/warm/<month_key>")
@require_auth
def warm_month(month_key):
    """Trigger background thumb generation for a specific month (YYYY-MM).
    Called by the frontend when a month is clicked so thumbs are ready when images load.
    """
    items = load_photo_index()
    month_items = [i for i in items if
        datetime.fromtimestamp(i["date"], tz=_GALLERY_TZ).strftime("%Y-%m") == month_key]
    if not month_items:
        return jsonify({"status": "empty"})
    threading.Thread(target=_warm_recent_months, args=(month_items, 99), daemon=True).start()
    return jsonify({"status": "warming", "count": len(month_items)})


@app.route("/photos")
@require_auth
def photos_page():
    hidden = _get_hidden_hashes()
    screenshots = _get_screenshot_hashes()
    exclude = hidden | screenshots | _get_vault_hashes()
    all_items = sorted(load_photo_index(), key=lambda x: x.get('date', 0), reverse=True)
    items = [i for i in all_items if not (exclude and i.get("thumb", "").rsplit("/", 1)[-1].replace(".jpg", "") in exclude)]
    groups = OrderedDict()
    for item in items:
        try:
            dt = datetime.fromtimestamp(item.get("date", 0), tz=_GALLERY_TZ)
        except Exception:
            continue
        month_key = dt.strftime("%Y-%m")
        if month_key not in groups:
            groups[month_key] = {
                "month": dt.strftime("%B %Y"),
                "month_key": month_key,
                "count": 0,
                "cover": item.get("thumb", ""),
            }
        groups[month_key]["count"] += 1
    # Inline the first 3 months of items so they render without an API call
    by_month = load_month_index()
    group_keys = list(groups.keys())
    if exclude:
        inline_items = {key: [i for i in by_month.get(key, []) if i.get("thumb", "").rsplit("/", 1)[-1].replace(".jpg", "") not in exclude] for key in group_keys[:3]}
    else:
        inline_items = {key: by_month.get(key, []) for key in group_keys[:3]}
    photo_data_json = json.dumps({
        "groups": list(groups.values()),
        "total": len(items),
        "inline": inline_items,
    })
    first_thumbs = [item["thumb"] for item in items[:8] if item.get("thumb")]
    resp = make_response(render_template("photos.html",
        photo_data_json=photo_data_json,
        first_thumbs=first_thumbs,
    ))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/api/photos/month/<month_key>")
@require_auth
def api_photos_month(month_key):
    """Return all items for a specific month. `?kind=video` filters to videos."""
    by_month = load_month_index()
    items = by_month.get(month_key, [])
    exclude = _get_hidden_hashes() | _get_screenshot_hashes() | _get_duplicate_hashes() | _get_vault_hashes()
    if exclude:
        items = [i for i in items if i.get("thumb", "").rsplit("/", 1)[-1].replace(".jpg", "") not in exclude]
    if (request.args.get("kind") or "").lower() == "video":
        items = [i for i in items if i.get("type") == "video"]
    return jsonify(items)


@app.route("/api/photos/all-months")
@require_auth
def api_photos_all_months():
    """Return every month's items in one shot for bulk client-side caching.

    Response is gzip-compressed (~1-2 MB) and pre-serialized so repeated
    calls are near-instant. The client fetches this once after page load
    to populate its monthCache, making all subsequent month clicks instant.
    """
    import gzip as _gzip

    mtime = photo_db.version()
    if not mtime:
        return jsonify({})

    exclude = _get_hidden_hashes() | _get_screenshot_hashes() | _get_duplicate_hashes() | _get_vault_hashes()

    # ETag over (index version, exclude-set) so unchanged libraries revalidate as an
    # empty 304 instead of a ~12 MB body — the iOS app refetches this on every open.
    # Python's set hash is per-process-random: stable for this worker's lifetime,
    # so a service restart costs each client exactly one full re-download.
    tag = f"am-{mtime}-{hash(frozenset(exclude)) & 0xffffffff:x}"
    etag = f'"{tag}"'
    # request.if_none_match holds UNQUOTED tags — compare the bare tag, not the
    # quoted header form, or every revalidation silently misses.
    if request.if_none_match.contains(tag):
        return Response(status=304, headers={"ETag": etag, "Cache-Control": "public, max-age=120"})

    if _month_json_cache["data"] is not None and _month_json_cache["mtime"] == mtime and not exclude:
        raw = _month_json_cache["data"]
    else:
        by_month = load_month_index()
        if exclude:
            by_month = {k: [i for i in v if i.get("thumb", "").rsplit("/", 1)[-1].replace(".jpg", "") not in exclude] for k, v in by_month.items()}
        raw = json.dumps(by_month).encode("utf-8")
        if not exclude:
            _month_json_cache["data"] = raw
            _month_json_cache["mtime"] = mtime

    if "gzip" in request.headers.get("Accept-Encoding", ""):
        compressed = _gzip.compress(raw, compresslevel=4)
        return Response(compressed, mimetype="application/json", headers={
            "Content-Encoding": "gzip",
            "Cache-Control": "public, max-age=120",
            "ETag": etag,
        })
    return Response(raw, mimetype="application/json", headers={
        "Cache-Control": "public, max-age=120",
        "ETag": etag,
    })


@app.route("/api/photos/trash/list")
@require_auth
def api_photos_trash_list():
    return jsonify(_trash_review.list_photo_trash(str(_TRASH_DIR)))


@app.route("/api/photos/trash/thumb/<path:trash_name>")
@require_auth
def api_photos_trash_thumb(trash_name):
    tdir = str(_TRASH_DIR)
    thumb = _trash_review.trash_thumb_path(tdir, trash_name)
    src = _trash_review.original_file_path(tdir, trash_name)
    root = os.path.realpath(tdir)
    if not (os.path.realpath(src).startswith(root + os.sep)
            and os.path.realpath(thumb).startswith(root + os.sep)):
        return "not found", 404  # path traversal via ".." in trash_name
    if not os.path.exists(thumb):
        if not os.path.exists(src):
            return "not found", 404
        os.makedirs(os.path.dirname(thumb), exist_ok=True)
        try:
            from PIL import Image
            im = Image.open(src); im.thumbnail((475, 475))
            im.convert("RGB").save(thumb, "JPEG", quality=80)
        except Exception:
            return "thumb failed", 415  # e.g. video without a still — frontend shows a placeholder
    return send_file(thumb, mimetype="image/jpeg")

@app.route("/api/photos/trash/restore", methods=["POST"])
@require_auth
def api_photos_trash_restore():
    tn = (request.json or {}).get("trash_name", "")
    res = restore_trash(tn)
    if res.get("success"):
        t = _trash_review.trash_thumb_path(str(_TRASH_DIR), tn)
        if os.path.exists(t):
            os.remove(t)
        _photo_cache["data"] = None; _month_cache["data"] = None
    return jsonify(res)

@app.route("/api/photos/trash/purge", methods=["POST"])
@require_auth
def api_photos_trash_purge():
    tn = (request.json or {}).get("trash_name", "")
    return jsonify(_trash_review.purge_item(str(_TRASH_DIR), tn))

@app.route("/api/photos/trash/empty", methods=["POST"])
@require_auth
def api_photos_trash_empty():
    return jsonify(_trash_review.empty_bin(str(_TRASH_DIR)))


_blurhash_cache = {"raw": None, "gz": None, "mtime": 0}

@app.route("/api/photos/blurhashes")
@require_auth
def api_photos_blurhashes():
    """{thumb_hash: blurhash} map for progressive blurred placeholders. Fetched lazily by the
    clients AFTER first paint (not part of the critical all-months path), so it never slows the
    grid. Static file generated by gen_blurhashes.py; served gzipped + ETag/304."""
    import gzip as _gzip
    path = os.path.join(_APP_DIR, "blurhashes.json")
    try:
        mtime = int(os.path.getmtime(path))
    except OSError:
        return jsonify({})
    etag = f'"bh-{mtime}"'
    if request.if_none_match.contains(f"bh-{mtime}"):
        return Response(status=304, headers={"ETag": etag, "Cache-Control": "public, max-age=300"})
    if _blurhash_cache["mtime"] != mtime:
        raw = open(path, "rb").read()
        _blurhash_cache.update(raw=raw, gz=_gzip.compress(raw, compresslevel=6), mtime=mtime)
    if "gzip" in request.headers.get("Accept-Encoding", ""):
        return Response(_blurhash_cache["gz"], mimetype="application/json", headers={
            "Content-Encoding": "gzip", "Cache-Control": "public, max-age=300", "ETag": etag})
    return Response(_blurhash_cache["raw"], mimetype="application/json", headers={
        "Cache-Control": "public, max-age=300", "ETag": etag})


@app.route("/api/photos")
@require_auth
def api_photos():
    page = request.args.get("page", 0, type=int)
    per_page = 200
    exclude = _get_hidden_hashes() | _get_screenshot_hashes() | _get_duplicate_hashes() | _get_vault_hashes()
    all_idx = sorted(load_photo_index(), key=lambda x: x.get('date', 0), reverse=True)
    if exclude:
        items = [i for i in all_idx if i.get("thumb", "").rsplit("/", 1)[-1].replace(".jpg", "") not in exclude]
    else:
        items = all_idx
    total = len(items)
    start = page * per_page
    end = start + per_page
    page_items = items[start:end]

    # Preload this page + next page thumbnails into RAM in background
    preload_slice = items[start:end + per_page]
    threading.Thread(target=_preload_thumb_batch, args=(preload_slice,), daemon=True).start()

    # Group by month
    groups = OrderedDict()
    for item in page_items:
        dt = datetime.fromtimestamp(item["date"], tz=_GALLERY_TZ)
        month_key = dt.strftime("%Y-%m")
        month_label = dt.strftime("%B %Y")
        if month_key not in groups:
            groups[month_key] = {"month": month_label, "month_key": month_key, "items": []}
        groups[month_key]["items"].append(item)

    return jsonify({"groups": list(groups.values()), "total": total, "page": page})


@app.route("/api/photos/all")
@require_auth
def api_photos_all():
    """Return every photo grouped by month in one shot — for LAN use where latency is negligible."""
    exclude = _get_hidden_hashes() | _get_screenshot_hashes() | _get_duplicate_hashes() | _get_vault_hashes()
    all_items = sorted(load_photo_index(), key=lambda x: x.get('date', 0), reverse=True)
    if exclude:
        items = [i for i in all_items if i.get("thumb", "").rsplit("/", 1)[-1].replace(".jpg", "") not in exclude]
    else:
        items = all_items
    total = len(items)

    # Preload all thumbnails into RAM in background
    threading.Thread(target=_preload_thumb_batch, args=(items,), daemon=True).start()

    groups = OrderedDict()
    for item in items:
        dt = datetime.fromtimestamp(item["date"], tz=_GALLERY_TZ)
        month_key = dt.strftime("%Y-%m")
        month_label = dt.strftime("%B %Y")
        if month_key not in groups:
            groups[month_key] = {"month": month_label, "month_key": month_key, "items": []}
        groups[month_key]["items"].append(item)

    return jsonify({"groups": list(groups.values()), "total": total})


_CAMERA_FOLDERS = {"S95", "RX100", "GX9", "FUJI", "PIXPRO"}


def _is_camera_source(path):
    """True if the photo came from a digital camera folder (not phone)."""
    parts = path.upper().split("/")
    return any(p in _CAMERA_FOLDERS for p in parts)


def _is_landscape(thumb_url):
    """Check if a thumbnail is landscape using the pre-built index."""
    if not _landscape_index_ready or not thumb_url:
        return False
    name = thumb_url.rsplit("/", 1)[-1]
    return name in _landscape_thumbs


def _pick_covers(candidates, max_covers=6):
    """Pick cover photos preferring landscape camera shots.

    Priority: landscape camera > landscape phone > any camera > fallback.
    """
    import random

    cam = [c for c in candidates if c["is_camera"]]
    phone = [c for c in candidates if not c["is_camera"]]
    random.shuffle(cam)
    random.shuffle(phone)

    landscape_cam = [c["thumb"] for c in cam if _is_landscape(c["thumb"])]
    landscape_phone = [c["thumb"] for c in phone if _is_landscape(c["thumb"])]

    pool = landscape_cam[:max_covers]
    if len(pool) < max_covers:
        pool.extend(landscape_phone[:max_covers - len(pool)])
    if len(pool) < max_covers:
        cam_thumbs = [c["thumb"] for c in cam if c["thumb"] not in pool]
        pool.extend(cam_thumbs[:max_covers - len(pool)])
    if len(pool) < max_covers:
        remaining = [c["thumb"] for c in candidates if c["thumb"] not in pool]
        random.shuffle(remaining)
        pool.extend(remaining[:max_covers - len(pool)])

    random.shuffle(pool)
    return pool[:max_covers]


def _clamp_ar(ar):
    """Aspect ratio clamped to the same range as the frontend itemAr(), so the
    per-month sumar matches the layout. Missing/invalid → 1.0 (square)."""
    try:
        ar = float(ar)
    except (TypeError, ValueError):
        return 1.0
    if not (ar > 0):
        return 1.0
    return max(0.45, min(2.8, ar))


@app.route("/api/photos/summary")
@require_auth
def api_photos_summary():
    """Pre-computed month/year summary, cached by index mtime.
    `?kind=video` returns a videos-only view (months that contain at least
    one video, count = video count, covers picked from video thumbs)."""
    kind = (request.args.get("kind") or "").lower()
    mtime = photo_db.version()
    if not mtime:
        return jsonify({"months": [], "years": [], "total": 0})
    cache_key = "kind:" + kind
    if (_summary_cache.get("data_" + cache_key) is not None
            and _summary_cache.get("mtime_" + cache_key) == mtime):
        return jsonify(_summary_cache["data_" + cache_key])
    if kind == "video":
        items_all = load_photo_index()
        items = [i for i in items_all if i.get("type") == "video"]
        months = OrderedDict()
        for item in items:
            try:
                dt = datetime.fromtimestamp(item["date"], tz=_GALLERY_TZ)
            except Exception:
                continue
            mk = dt.strftime("%Y-%m")
            if mk not in months:
                months[mk] = {"month_key": mk, "month": dt.strftime("%B %Y"),
                              "year": dt.year, "count": 0, "sumar": 0.0, "covers": []}
            months[mk]["count"] += 1
            months[mk]["sumar"] += _clamp_ar(item.get("ar"))
            thumb = item.get("thumb_hq") or item.get("thumb", "")
            if thumb and len(months[mk]["covers"]) < 6:
                months[mk]["covers"].append(thumb)
        years = OrderedDict()
        for mk, mo in months.items():
            yk = str(mo["year"])
            if yk not in years:
                years[yk] = {"year": mo["year"], "count": 0, "covers": [], "months": 0}
            years[yk]["count"] += mo["count"]
            years[yk]["months"] += 1
        sorted_months = sorted(months.values(), key=lambda m: m["month_key"], reverse=True)
        sorted_years = sorted(years.values(), key=lambda y: y["year"], reverse=True)
        result = {"months": sorted_months, "years": sorted_years,
                  "total": len(items), "videos": len(items)}
        _summary_cache["data_" + cache_key] = result
        _summary_cache["mtime_" + cache_key] = mtime
        return jsonify(result)
    if _summary_cache["data"] is not None and _summary_cache["mtime"] == mtime:
        return jsonify(_summary_cache["data"])

    items = load_photo_index()
    _summary_exclude = _get_hidden_hashes() | _get_screenshot_hashes() | _get_duplicate_hashes() | _get_vault_hashes()
    if _summary_exclude:
        items = [i for i in items if i.get("thumb", "").rsplit("/", 1)[-1].replace(".jpg", "") not in _summary_exclude]
    months = OrderedDict()
    month_candidates = {}  # month_key -> list of {thumb, is_camera}
    MAX_COVERS = 6
    # Photos view includes BOTH images and videos, interleaved by date — so a
    # video-only month still appears (the old two-loop form dropped any month
    # that had no photo). Videos also seed month covers as a fallback.
    for item in items:
        try:
            dt = datetime.fromtimestamp(item["date"], tz=_GALLERY_TZ)
        except Exception:
            continue
        mk = dt.strftime("%Y-%m")
        if mk not in months:
            months[mk] = {
                "month_key": mk,
                "month": dt.strftime("%B %Y"),
                "year": dt.year,
                "count": 0,
                "sumar": 0.0,
                "covers": [],
            }
            month_candidates[mk] = []
        months[mk]["count"] += 1
        months[mk]["sumar"] += _clamp_ar(item.get("ar"))
        thumb = item.get("thumb_hq") or item.get("thumb", "")
        if thumb:
            month_candidates[mk].append({
                "thumb": thumb,
                "is_camera": _is_camera_source(item.get("path", "")),
            })

    # Pick covers using CLIP aesthetic scoring (falls back to landscape heuristic)
    for mk, cands in month_candidates.items():
        months[mk]["covers"] = _pick_aesthetic_covers(cands, MAX_COVERS)

    # Build year summary
    years = OrderedDict()
    for mk, mo in months.items():
        yk = str(mo["year"])
        if yk not in years:
            years[yk] = {"year": mo["year"], "count": 0, "covers": [], "months": 0, "_candidates": []}
        years[yk]["count"] += mo["count"]
        years[yk]["months"] += 1
        years[yk]["_candidates"].extend(month_candidates.get(mk, []))

    for yk, yr in years.items():
        yr["covers"] = _pick_aesthetic_covers(yr["_candidates"], MAX_COVERS)
        del yr["_candidates"]

    # Sort newest first
    sorted_months = sorted(months.values(), key=lambda m: m["month_key"], reverse=True)
    sorted_years = sorted(years.values(), key=lambda y: y["year"], reverse=True)

    photo_count = sum(1 for i in items if i.get("type") != "video")
    video_count = sum(1 for i in items if i.get("type") == "video")
    result = {
        "months": sorted_months,
        "years": sorted_years,
        "total": len(items),
        "photos": photo_count,
        "videos": video_count,
    }
    _summary_cache["data"] = result
    _summary_cache["mtime"] = mtime
    return jsonify(result)


PATH_ALIASES = [
    ("/srv/mergerfs/PROMETHEUS/", "/Volumes/PROMETHEUS/"),
]

MEDIA_CACHE_MAX_AGE = 86400 * 7  # 7 days


def resolve_media_path(filepath):
    """Translate path aliases before hitting the filesystem."""
    for src, dst in PATH_ALIASES:
        if filepath.startswith(src):
            return dst + filepath[len(src):]
        elif filepath.startswith(dst):
            return src + filepath[len(dst):]
    return None


@app.route("/media/<path:filepath>")
@require_auth
def serve_media(filepath):
    import mimetypes
    filepath = "/" + filepath
    # Try alias first to avoid slow stat() on non-existent mount paths
    alt = resolve_media_path(filepath)
    if alt and os.path.isfile(alt):
        filepath = alt
    elif not os.path.isfile(filepath):
        abort(404)
    abs_path = os.path.realpath(filepath)
    allowed = ["/srv/mergerfs/PROMETHEUS/PHOTOS/", "/Volumes/PROMETHEUS/PHOTOS/"]
    if not any(abs_path.startswith(prefix) for prefix in allowed):
        abort(403)
    # X-Accel-Redirect: let nginx serve file directly (zero-copy sendfile)
    # Falls back to Flask send_file when not behind nginx
    if os.environ.get("NGINX_ACCEL"):
        mime = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
        resp = make_response("")
        resp.headers["X-Accel-Redirect"] = "/internal-media" + abs_path
        resp.headers["Content-Type"] = mime
        resp.headers["Cache-Control"] = f"public, max-age={MEDIA_CACHE_MAX_AGE}"
        return resp
    return send_file(filepath, conditional=True, max_age=MEDIA_CACHE_MAX_AGE)




# ─── Download tokens (one-time URLs so browser handles download natively) ───
_dl_tokens      = {}  # token -> {"path": str, "expires": float}         (single file)
_dl_zip_tokens  = {}  # token -> {"files": [(path, name)], "expires": float}  (zip)
_dl_tokens_lock = threading.Lock()


def _prune_tokens():
    """Remove expired tokens."""
    now = time.time()
    with _dl_tokens_lock:
        for t in [t for t, v in _dl_tokens.items() if v["expires"] < now]:
            _dl_tokens.pop(t)
        for t in [t for t, v in _dl_zip_tokens.items() if v["expires"] < now]:
            _dl_zip_tokens.pop(t)


def _streaming_zip(files):
    """
    Generator that yields raw bytes of a valid ZIP file without any temp file.
    Uses data descriptors (flag bit 3) so CRC/sizes are written after each file,
    allowing true streaming with no seek-back.
    files: list of (real_path, arcname) tuples.
    """
    import struct, zlib as _zlib

    central = []
    pos = 0

    for real_path, arcname in files:
        try:
            stat = os.stat(real_path)
        except OSError:
            continue
        name_b = arcname.encode("utf-8")
        t = time.localtime(stat.st_mtime)
        dos_time = (t.tm_sec >> 1) | (t.tm_min << 5) | (t.tm_hour << 11)
        dos_date = t.tm_mday | (t.tm_mon << 5) | ((t.tm_year - 1980) << 9)

        # Local file header — CRC/sizes are 0; data descriptor follows file data
        lf = struct.pack(
            "<4sHHHHHIIIHH",
            b"PK\x03\x04", 20, 0x0808, 0,
            dos_time, dos_date, 0, 0, 0,
            len(name_b), 0,
        ) + name_b
        yield lf
        file_offset = pos
        pos += len(lf)

        crc, size = 0, 0
        try:
            with open(real_path, "rb") as fh:
                while True:
                    chunk = fh.read(1 << 16)
                    if not chunk:
                        break
                    crc = _zlib.crc32(chunk, crc) & 0xFFFFFFFF
                    size += len(chunk)
                    pos += len(chunk)
                    yield chunk
        except OSError:
            pass

        dd = struct.pack("<4sIII", b"PK\x07\x08", crc, size, size)
        yield dd
        pos += len(dd)

        central.append((name_b, dos_time, dos_date, crc, size, file_offset))

    cd_offset, cd_size = pos, 0
    for name_b, dos_time, dos_date, crc, size, offset in central:
        cde = struct.pack(
            "<4sHHHHHHIIIHHHHHII",
            b"PK\x01\x02", 20, 20, 0x0808, 0,
            dos_time, dos_date, crc, size, size,
            len(name_b), 0, 0, 0, 0, 0, offset,
        ) + name_b
        yield cde
        cd_size += len(cde)

    yield struct.pack(
        "<4sHHHHIIH",
        b"PK\x05\x06", 0, 0,
        len(central), len(central),
        cd_size, cd_offset, 0,
    )


def _resolve_photo_path(p):
    if os.path.isfile(p):
        return p
    for src, dst in PATH_ALIASES:
        if p.startswith(src):
            alt = dst + p[len(src):]
            if os.path.isfile(alt):
                return alt
        elif p.startswith(dst):
            alt = src + p[len(dst):]
            if os.path.isfile(alt):
                return alt
    # photo_index has stale paths with a duplicated "/PHOTOS/PHOTOS/" segment;
    # actual files live at /mnt/data/PROMETHEUS/PHOTOS/<...> (and /mnt/data/PHOTOS
    # is a symlink to it). Try both rewrites before giving up.
    if "/PHOTOS/PHOTOS/" in p:
        alt = p.replace("/PHOTOS/PHOTOS/", "/PHOTOS/", 1)
        if os.path.isfile(alt):
            return alt
    if p.startswith("/mnt/data/PHOTOS/"):
        alt = "/mnt/data/PROMETHEUS/PHOTOS/" + p[len("/mnt/data/PHOTOS/"):]
        if os.path.isfile(alt):
            return alt
    return None


# Deferred vault startup: _vault_migrate_v1 uses _resolve_photo_path so it
# must run after that function is defined.
_vault_ensure_initialized()
if os.path.exists(_VAULT_PATH):
    _load_vault()
    _vault_migrate_v1()

_PHOTO_ALLOWED = [
    "/srv/mergerfs/PROMETHEUS/PHOTOS/",
    "/Volumes/PROMETHEUS/PHOTOS/",
    "/mnt/data/PHOTOS/",
    "/mnt/data/PROMETHEUS/PHOTOS/",
]


@app.route("/api/photos/download-zip")
@require_auth
def download_photos_zip():
    """Stream a zip of selected photos. Paths come as repeated ?p= query params."""
    paths = request.args.getlist("p")
    if not paths:
        abort(400)
    if len(paths) > 500:
        abort(400)
    valid = []
    seen_names = {}
    for raw_path in paths:
        rp = _resolve_photo_path(raw_path)
        if not rp:
            continue
        if not any(os.path.abspath(rp).startswith(pfx) for pfx in _PHOTO_ALLOWED):
            continue
        name = os.path.basename(rp)
        if name in seen_names:
            seen_names[name] += 1
            base, ext = os.path.splitext(name)
            name = f"{base}_{seen_names[name]}{ext}"
        else:
            seen_names[name] = 0
        valid.append((rp, name))
    if not valid:
        abort(400)
    return Response(
        stream_with_context(_streaming_zip(valid)),
        mimetype="application/zip",
        headers={"Content-Disposition": "attachment; filename=photos.zip"},
    )


def _to_jpeg_bytes(src_path):
    """Convert any image (including HEIC) to JPEG bytes. Returns None on failure."""
    try:
        from PIL import Image
        import io
        ext = src_path.rsplit(".", 1)[-1].lower()
        if ext in ("heic", "heif"):
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except ImportError:
                pass
        img = Image.open(src_path)
        img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        buf.seek(0)
        return buf
    except Exception:
        return None


_VIDEO_MIMES = {
    "mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime",
    "webm": "video/webm", "mkv": "video/x-matroska", "avi": "video/x-msvideo",
    "3gp": "video/3gpp",
}

# ─── NVENC web-friendly video cache ───
# HEVC / large originals get transcoded once to H.264 + AAC + faststart MP4
# (fits Safari, Chrome, mobile, with the moov atom upfront for instant
# playback start). Cache lives next to thumbnails on NVMe.
_VIDEO_CACHE_DIR = os.path.join(_APP_DIR, "static", "video_cache")
os.makedirs(_VIDEO_CACHE_DIR, exist_ok=True)
_HLS_CACHE_DIR = os.path.join(_APP_DIR, "static", "hls")
os.makedirs(_HLS_CACHE_DIR, exist_ok=True)
_video_transcode_locks = {}
_video_transcode_locks_mutex = threading.Lock()
_NVENC_AVAILABLE = None  # lazy-detected


def _nvenc_available():
    """Returns 'nvenc', 'libx264', or None. Tries RTX 3080 NVENC first."""
    global _NVENC_AVAILABLE
    if _NVENC_AVAILABLE is not None:
        return _NVENC_AVAILABLE
    import subprocess
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                           capture_output=True, timeout=3)
        has_nvenc = b"h264_nvenc" in r.stdout
        has_x264  = b"libx264"    in r.stdout
    except Exception as e:
        print(f"[video] encoder detection failed: {e}")
        _NVENC_AVAILABLE = None
        return None
    if has_nvenc:
        test = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "nullsrc=s=256x256:d=0.1",
             "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=10)
        if test.returncode == 0:
            _NVENC_AVAILABLE = "nvenc"
            print("[video] RTX 3080 NVENC active — GPU transcoding enabled.")
            return "nvenc"
        print("[video] h264_nvenc present but GPU not ready; falling back to CPU.")
    if has_x264:
        _NVENC_AVAILABLE = "libx264"
        print("[video] libx264 active — CPU transcoding enabled.")
        return "libx264"
    _NVENC_AVAILABLE = None
    print("[video] no usable encoder found.")
    return None

def _video_cache_key(src_path):
    """Stable hash of (path, mtime, size) so re-uploads invalidate."""
    import hashlib
    try:
        st = os.stat(src_path)
        h = hashlib.sha1(f"{os.path.abspath(src_path)}|{int(st.st_mtime)}|{st.st_size}".encode()).hexdigest()
        return h[:24]
    except OSError:
        return hashlib.sha1(os.path.abspath(src_path).encode()).hexdigest()[:24]


def _video_rotation_flag(path):
    """Degrees of rotation metadata (display matrix / rotate tag), 0 if none.
    iPhone videos record landscape pixels + a rotation flag. The full-GPU pipeline
    (-hwaccel_output_format cuda) can't run ffmpeg's autorotate (rotation filters are
    CPU-only), so it bakes landscape pixels + a flag — and HLS then plays them sideways.
    A file that STILL has a flag after transcode is an old broken cache to re-bake."""
    import subprocess
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream_side_data=rotation:stream_tags=rotate",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, timeout=8)
        for tok in (r.stdout or b"").decode().split():
            try:
                if int(float(tok)) % 360 != 0:
                    return int(float(tok))
            except ValueError:
                pass
    except Exception:
        pass
    return 0


def _transcode_to_h264(src_path, dest_path):
    """Run ffmpeg with NVENC. Synchronous. Returns True on success."""
    import subprocess
    if _gpu_on_loan():
        return False  # GPU lent to a VM — never CPU-transcode while gaming; prewarm recovers after.
    if not _nvenc_available():
        return False
    # Unique per-writer .part — a shared "<key>.part" lets two racing transcodes
    # (e.g. a missed lock) interleave-write one temp and race os.replace. Still
    # "*.part", so _video_cache_cleanup_partials sweeps it on restart.
    tmp = dest_path + f".{os.getpid()}.{threading.get_ident()}.part"

    def _cmd(gpu_decode, encoder=None):
        """ffmpeg argv. gpu_decode=True keeps decode+scale on the 3080 too.
        encoder='libx264' forces a pure-CPU encode (no NVENC session) — the fallback when
        the GPU encoder is saturated (concurrent transcodes exhaust NVENC sessions → the
        nvenc attempts fail fast; libx264 always succeeds on a valid source, just slower)."""
        if gpu_decode and encoder is None and _nvenc_available() == "nvenc":
            return [
                "ffmpeg", "-y", "-loglevel", "error",
                # Full-GPU pipeline: NVDEC decode -> scale_cuda -> NVENC.
                # -extra_hw_frames avoids the historical "No decoder surfaces
                # left" failures on iPhone HEVC; format=yuv420p forces 8-bit
                # output so 10-bit HDR sources stay browser-playable.
                "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                "-extra_hw_frames", "8",
                "-i", src_path,
                "-map", "0:v:0?", "-map", "0:a:0?",
                "-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "26",
                "-vf", "scale_cuda=1920:1920:force_original_aspect_ratio=decrease:force_divisible_by=2:format=yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                "-max_muxing_queue_size", "1024",
                "-f", "mp4",
                tmp,
            ]
        enc = encoder or ("h264_nvenc" if _nvenc_available() == "nvenc" else "libx264")
        is_nvenc = (enc == "h264_nvenc")
        return [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", src_path,
            "-map", "0:v:0?", "-map", "0:a:0?",
            "-c:v", enc,
            "-preset", "p4" if is_nvenc else "veryfast",
            *(["-rc", "vbr", "-cq", "26"] if is_nvenc else ["-crf", "26"]),
            "-pix_fmt", "yuv420p",
            # Cap longest dimension at 1920 while preserving aspect; even-pixel.
            "-vf", "scale='if(gt(iw,ih),min(1920,iw),-2)':\'if(gt(iw,ih),-2,min(1920,ih))\',format=yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-max_muxing_queue_size", "1024",
            *(["-threads", "4"] if not is_nvenc else []),
            "-f", "mp4",
            tmp,
        ]

    # Rotated sources MUST use CPU decode: the full-GPU path can't autorotate, so it would
    # bake landscape pixels + a flag that HLS drops -> sideways video. CPU decode autorotates
    # into the pixels (no flag), so playback is correct regardless of container.
    rotated = _video_rotation_flag(src_path) != 0
    MIN_VALID_BYTES = 50 * 1024

    def _output_valid():
        # ffmpeg sometimes exits 0 while writing a tiny/streamless file (odd HEVC, data
        # streams, moov-in-free-atom). Accept ONLY a real >50KB mp4 with a decodable video
        # stream — otherwise the attempt is a failure regardless of returncode.
        try:
            if os.path.getsize(tmp) < MIN_VALID_BYTES:
                return False
        except OSError:
            return False
        vp = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", tmp],
            capture_output=True, timeout=15)
        return vp.returncode == 0 and bool((vp.stdout or b"").strip())

    # Attempts, in order of speed: full-GPU (non-rotated only) → CPU-decode+NVENC → pure CPU
    # libx264. Each is validated for a real output; a rc=0-but-garbage result (the 262-byte
    # empty file some rotated HEVCs produced) falls through to the next, so libx264 — which
    # always works on a valid source — is the guaranteed backstop.
    attempts = [
        {"gpu_decode": not rotated},
        {"gpu_decode": False},
        {"gpu_decode": False, "encoder": "libx264"},
    ]
    try:
        for i, kw in enumerate(attempts):
            proc = subprocess.run(_cmd(**kw), capture_output=True, timeout=900)
            if proc.returncode == 0 and _output_valid():
                os.replace(tmp, dest_path)
                return True
            try: os.remove(tmp)
            except OSError: pass
            if i < len(attempts) - 1:
                print(f"[video] attempt {i+1} ({kw}) failed for {src_path}; trying next")
        print(f"[video] all transcode attempts failed for {src_path}: "
              f"{(proc.stderr or b'').decode()[-400:]}")
        return False
    except subprocess.TimeoutExpired:
        try: os.remove(tmp)
        except OSError: pass
        print(f"[video] transcode timed out for {src_path}")
        return False
    except Exception as e:
        try: os.remove(tmp)
        except OSError: pass
        print(f"[video] transcode error for {src_path}: {e}")
        return False


def _video_already_web_friendly(rp):
    """True if rp is h264 in mp4 — can be served as-is."""
    assert rp, "empty path"
    assert os.path.isabs(rp), "rp must be absolute"
    if not rp.lower().endswith(".mp4"):
        return False
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", rp],
            capture_output=True, timeout=4)
        return (r.stdout or b"").decode().strip().lower() == "h264"
    except Exception:
        return False


def _ensure_video_cached(item):
    """Transcode item if needed. Returns 'done'|'skipped'|'failed'.
    No longer skips 'web-friendly' h264 mp4 — the cache transcode is
    much smaller (1920px capped) and serves uniformly faster than the
    original through Werkzeug under load."""
    assert isinstance(item, dict), "item must be dict"
    rp = _resolve_photo_path(item.get("path", ""))
    if not rp or not any(os.path.abspath(rp).startswith(p) for p in _PHOTO_ALLOWED):
        return "skipped"
    ext = rp.rsplit(".", 1)[-1].lower()
    if ext not in _VIDEO_MIMES:
        return "skipped"
    key = _video_cache_key(rp)
    cached = os.path.join(_VIDEO_CACHE_DIR, key + ".mp4")
    if os.path.exists(cached):
        return "skipped"
    with _video_transcode_locks_mutex:
        lock = _video_transcode_locks.setdefault(key, threading.Lock())
    with lock:
        if os.path.exists(cached):
            return "skipped"
        ok = _transcode_to_h264(rp, cached)
    return "done" if ok else "failed"


_VIDEO_PREWARM_INTERVAL = 300  # 5 min between full re-scans
_VIDEO_PREWARM_MAX_PASSES = 1_000_000  # bounded — runs for years of passes


def _video_prewarm_pass(pass_idx):
    """One full pass over the photo index. Idempotent — _ensure_video_cached
    skips already-cached and already-web-friendly videos so a re-run only
    does work for newly-added or newly-modified videos."""
    if _gpu_on_loan():
        print(f"[video-prewarm] pass={pass_idx} skipped — GPU on loan to a VM (deferring CPU transcode)")
        return {"done": 0, "skipped": 0, "failed": 0}
    items = load_photo_index()
    assert isinstance(items, list), "photo index must be list"
    videos = [i for i in items if i.get("type") == "video"]
    MAX_VIDEOS = 50000
    n = min(len(videos), MAX_VIDEOS)
    counts = {"done": 0, "skipped": 0, "failed": 0}
    counts_lock = threading.Lock()
    progress = {"i": 0}
    def _do(item):
        try:
            outcome = _ensure_video_cached(item)
            if outcome in ("done", "skipped"):
                rp = _resolve_photo_path(item.get("path", ""))
                if rp:
                    key = _video_cache_key(rp)
                    cached = os.path.join(_VIDEO_CACHE_DIR, key + ".mp4")
                    hls_dir = os.path.join(_HLS_CACHE_DIR, key)
                    if os.path.exists(cached) and not os.path.exists(os.path.join(hls_dir, "index.m3u8")):
                        # HLS (re)generation needed — gate the rotation probe here so it runs
                        # once per video during regen, not on every 5-min pass. Re-bake caches
                        # left rotated by the pre-fix GPU path (see serve_hls_playlist).
                        if _video_rotation_flag(cached) != 0:
                            try: os.remove(cached)
                            except OSError: pass
                            _transcode_to_h264(rp, cached)
                        _generate_hls(cached, hls_dir)
        except Exception as e:
            print(f"[video-prewarm] error on {item.get('path','?')}: {e}")
            outcome = "failed"
        with counts_lock:
            counts[outcome] = counts.get(outcome, 0) + 1
            progress["i"] += 1
            if progress["i"] % 100 == 0:
                print(f"[video-prewarm] pass={pass_idx} {progress['i']}/{n} {dict(counts)}", flush=True)
        if outcome == "done" and _nvenc_available() == "nvenc":
            time.sleep(2)  # throttle GPU — 2s between encodes prevents power spike
    # 2 workers leaves NVENC headroom for on-demand clicks (progressive
    # transcode + the user's own play). 6 workers saturated NVENC and made
    # a clicked uncached video wait 20+s for a free encoder session.
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(_do, videos[:n]))
    print(f"[video-prewarm] pass {pass_idx} complete: {counts}", flush=True)
    return counts


def _video_cache_prune():
    """Delete cached transcodes whose source video is no longer in the
    index (file deleted, renamed, or rescanned with new mtime)."""
    items = load_photo_index()
    valid_keys = set()
    for it in items:
        if it.get("type") != "video":
            continue
        rp = _resolve_photo_path(it.get("path", ""))
        if rp:
            valid_keys.add(_video_cache_key(rp))
    if not valid_keys:
        return  # paranoid no-op if index is empty/broken
    pruned = 0
    try:
        cached_files = os.listdir(_VIDEO_CACHE_DIR)
    except OSError:
        return
    MAX_PRUNE = 50000
    for fname in cached_files[:MAX_PRUNE]:
        if not fname.endswith(".mp4"):
            continue
        key = fname[:-4]
        if key not in valid_keys:
            try:
                os.remove(os.path.join(_VIDEO_CACHE_DIR, fname))
                pruned += 1
            except OSError:
                pass
    if pruned:
        print(f"[video-prewarm] pruned {pruned} orphaned cache files")


def _video_cache_cleanup_partials():
    """Remove .part files left over from killed ffmpeg jobs (service
    restarts, OOMs). Also nuke any committed .mp4 < 50KB — those are old
    corrupt entries from before the post-transcode sanity check landed."""
    MIN_VALID_BYTES = 50 * 1024
    pruned_part = 0
    pruned_tiny = 0
    try:
        files = os.listdir(_VIDEO_CACHE_DIR)
    except OSError:
        return
    MAX_FILES = 100000
    for fname in files[:MAX_FILES]:
        p = os.path.join(_VIDEO_CACHE_DIR, fname)
        if fname.endswith(".part"):
            try: os.remove(p); pruned_part += 1
            except OSError: pass
        elif fname.endswith(".mp4"):
            try:
                if os.path.getsize(p) < MIN_VALID_BYTES:
                    os.remove(p); pruned_tiny += 1
            except OSError: pass
    # HLS leaves "<key>.part" DIRECTORIES when ffmpeg is killed mid-segmentation
    # (service restart, OOM, watcher auto-restart on .py change). The .mp4 sweep
    # above never touches _HLS_CACHE_DIR, so those orphan dirs accumulate forever.
    pruned_hls = 0
    try:
        import shutil
        for fname in os.listdir(_HLS_CACHE_DIR)[:MAX_FILES]:
            if fname.endswith(".part"):
                shutil.rmtree(os.path.join(_HLS_CACHE_DIR, fname), ignore_errors=True)
                pruned_hls += 1
    except OSError:
        pass
    if pruned_part or pruned_tiny or pruned_hls:
        print(f"[video-prewarm] startup cleanup: {pruned_part} .part, {pruned_tiny} corrupt .mp4, {pruned_hls} orphan hls dirs removed", flush=True)


def _video_prewarm_loop():
    """Recurring NVENC pre-transcode. First pass catches everything,
    every _VIDEO_PREWARM_INTERVAL seconds we re-scan to catch newly added
    videos and prune orphans. Runs forever (bounded loop count)."""
    time.sleep(120)  # let system settle before GPU load
    _video_cache_cleanup_partials()
    if _nvenc_available() is None:
        print("[video-prewarm] no encoder available; skipping pre-warm")
        return
    for pass_idx in range(1, _VIDEO_PREWARM_MAX_PASSES + 1):
        try:
            _video_prewarm_pass(pass_idx)
            _video_cache_prune()
        except Exception as e:
            print(f"[video-prewarm] pass {pass_idx} crashed: {e}")
        time.sleep(_VIDEO_PREWARM_INTERVAL)


def _sysinfo_prewarm():
    """One-shot at startup: populate the system-info caches so the first user
    request doesn't pay the cold SSH cost (~2.6s: Proxmox-host CPU sample +
    offline-GPU connect timeout). The caches are stale-while-revalidate, so
    every request after this returns instantly while refreshing in background."""
    try:
        get_system_info()
    except Exception as e:
        print(f"[sysinfo-prewarm] {e}")


def _stream_progressive_transcode(rp):
    """Pipe a fragmented MP4 from NVENC straight to the client. Browser
    starts playing within ~1s as the encode runs; no full-file wait. No
    Range/seek (the browser gets a 200 with chunked body), but the cached
    follow-up transcode produces a fully seekable file for next click."""
    assert rp and os.path.isabs(rp), "rp must be absolute"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", rp,
        "-map", "0:v:0?", "-map", "0:a:0?",
        "-c:v", "h264_nvenc" if _nvenc_available() == "nvenc" else "libx264",
        "-preset", "p1" if _nvenc_available() == "nvenc" else "ultrafast",
        *(["-rc", "vbr", "-cq", "26"] if _nvenc_available() == "nvenc" else ["-crf", "26"]),
        "-pix_fmt", "yuv420p",
        "-vf", "scale='if(gt(iw,ih),min(1920,iw),-2)':'if(gt(iw,ih),-2,min(1920,ih))',format=yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, bufsize=0)
    # Wall-clock cap: if a client opens a video then stops reading (backgrounded
    # tab), the pipe fills, ffmpeg blocks on write, and this thread blocks in
    # read() forever. Enough of those starve the 32-thread worker. Kill after
    # 300s so read() gets EOF and the generator unwinds. ponytail: single global
    # timeout, no per-stream semaphore until this proves insufficient.
    watchdog = threading.Timer(300, proc.kill)
    watchdog.daemon = True
    watchdog.start()
    def gen():
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            watchdog.cancel()
            try:
                proc.terminate(); proc.wait(timeout=2)
            except Exception:
                try: proc.kill()
                except Exception: pass
    return Response(stream_with_context(gen()), mimetype="video/mp4",
                    headers={"Cache-Control": "no-store",
                             "X-Accel-Buffering": "no"})


def _kick_background_transcode(rp, cached_path, key):
    """Spawn one background NVENC transcode. No-op if already running/done."""
    assert rp and cached_path and key, "params required"
    if os.path.exists(cached_path):
        return
    with _video_transcode_locks_mutex:
        if key in _video_transcode_locks and _video_transcode_locks[key].locked():
            return  # already in flight
        lock = _video_transcode_locks.setdefault(key, threading.Lock())
    def _worker():
        if not lock.acquire(blocking=False):
            return
        try:
            if not os.path.exists(cached_path):
                _transcode_to_h264(rp, cached_path)
        finally:
            lock.release()
    threading.Thread(target=_worker, daemon=True).start()


@app.route("/api/photos/video")
@require_auth
def serve_web_video():
    """Serve a video. Cache hit → instant. Cache miss → serve original now,
    transcode in background for next click. NEVER blocks on transcode."""
    raw_path = request.args.get("p", "").strip()
    rp = _resolve_photo_path(raw_path)
    if not rp:
        abort(404)
    if not any(os.path.abspath(rp).startswith(pfx) for pfx in _PHOTO_ALLOWED):
        abort(403)
    ext = rp.rsplit(".", 1)[-1].lower()
    if ext not in _VIDEO_MIMES:
        abort(400)

    # Reject if item is vaulted — use /api/vault/video instead
    _load_vault()
    _vk = None
    for _vi in load_photo_index():
        if _vi.get("path") == rp:
            _vk = _thumb_key_for_item(_vi)
            break
    if _vk:
        with _vault_state_lock:
            if _vk in _vault_state["items"]:
                abort(403)

    # Cache hit — serve immediately, no ffprobe.
    # NOTE: we no longer fast-path "already h264 mp4" sources. Originals are
    # often 50-200 MB; serving them through Werkzeug under prewarm load
    # produced multi-second stalls per Range request. The cached transcode
    # is scaled to 1920px and ~5-15 MB — uniformly fast.
    key = _video_cache_key(rp)
    cached = os.path.join(_VIDEO_CACHE_DIR, key + ".mp4")
    if os.path.exists(cached):
        # Redirect to Caddy-served static URL so Caddy handles file I/O
        # directly (sendfile, native range requests) — no Python in the hot path.
        static_url = "/static/video_cache/" + key + ".mp4"
        return redirect(static_url, code=302)

    # Cache miss. Two parallel actions:
    # 1. Kick background transcode — second click will be a cache hit.
    # 2. Serve via progressive CPU transcode (libx264 ultrafast pipes data
    #    to the browser immediately; first frame appears in <1s even for HEVC
    #    sources that Chrome can't decode natively). No Range/seek on this
    #    first response, but the cached follow-up will be fully seekable.
    _kick_background_transcode(rp, cached, key)
    if _nvenc_available() is not None and not _gpu_on_loan():
        return _stream_progressive_transcode(rp)
    # GPU on loan (VM has priority) or libx264 missing: serve raw original —
    # no CPU x264 encode competing with the VM's cores.
    mime = "video/mp4" if ext in ("mp4", "mov", "m4v") else _VIDEO_MIMES.get(ext, "video/mp4")
    resp = send_file(rp, mimetype=mime, as_attachment=False,
                     conditional=True, download_name=os.path.basename(rp))
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp



_hls_gen_locks = {}
_hls_gen_locks_mutex = threading.Lock()


def _generate_hls(cached_mp4, hls_dir):
    """Segment a cached H.264 MP4 into HLS with stream copy (no re-encode).
    Returns True on success. Safe to call concurrently — locks per key."""
    assert os.path.isabs(cached_mp4) and os.path.isabs(hls_dir), "abs paths required"
    m3u8 = os.path.join(hls_dir, "index.m3u8")
    if os.path.exists(m3u8):
        return True
    if _gpu_on_loan():
        return False  # GPU lent to a VM — defer HLS segmentation (CPU ffmpeg) until it returns.
    key = os.path.basename(hls_dir)
    with _hls_gen_locks_mutex:
        lock = _hls_gen_locks.setdefault(key, threading.Lock())
    with lock:
        if os.path.exists(m3u8):
            return True
        tmp = hls_dir + ".part"
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)  # discard any stale dir from a killed prior run
            os.makedirs(tmp, exist_ok=True)
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", cached_mp4,
                "-c", "copy",
                "-hls_time", "4",
                "-hls_playlist_type", "vod",
                "-hls_flags", "independent_segments",
                # fMP4 (not MPEG-TS) segments: TS has no track header, so it DROPS the
                # rotation display matrix — the NVENC transcode encodes landscape pixels +
                # a rotate flag, and TS silently loses the flag, so portrait videos played
                # sideways. fMP4 keeps the matrix in the init segment. Still stream-copy.
                "-hls_segment_type", "fmp4",
                "-hls_fmp4_init_filename", "init.mp4",
                "-hls_segment_filename", os.path.join(tmp, "seg%04d.m4s"),
                os.path.join(tmp, "index.m3u8"),
            ]
            r = subprocess.run(cmd, capture_output=True, timeout=120)
            if r.returncode != 0 or not os.path.exists(os.path.join(tmp, "index.m3u8")):
                print(f"[hls] segment failed for {cached_mp4}: {r.stderr.decode()[-300:]}")
                import shutil; shutil.rmtree(tmp, ignore_errors=True)
                return False
            if os.path.exists(hls_dir):
                import shutil; shutil.rmtree(hls_dir, ignore_errors=True)
            os.rename(tmp, hls_dir)
            return True
        except Exception as e:
            print(f"[hls] error: {e}")
            import shutil; shutil.rmtree(tmp, ignore_errors=True)
            return False


@app.route("/api/photos/hls")
@require_auth
def serve_hls_playlist():
    """Return HLS playlist URL for a video. Generates HLS on first call
    (stream copy from cached MP4 — typically < 2s). Subsequent calls are
    instant redirects to Caddy-served /static/hls/{key}/index.m3u8."""
    raw_path = request.args.get("p", "").strip()
    rp = _resolve_photo_path(raw_path)
    if not rp or not any(os.path.abspath(rp).startswith(pfx) for pfx in _PHOTO_ALLOWED):
        abort(404)
    ext = rp.rsplit(".", 1)[-1].lower()
    if ext not in _VIDEO_MIMES:
        abort(400)
    # Reject if item is vaulted
    _load_vault()
    for _hvi in load_photo_index():
        if _hvi.get("path") == rp:
            _hvk = _thumb_key_for_item(_hvi)
            if _hvk:
                with _vault_state_lock:
                    if _hvk in _vault_state["items"]:
                        abort(403)
            break
    key = _video_cache_key(rp)
    hls_dir = os.path.join(_HLS_CACHE_DIR, key)
    cached_mp4 = os.path.join(_VIDEO_CACHE_DIR, key + ".mp4")

    if not os.path.exists(os.path.join(hls_dir, "index.m3u8")):
        # Self-heal old caches transcoded before the rotation fix: a cached MP4 that still
        # carries a rotation flag was baked wrong (landscape pixels + flag) — drop it so it
        # re-transcodes via the CPU path with orientation baked into the pixels.
        if os.path.exists(cached_mp4) and _video_rotation_flag(cached_mp4) != 0:
            import shutil
            try: os.remove(cached_mp4)
            except OSError: pass
            shutil.rmtree(hls_dir, ignore_errors=True)
        if not os.path.exists(cached_mp4):
            # Take the SAME per-key lock every other cached_mp4 writer uses
            # (_ensure_video_cached, _kick_background_transcode). Without it, two
            # concurrent HLS requests both call _transcode_to_h264 on the same
            # dest → both write the shared "<key>.mp4.part" and race os.replace →
            # corrupt/truncated cache + doubled GPU load. Double-check inside.
            with _video_transcode_locks_mutex:
                _tlock = _video_transcode_locks.setdefault(key, threading.Lock())
            with _tlock:
                if not os.path.exists(cached_mp4):
                    ok = _transcode_to_h264(rp, cached_mp4)
                    if not ok:
                        return jsonify({"error": "transcode failed"}), 500
        ok = _generate_hls(cached_mp4, hls_dir)
        if not ok:
            return jsonify({"error": "hls generation failed"}), 500

    return redirect(f"/static/hls/{key}/index.m3u8", code=302)


_infuse_tokens = {}
_infuse_tokens_lock = threading.Lock()


@app.route("/api/photos/infuse-token")
@require_auth
def infuse_token():
    """Issue a 24-hour token URL for Infuse to stream a video without a
    browser session cookie. Returns {infuse_url, direct_url}."""
    import secrets as _secrets
    raw_path = request.args.get("p", "").strip()
    rp = _resolve_photo_path(raw_path)
    if not rp or not any(os.path.abspath(rp).startswith(pfx) for pfx in _PHOTO_ALLOWED):
        abort(404)
    ext = rp.rsplit(".", 1)[-1].lower()
    if ext not in _VIDEO_MIMES:
        abort(400)
    key = _video_cache_key(rp)
    cached_mp4 = os.path.join(_VIDEO_CACHE_DIR, key + ".mp4")
    token = _secrets.token_urlsafe(24)
    with _infuse_tokens_lock:
        _infuse_tokens[token] = {
            "path": cached_mp4 if os.path.exists(cached_mp4) else rp,
            "expires": time.time() + 86400,
        }
    host = request.host_url.rstrip("/")
    direct = f"{host}/api/photos/play/{token}"
    infuse_url = f"infuse://x-callback-url/play?url={direct}"
    return jsonify({"infuse_url": infuse_url, "direct_url": direct})


@app.route("/api/photos/play/<token>")
def play_with_token(token):
    """Serve a video using a time-limited token (for Infuse / native players).
    No session auth required — the token IS the credential."""
    assert token and len(token) < 100, "invalid token"
    with _infuse_tokens_lock:
        entry = _infuse_tokens.get(token)
    if not entry or entry["expires"] < time.time():
        abort(403)
    fp = entry["path"]
    if not os.path.isfile(fp):
        abort(404)
    import mimetypes as _mt
    mime = _mt.guess_type(fp)[0] or "video/mp4"
    resp = send_file(fp, mimetype=mime, conditional=True)
    resp.headers["Accept-Ranges"] = "bytes"
    return resp


@app.route("/api/photos/download")
@require_auth
def download_photo_direct():
    """Serve a photo/video. ?p=path  Videos stream inline (range support). ?dl=1 forces attachment. ?fmt=jpeg converts to JPEG."""
    raw_path = request.args.get("p", "").strip()
    rp = _resolve_photo_path(raw_path)
    if not rp:
        abort(404)
    if not any(os.path.abspath(rp).startswith(pfx) for pfx in _PHOTO_ALLOWED):
        abort(403)
    # Reject if item is vaulted
    _load_vault()
    for _dvi in load_photo_index():
        if _dvi.get("path") == rp:
            _dvk = _thumb_key_for_item(_dvi)
            if _dvk:
                with _vault_state_lock:
                    if _dvk in _vault_state["items"]:
                        abort(403)
            break
    force_download = request.args.get("dl") == "1"
    want_jpeg = request.args.get("fmt") == "jpeg"
    ext = rp.rsplit(".", 1)[-1].lower()
    if want_jpeg and ext not in ("jpg", "jpeg"):
        buf = _to_jpeg_bytes(rp)
        if buf:
            stem = os.path.splitext(os.path.basename(rp))[0]
            return send_file(buf, mimetype="image/jpeg",
                             as_attachment=force_download,
                             download_name=stem + ".jpg")
    # Videos: inline with explicit mimetype so browser streams and supports Range
    if ext in _VIDEO_MIMES and not force_download:
        resp = send_file(rp, mimetype=_VIDEO_MIMES[ext], as_attachment=False,
                         conditional=True, download_name=os.path.basename(rp))
        resp.headers["Accept-Ranges"] = "bytes"
        return resp
    return send_file(rp, as_attachment=force_download or True,
                     download_name=os.path.basename(rp))


# ─── Photo Upload (iPhone Sync) ───

from photos.photo_scanner import gen_thumb, get_media_date, hash_path, IMAGE_EXTS, VIDEO_EXTS, ALL_EXTS, THUMB_DIR, THUMB_HQ_DIR, PHOTOS_ROOT, CONTENT_HASH_FILE
import hashlib as _hashlib

_content_hashes = {}
_index_write_lock = threading.Lock()
_content_hashes_lock = threading.Lock()


def _load_content_hashes():
    """Load content_hashes.json into memory (called in background thread)."""
    global _content_hashes
    if os.path.exists(CONTENT_HASH_FILE):
        try:
            with open(CONTENT_HASH_FILE) as f:
                data = json.load(f)
            with _content_hashes_lock:
                _content_hashes = data
            print(f"[hashes] Loaded {len(data)} content hashes")
        except Exception as e:
            print(f"[hashes] Failed to load content hashes: {e}")
    else:
        print("[hashes] No content_hashes.json found — upload dedup disabled until built")


def _save_content_hashes():
    """Persist content hashes to disk (runs in background thread)."""
    with _content_hashes_lock:
        data = dict(_content_hashes)
    try:
        tmp = CONTENT_HASH_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, CONTENT_HASH_FILE)
    except Exception as e:
        print(f"[hashes] Failed to save content hashes: {e}")


# Load synchronously at import. The previous background-thread load had a
# subtle race: an upload arriving while the thread was still reading the
# 4MB JSON would mutate the in-memory dict, then the load would finish and
# overwrite (losing the new entry), causing the next sync of the same
# photo to bypass dedup and create a duplicate.
_load_content_hashes()

# Per-content-SHA lock map. Multiple uploads of the same SHA serialize
# through the inner critical section; uploads of different SHAs proceed
# in parallel. Stale entries are pruned best-effort once the request
# completes — see _release_upload_lock.
_upload_locks = {}
_upload_locks_lock = threading.Lock()


def _acquire_upload_lock(sha: str) -> threading.Lock:
    with _upload_locks_lock:
        lk = _upload_locks.get(sha)
        if lk is None:
            lk = threading.Lock()
            _upload_locks[sha] = lk
        return lk


# Note: locks are kept in _upload_locks for the process lifetime. Bounded
# by unique SHAs ever uploaded — a few MB of RAM at worst. Releasing the
# entry mid-flight while another thread is waiting on it would create a
# subtle race (waiter holds the old lock, new caller gets a new lock,
# both run the critical section), so we accept the bounded leak.


# iOS PHAsset localIdentifier dedup: {ios_id: content_sha}
_IOS_IDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ios_photo_ids.json")
_ios_ids = {}
_ios_ids_lock = threading.Lock()
_ios_ids_dirty = False


def _load_ios_ids():
    global _ios_ids
    if os.path.exists(_IOS_IDS_FILE):
        try:
            with open(_IOS_IDS_FILE) as f:
                data = json.load(f)
            with _ios_ids_lock:
                _ios_ids = data
            print(f"[ios_ids] Loaded {len(data)} iOS PHAsset identifiers")
        except Exception as e:
            print(f"[ios_ids] Failed to load: {e}")


def _record_ios_id(ios_id: str, content_sha: str):
    """Record that we've seen this iOS PHAsset id (atomic persist-on-write)."""
    global _ios_ids_dirty
    if not ios_id:
        return
    with _ios_ids_lock:
        if _ios_ids.get(ios_id) == content_sha:
            return
        _ios_ids[ios_id] = content_sha
        _ios_ids_dirty = True
    # Persist in background; coalesce with a small debounce via thread
    threading.Thread(target=_save_ios_ids, daemon=True).start()


def _save_ios_ids():
    global _ios_ids_dirty
    with _ios_ids_lock:
        if not _ios_ids_dirty:
            return
        data = dict(_ios_ids)
        _ios_ids_dirty = False
    try:
        tmp = _IOS_IDS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _IOS_IDS_FILE)
    except Exception as e:
        print(f"[ios_ids] Failed to save: {e}")


threading.Thread(target=_load_ios_ids, daemon=True).start()


@app.route("/api/photos/hashes")
@require_auth
def photo_hashes():
    """Return set of SHA256 content hashes for dedup checking."""
    with _content_hashes_lock:
        return jsonify(list(_content_hashes.keys()))


@app.route("/api/photos/ios-ids")
@require_auth
def photo_ios_ids():
    """Return list of iOS PHAsset localIdentifiers the server has seen.

    The iOS app uses this to skip the expensive SHA256 check for photos
    it already uploaded — just filter out any asset whose localIdentifier
    is in this set.
    """
    with _ios_ids_lock:
        return jsonify(list(_ios_ids.keys()))


@app.route("/api/photos/ios-id-map")
@require_auth
def photo_ios_id_map():
    """Reverse of _ios_ids: {content_sha: ios_id}.

    Photos are named by content SHA (the thumbnail filename), so the iOS app can
    join each gallery item (thumb-sha) to the on-device PHAsset localIdentifier —
    used for offline full-res from the device and safe "free up space" dedupe.
    If several localIdentifiers map to one SHA, last one wins (any is fine).
    """
    with _ios_ids_lock:
        rev = {sha: ios for ios, sha in _ios_ids.items()}
    return jsonify(rev)


@app.route("/api/photos/register-ios-ids", methods=["POST"])
@require_auth
def register_ios_ids():
    """Batch-register iOS identifiers for photos the client has confirmed
    are already on the NAS (via SHA256 match). Payload: [[ios_id, content_sha], ...].
    This lets the server learn about pre-existing photos so future syncs are instant.
    """
    data = request.get_json(silent=True) or []
    if not isinstance(data, list):
        return jsonify({"error": "expected list"}), 400
    added = 0
    with _ios_ids_lock:
        for pair in data:
            if not isinstance(pair, list) or len(pair) != 2:
                continue
            ios_id, sha = pair
            if not (isinstance(ios_id, str) and isinstance(sha, str)):
                continue
            if not ios_id or _ios_ids.get(ios_id) == sha:
                continue
            _ios_ids[ios_id] = sha
            added += 1
        global _ios_ids_dirty
        _ios_ids_dirty = _ios_ids_dirty or (added > 0)
    if added:
        threading.Thread(target=_save_ios_ids, daemon=True).start()
    return jsonify({"registered": added, "total_known": len(_ios_ids)})


_SCRATCH_DIR = os.path.join(_APP_DIR, "..", "..", "PROJECTS", "_scratch")

@app.route("/api/scratch-upload", methods=["POST"])
@require_auth
def scratch_upload():
    """Save a pasted image to _scratch/ and return its host path for Claude Code."""
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    import time as _time, hashlib as _hl
    data = f.read(20 * 1024 * 1024)  # 20 MB cap
    if not data:
        return jsonify({"error": "Empty"}), 400
    ext = os.path.splitext(f.filename or "")[1].lower() or ".png"
    if ext not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"}:
        return jsonify({"error": "Unsupported type"}), 400
    name = "paste-" + _hl.md5(data).hexdigest()[:8] + ext
    scratch = os.path.realpath(os.path.join(_APP_DIR, "..", "..", "PROJECTS", "_scratch"))
    assert scratch.endswith(os.sep + "_scratch") or "_scratch" in scratch, "bad path"
    os.makedirs(scratch, exist_ok=True)
    dest = os.path.join(scratch, name)
    with open(dest, "wb") as fp:
        fp.write(data)
    # Return the host-side path Claude Code sees (LXC mount rewrite)
    host_path = dest.replace("/mnt/data/PROMETHEUS", "/mnt/nvme/PROMETHEUS")
    return jsonify({"path": host_path, "name": name})


@app.route("/api/upload", methods=["POST"])
@require_auth
def upload_photo():
    """Accept photo/video upload, thumbnail it, and add to the index."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    filename = secure_filename(f.filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALL_EXTS:
        return jsonify({"error": f"Unsupported file type: {ext}"}), 400

    # Read file bytes and compute content hash for dedup
    file_bytes = f.read()
    content_sha = _hashlib.sha256(file_bytes).hexdigest()

    # iOS PHAsset localIdentifier for fast future dedup (avoids SHA256 round-trip)
    ios_id = (request.form.get("photo_ios_id") or "").strip()

    # iOS-id dedup must come BEFORE content-SHA dedup. iCloud Photos can
    # hand out different byte sequences for the same PHAsset across syncs
    # (HEIC re-encodes, edit revisions, low-res→full-res transitions), so
    # the SHA can drift even though we're looking at the same logical
    # photo. Without this check the server would fall through to the
    # filename-collision branch and save IMG_XXXX_1.EXT next to the
    # original — which is exactly the duplication the user is seeing.
    if ios_id:
        with _ios_ids_lock:
            prior_sha = _ios_ids.get(ios_id)
        if prior_sha:
            return jsonify({"status": "skipped", "reason": "duplicate_ios_id",
                            "prior_sha": prior_sha}), 200

    # Serialize the rest of the upload by content SHA so two parallel
    # uploads of the same bytes can't both pass the dedup check before
    # either finishes writing.
    upload_lock = _acquire_upload_lock(content_sha)
    with upload_lock:
        return _do_upload(f, file_bytes, content_sha, ios_id, ext)


def _do_upload(f, file_bytes, content_sha, ios_id, ext):
    filename = secure_filename(f.filename)

    # Content-hash dedup: same bytes already on NAS (regardless of filename)
    with _content_hashes_lock:
        existing = _content_hashes.get(content_sha)
    if existing:
        # Still record the iOS id so the phone can skip this on next sync.
        if ios_id:
            _record_ios_id(ios_id, content_sha)
        return jsonify({"status": "skipped", "reason": "duplicate_content",
                        "existing_path": existing}), 200

    # Determine date: phone creation_date > EXIF > now
    client_date = request.form.get("creation_date")
    if client_date:
        try:
            photo_dt = datetime.fromtimestamp(float(client_date), tz=_GALLERY_TZ)
        except (ValueError, OSError):
            photo_dt = datetime.now(tz=_GALLERY_TZ)
    else:
        photo_dt = datetime.now(tz=_GALLERY_TZ)

    # Save to iPhone/<YYYY>/<MM>/ based on actual photo date
    dest_dir = os.path.join(PHOTOS_ROOT, "iPhone", str(photo_dt.year), f"{photo_dt.month:02d}")
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, filename)

    # Handle filename collisions with incrementing suffix
    if os.path.exists(dest_path):
        stem, fext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(dest_path):
            dest_path = os.path.join(dest_dir, f"{stem}_{counter}{fext}")
            counter += 1

    with open(dest_path, "wb") as out_f:
        out_f.write(file_bytes)

    # Generate thumbnails
    rel_path = os.path.relpath(dest_path, PHOTOS_ROOT)
    thumb_name = hash_path(rel_path) + ".jpg"
    thumb_path = os.path.join(THUMB_DIR, thumb_name)
    thumb_hq_path = os.path.join(THUMB_HQ_DIR, thumb_name)
    is_video = ext in VIDEO_EXTS

    os.makedirs(THUMB_DIR, exist_ok=True)
    os.makedirs(THUMB_HQ_DIR, exist_ok=True)
    gen_thumb(dest_path, thumb_path, 475, 3, is_video)
    gen_thumb(dest_path, thumb_hq_path, 800, 2, is_video)

    # Date priority: phone creation_date > EXIF > mtime
    date = None
    if client_date:
        try:
            date = float(client_date)
        except (ValueError, OSError):
            pass
    if date is None:
        date = get_media_date(dest_path)
    if date is None:
        date = os.path.getmtime(dest_path)

    media_type = "video" if is_video else "image"
    entry = {
        "path": dest_path,
        "thumb": f"/static/thumbs/{thumb_name}",
        "thumb_hq": f"/static/thumbs_hq/{thumb_name}",
        "date": date,
        "type": media_type,
    }

    # Insert into index in sorted position (newest first = descending by date)
    items = load_photo_index()
    insert_pos = len(items)
    for i, item in enumerate(items):
        if date >= item["date"]:
            insert_pos = i
            break
    items.insert(insert_pos, entry)

    _save_photo_index(items)

    if ios_id:
        _record_ios_id(ios_id, content_sha)

    _summary_cache["data"] = None
    _summary_cache["mtime"] = 0
    _month_json_cache["data"] = None

    # Register content hash and persist in background
    with _content_hashes_lock:
        _content_hashes[content_sha] = dest_path
    threading.Thread(target=_save_content_hashes, daemon=True).start()

    return jsonify({"status": "ok", "entry": entry}), 201


# ─── Photo Delete (trash) ───

@app.route("/api/photos/rotate", methods=["POST"])
@require_auth
def rotate_photo():
    """Rotate photo thumbnails. Supports single or batch rotation.

    Body: {"hash": "abc", "direction": "cw"} — single photo
      or: {"hashes": ["abc","def"], "direction": "ccw"} — batch
    direction: "cw" (default, 90° clockwise) or "ccw" (90° counter-clockwise)
    """
    from PIL import Image
    data = request.json
    direction = data.get("direction", "cw")
    angle = -90 if direction == "cw" else 90

    hashes = data.get("hashes", [])
    if not hashes:
        h = data.get("hash", "")
        if h:
            hashes = [h]
    if not hashes:
        return jsonify({"error": "No hash provided"}), 400

    results = {"rotated": 0, "errors": []}
    for photo_hash in hashes:
        # Thumb keys are hex hash_path() outputs. Reject anything else so a crafted "hash"
        # like "../../foo" can't open+overwrite an arbitrary .jpg outside the thumb dirs.
        if not isinstance(photo_hash, str) or not re.fullmatch(r"[0-9a-f]{8,64}", photo_hash):
            results["errors"].append(f"{photo_hash}: invalid hash")
            continue
        ok = False
        for tdir in [_THUMB_DIR, _THUMB_HQ_DIR]:
            tp = os.path.join(tdir, photo_hash + ".jpg")
            if os.path.exists(tp):
                try:
                    img = Image.open(tp)
                    img = img.rotate(angle, expand=True)
                    img.save(tp, "JPEG", quality=85, optimize=True)
                    ok = True
                except Exception as e:
                    results["errors"].append(f"{photo_hash}: {e}")
        if ok:
            results["rotated"] += 1
            name = photo_hash + ".jpg"
            with _thumb_cache_lock:
                _thumb_cache.pop((_THUMB_DIR, name), None)
                _thumb_cache.pop((_THUMB_HQ_DIR, name), None)

    return jsonify({"success": True, **results})


@app.route("/api/photos/delete", methods=["POST"])
@require_auth
def delete_photo():
    """Move a photo to the recycling bin and remove from all indices."""
    data = request.json
    photo_path = data.get("path", "")
    if not photo_path:
        return jsonify({"error": "No path provided"}), 400

    # Security: only allow deleting from known photo directories.
    # The same filesystem is mounted at different paths on the PVE host
    # vs. the LXC (/mnt/nvme/PHOTOS/ vs /mnt/data/PHOTOS/), and the
    # photo_index can hold either form depending on which side wrote it.
    abs_path = os.path.abspath(photo_path)
    allowed = [
        "/mnt/data/PHOTOS/",
        "/mnt/data/PROMETHEUS/PHOTOS/",
        "/mnt/nvme/PHOTOS/",
        "/mnt/nvme/PROMETHEUS/PHOTOS/",
        "/srv/mergerfs/PROMETHEUS/PHOTOS/",
        "/Volumes/PROMETHEUS/PHOTOS/",
    ]
    if not any(abs_path.startswith(prefix) for prefix in allowed):
        return jsonify({"error": "Not allowed"}), 403

    # Resolve to the actual on-disk path. The photo_index has stale paths
    # with a duplicated "/PHOTOS/PHOTOS/" segment; the resolver rewrites
    # them to the real filesystem location.
    real_path = _resolve_photo_path(photo_path)
    if not real_path or not os.path.isfile(real_path):
        return jsonify({"error": "Path does not exist: " + photo_path}), 404

    # Compute thumb hash from the indexed path (that's what was hashed
    # at index time, so it matches the on-disk thumb filename even if
    # the path was stale).
    try:
        rel_path = os.path.relpath(photo_path, PHOTOS_ROOT)
    except ValueError:
        rel_path = os.path.relpath(real_path, PHOTOS_ROOT)
    photo_hash = hash_path(rel_path)
    thumb_name = photo_hash + ".jpg"

    # Move the real file to the recycling bin
    result = trash_file(real_path)
    if not result["success"]:
        return jsonify({"error": result["error"]}), 400

    # Remove from photo index (atomic)
    items = load_photo_index()
    items = [i for i in items if i["path"] != photo_path]
    _save_photo_index(items)

    # Remove thumbnails from disk + RAM cache
    for tdir in [THUMB_DIR, THUMB_HQ_DIR]:
        tp = os.path.join(tdir, thumb_name)
        if os.path.exists(tp):
            try:
                os.unlink(tp)
            except OSError:
                pass
    with _thumb_cache_lock:
        _thumb_cache.pop((_THUMB_DIR, thumb_name), None)
        _thumb_cache.pop((_THUMB_HQ_DIR, thumb_name), None)

    # Remove video frame thumbnail if present
    vidframe = os.path.join(_AI_DIR, "vidframes", photo_hash + ".jpg")
    if os.path.exists(vidframe):
        try:
            os.unlink(vidframe)
        except OSError:
            pass

    # Remove CLIP embedding row (atomic npy + json rewrite)
    try:
        import numpy as np
        hashes_path = os.path.join(_AI_DIR, "clip_hashes.json")
        emb_path = os.path.join(_AI_DIR, "clip_embeddings.npy")
        if os.path.exists(hashes_path) and os.path.exists(emb_path):
            with open(hashes_path) as f:
                clip_hashes = json.load(f)
            if photo_hash in clip_hashes:
                idx = clip_hashes.index(photo_hash)
                clip_hashes.pop(idx)
                embs = np.load(emb_path)
                embs = np.delete(embs, idx, axis=0)
                # Write atomically — np.save appends .npy so use a path that already ends in .npy
                tmp_emb = emb_path[:-4] + ".tmp.npy"  # clip_embeddings.tmp.npy
                np.save(tmp_emb, embs)
                os.replace(tmp_emb, emb_path)
                _atomic_write_json(hashes_path, clip_hashes)
                # Refresh in-memory CLIP index
                _ai["clip_hashes"] = clip_hashes
                _ai["clip_emb"] = embs
                _ai["hash_to_idx"] = {h: i for i, h in enumerate(clip_hashes)}
    except Exception as e:
        app.logger.warning(f"[delete] CLIP cleanup failed for {photo_hash}: {e}")

    # Remove face data tied to this photo
    try:
        import numpy as np
        fi_path = os.path.join(_AI_DIR, "face_index.json")
        fc_path = os.path.join(_AI_DIR, "face_clusters.json")
        fe_path = os.path.join(_AI_DIR, "face_embeddings.npy")
        if os.path.exists(fi_path):
            with open(fi_path) as f:
                face_index = json.load(f)
            if photo_hash in face_index:
                dead_faces = face_index.pop(photo_hash)
                # emb_idx may be int or list[int] depending on how face data was written
                dead_emb_idxs = set()
                for _face in dead_faces:
                    if "emb_idx" not in _face:
                        continue
                    _ei = _face["emb_idx"]
                    if isinstance(_ei, list):
                        dead_emb_idxs.update(_ei)
                    else:
                        dead_emb_idxs.add(_ei)
                _atomic_write_json(fi_path, face_index)

                # Update face_clusters: remove hash + emb_indices
                if os.path.exists(fc_path) and dead_emb_idxs:
                    with open(fc_path) as f:
                        clusters = json.load(f)
                    surviving = {}
                    for cid, c in clusters.items():
                        c["photo_hashes"] = [h for h in c.get("photo_hashes", []) if h != photo_hash]
                        c["emb_indices"] = [i for i in c.get("emb_indices", []) if i not in dead_emb_idxs]
                        if c.get("exemplars"):
                            c["exemplars"] = [i for i in c["exemplars"] if i not in dead_emb_idxs]
                        c["photo_count"] = len(c["photo_hashes"])
                        # Drop cluster only if it has zero photos AND zero emb_indices
                        if c["photo_count"] > 0 or c.get("emb_indices"):
                            surviving[cid] = c
                    _atomic_write_json(fc_path, surviving)

                # Rebuild face_embeddings.npy with dead rows zeroed (preserve row indices)
                # We zero rather than delete to keep all emb_idx references stable.
                # A future re-cluster will compact them.
                if os.path.exists(fe_path) and dead_emb_idxs:
                    face_embs = np.load(fe_path)
                    for i in dead_emb_idxs:
                        if i < len(face_embs):
                            face_embs[i] = 0.0
                    tmp_fe = fe_path[:-4] + ".tmp.npy"  # face_embeddings.tmp.npy
                    np.save(tmp_fe, face_embs)
                    os.replace(tmp_fe, fe_path)

                # Invalidate in-memory AI state so next search reloads fresh data
                _ai["face_index"] = None
                _ai["face_clusters"] = None
                _ai["face_embs"] = None
                _ai["face_centroids"] = None
                _ai["emb_to_cluster"] = None
    except Exception as e:
        app.logger.warning(f"[delete] Face cleanup failed for {photo_hash}: {e}")

    # Invalidate photo/summary/month caches
    _photo_cache["data"] = None
    _photo_cache["mtime"] = 0
    _summary_cache["data"] = None
    _summary_cache["mtime"] = 0
    _month_json_cache["data"] = None

    return jsonify({"ok": True, "trash_name": result["trash_name"]})


def _startup_preload():
    """Build hash index, month index, and warm ALL thumbs in background after startup."""
    try:
        items = load_photo_index()
        if not items:
            return
        _build_hash_index(items)
        load_month_index()
        # Disabled: startup warmer spawns dozens of ffmpeg workers and chokes the LXC.
        # Video thumbs are backfilled by scripts/backfill_video_thumbs.py instead.
        # Image thumbs: load existing into RAM but don't regenerate missing.
        threading.Thread(target=_load_existing_thumbs, args=(items,), daemon=True).start()
        # Peak-quality WebP tier — backfilled in background so lightbox-full
        # loads stay tiny (200-500 KB vs 2-5 MB original) on slow links.
        threading.Thread(target=_prewarm_all_max_webp, args=(items,), daemon=True).start()
    except Exception:
        pass


def _load_existing_thumbs(items):
    """Load already-generated thumbs into RAM cache without regenerating missing ones."""
    loaded = 0
    for item in items:
        url = item.get("thumb", "")
        if not url:
            continue
        name = url.rsplit("/", 1)[-1]
        path = os.path.join(_THUMB_DIR, name)
        if os.path.exists(path) and (_THUMB_DIR, name) not in _thumb_cache:
            _read_thumb(_THUMB_DIR, name)
            loaded += 1
    if loaded:
        print(f"[warmer] Loaded {loaded} existing thumbs into RAM (no regen).")
    _build_landscape_index()


def _warm_all_thumbs(items):
    """Generate every missing thumbnail and load all into RAM cache.
    Processes newest photos first so recent months are ready fastest.
    """
    # First: load already-existing thumbs into RAM cache
    existing_loaded = 0
    for item in items:
        url = item.get("thumb", "")
        if not url:
            continue
        name = url.rsplit("/", 1)[-1]
        path = os.path.join(_THUMB_DIR, name)
        if os.path.exists(path) and (_THUMB_DIR, name) not in _thumb_cache:
            _read_thumb(_THUMB_DIR, name)
            existing_loaded += 1
    if existing_loaded:
        print(f"[warmer] Loaded {existing_loaded} existing thumbs into RAM.")

    _build_landscape_index()
    _summary_cache["data"] = None  # force covers to use landscape data

    # Then: generate missing thumbs and add them to RAM too
    missing = []
    for item in items:
        orig = item.get("path", "")
        if not orig:
            continue
        url = item.get("thumb", "")
        if not url:
            continue
        name = url.rsplit("/", 1)[-1]
        if not os.path.exists(os.path.join(_THUMB_DIR, name)):
            missing.append(item)

    if not missing:
        print(f"[warmer] All {len(items)} thumbs on disk and in RAM.")
        return

    print(f"[warmer] Generating {len(missing)} missing thumbs...")

    def _gen(item):
        orig = item["path"]
        if not os.path.isfile(orig):
            return
        ext = os.path.splitext(orig)[1].lower()
        is_video = ext in VIDEO_EXTS
        url = item["thumb"]
        name = url.rsplit("/", 1)[-1]
        out = os.path.join(_THUMB_DIR, name)
        if gen_thumb(orig, out, 475, 3, is_video):
            _read_thumb(_THUMB_DIR, name)  # immediately cache in RAM

    done = 0
    # Throttled: 2 workers so warmer doesn't starve HTTP during startup flood.
    # With ~9k missing videos this is slower but keeps ARES responsive.
    with ThreadPoolExecutor(max_workers=2) as pool:
        for _ in pool.map(_gen, missing):
            done += 1
            if done % 1000 == 0:
                print(f"[warmer] {done}/{len(missing)} thumbs done")

    print(f"[warmer] Done — all thumbs on disk and in RAM.")
    _build_landscape_index()
    _summary_cache["data"] = None  # force re-generation with landscape data

    # After initial thumbs done, pre-generate preview (2048px) for ALL photos
    # This runs slowly in background so lightbox opens instantly
    threading.Thread(target=_prewarm_all_previews, args=(items,), daemon=True).start()


def _prewarm_all_previews(items):
    """Generate 2048px lightbox previews for all photos in background."""
    time.sleep(5)
    missing = []
    for item in items:
        url = item.get("thumb_hq", "")
        if not url:
            continue
        name = url.rsplit("/", 1)[-1]
        if not os.path.exists(os.path.join(_THUMB_PREVIEW_DIR, name)):
            missing.append((item, name))

    if not missing:
        print(f"[preview] All {len(items)} previews cached.")
    else:
        print(f"[preview] Pre-generating {len(missing)} lightbox previews...")
        done = 0
        for item, name in missing:
            orig = item.get("path", "")
            if not orig or not os.path.isfile(orig):
                continue
            ext = os.path.splitext(orig)[1].lower()
            is_video = ext in VIDEO_EXTS
            out = os.path.join(_THUMB_PREVIEW_DIR, name)
            try:
                gen_thumb(orig, out, 2048, 1, is_video)
            except Exception:
                pass
            done += 1
            if done % 500 == 0:
                print(f"[preview] {done}/{len(missing)} previews done", flush=True)
        print(f"[preview] Done — {done} previews generated.", flush=True)
    # Now produce the peak-quality WebP tier — same set of items, larger
    # resolution, smaller file size than the JPEG originals on the wire.
    _prewarm_all_max_webp(items)


def _gen_max_webp(src_path, out_path, max_size=2560, quality=85):
    """Generate a high-res WebP from a still image. Returns True on success.
    Used as the peak-quality lightbox tier — files are ~200-500 KB vs the
    2-5 MB JPEG/HEIC originals, decoding at full retina display density."""
    assert src_path and out_path, "paths required"
    assert max_size >= 256, "max_size sanity"
    try:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        from PIL import Image, ImageOps
        img = Image.open(src_path)
        img = ImageOps.exif_transpose(img)
        img.thumbnail((max_size, max_size * 4), Image.LANCZOS)
        img.convert("RGB").save(out_path, "WEBP", quality=quality, method=4)
        return os.path.exists(out_path)
    except Exception as e:
        print(f"[max-webp] failed for {src_path}: {e}", flush=True)
        return False


def _prewarm_all_max_webp(items):
    """Build /static/thumbs_max/<hash>.webp for every still image. Skipped
    for videos (those use the video_cache transcode pipeline)."""
    time.sleep(3)
    photos = [i for i in items if i.get("type") != "video"]
    missing = []
    MAX_PHOTOS = 200000
    for item in photos[:MAX_PHOTOS]:
        url = item.get("thumb_hq") or item.get("thumb") or ""
        if not url:
            continue
        name = url.rsplit("/", 1)[-1].rsplit(".", 1)[0] + ".webp"
        out = os.path.join(_THUMB_MAX_DIR, name)
        if not os.path.exists(out):
            missing.append((item, out))
    if not missing:
        print(f"[max-webp] All {len(photos)} max-tier WebPs cached.", flush=True)
        return
    print(f"[max-webp] Pre-generating {len(missing)} peak-quality WebPs...", flush=True)
    done = 0
    failed = 0
    skipped = 0
    processed = 0
    for item, out in missing:
        orig_raw = item.get("path", "")
        # Use the same path-rewrite chain that serve routes use so the
        # photo_index's "/mnt/data/PHOTOS/PHOTOS/..." entries actually
        # resolve to the real file on disk.
        orig = _resolve_photo_path(orig_raw)
        if not orig:
            skipped += 1
        elif _gen_max_webp(orig, out):
            done += 1
        else:
            failed += 1
        processed += 1
        if processed % 200 == 0:
            print(f"[max-webp] {processed}/{len(missing)} processed (done={done} failed={failed} skipped={skipped})", flush=True)
    print(f"[max-webp] Done — {done} generated, {failed} failed, {skipped} unresolved.", flush=True)


def _warm_recent_months(items, months=3):
    """Generate missing thumbs for the N most recent months in a thread pool."""
    from collections import OrderedDict

    # Group by month, take the most recent N
    groups = OrderedDict()
    for item in items:
        mk = datetime.fromtimestamp(item["date"], tz=_GALLERY_TZ).strftime("%Y-%m")
        groups.setdefault(mk, []).append(item)

    recent_keys = sorted(groups.keys(), reverse=True)[:months]
    to_warm = [item for k in recent_keys for item in groups[k]]

    missing = []
    for item in to_warm:
        for url_key, tdir, size, quality in [
            ("thumb", _THUMB_DIR, 475, 3),
            ("thumb_hq", _THUMB_HQ_DIR, 800, 2),
            ("thumb_hq", _THUMB_PREVIEW_DIR, 2048, 1),  # preview for lightbox
        ]:
            url = item.get(url_key, "")
            if not url:
                continue
            name = url.rsplit("/", 1)[-1]
            if not os.path.exists(os.path.join(tdir, name)):
                missing.append((item, tdir, name, size, quality))

    if not missing:
        return

    print(f"[warmer] Pre-generating {len(missing)} thumbs for recent {months} months...")

    def _gen_one(args):
        item, tdir, name, size, quality = args
        orig = item.get("path", "")
        if not orig or not os.path.isfile(orig):
            return
        ext = os.path.splitext(orig)[1].lower()
        is_video = ext in VIDEO_EXTS
        out = os.path.join(tdir, name)
        gen_thumb(orig, out, size, quality, is_video)

    with ThreadPoolExecutor(max_workers=8) as pool:
        pool.map(_gen_one, missing)

    print(f"[warmer] Done pre-warming {len(missing)} thumbs.")


def _should_run_background():
    """Only run heavy background tasks in the actual server process, not the reloader parent."""
    return os.environ.get("WERKZEUG_RUN_MAIN") == "true" or "werkzeug" not in str(os.environ.get("SERVER_SOFTWARE", ""))

if _should_run_background():
    threading.Thread(target=_startup_preload, daemon=True).start()


# ─── Auto-scan for new photos ───

_AUTO_SCAN_INTERVAL = 60  # seconds between scans

def _auto_scan_loop():
    """Background thread: detect new photos on disk and add them to the index automatically."""
    import time as _time
    _time.sleep(15)  # let startup finish first

    skip_dirs = {"takeouts", "RECYCLE_BIN", "_inbox-snapchat"}
    skip_patterns = {"branded", "low-res"}

    def _lib_rel(index_path):
        """Library-relative suffix of an index path, tolerant of prefix forms."""
        for marker in ("/PHOTOS/PHOTOS/", "/PHOTOS/"):
            if marker in index_path:
                return index_path.split(marker, 1)[1]
        return index_path

    while True:
        try:
            items = load_photo_index()
            # SAFETY: if the index loaded empty (corrupt/unreadable — both main and
            # .bak), do NOT treat every file on disk as "new" and rebuild from
            # scratch. That nukes the library and regenerates ~47k thumbnails.
            # Recovery from a genuinely empty index is photo_scanner.py's explicit
            # job, not this incremental loop.
            if not items:
                print("[auto-scan] empty index load — skipping cycle (refusing to rebuild from scratch)")
                _time.sleep(_AUTO_SCAN_INTERVAL)
                continue
            # Compare by library-relative path so the canonical index prefix
            # (/mnt/data/PHOTOS/PHOTOS/...) matches real walk paths — raw
            # path comparison saw every indexed file as "new" and mass-duplicated.
            known_rel = {_lib_rel(e["path"]) for e in items}

            new_files = []
            for root, dirs, files in os.walk(PHOTOS_ROOT):
                # Skip named dirs AND any dot-directory (includes .vault)
                dirs[:] = [d for d in dirs
                           if d not in skip_dirs and not d.startswith(".")]
                for fname in files:
                    if fname.startswith("._"):
                        continue
                    fname_lower = fname.lower()
                    if any(pat in fname_lower for pat in skip_patterns):
                        continue
                    ext = os.path.splitext(fname)[1].lower()
                    if ext not in ALL_EXTS:
                        continue
                    filepath = os.path.join(root, fname)
                    if os.path.relpath(filepath, PHOTOS_ROOT) not in known_rel:
                        new_files.append((filepath, ext))

            # SAFETY: a normal sync adds a handful of files. Thousands of "new"
            # files means the index is short/clobbered (or PHOTOS_ROOT changed) —
            # refuse rather than mass-rebuild. Recover via photo_scanner.py.
            if len(new_files) > 2000:
                print(f"[auto-scan] {len(new_files)} 'new' files vs {len(items)} indexed — "
                      f"refusing mass rebuild; run photo_scanner.py explicitly.")
                _time.sleep(_AUTO_SCAN_INTERVAL)
                continue

            if new_files:
                print(f"[auto-scan] Found {len(new_files)} new file(s), indexing...")
                new_entries = []
                for filepath, ext in new_files:
                    try:
                        is_video = ext in VIDEO_EXTS
                        date = get_media_date(filepath)
                        if date is None:
                            date = os.path.getmtime(filepath)
                        rel_path = os.path.relpath(filepath, PHOTOS_ROOT)
                        thumb_name = hash_path(rel_path) + ".jpg"
                        entry = {
                            "path": os.path.join("/mnt/data/PHOTOS/PHOTOS", rel_path),
                            "thumb": f"/static/thumbs/{thumb_name}",
                            "thumb_hq": f"/static/thumbs_hq/{thumb_name}",
                            "date": date,
                            "type": "video" if is_video else "image",
                        }
                        new_entries.append(entry)
                        # Generate thumbnails immediately
                        thumb_path = os.path.join(THUMB_DIR, thumb_name)
                        hq_path = os.path.join(THUMB_HQ_DIR, thumb_name)
                        if not os.path.exists(thumb_path):
                            gen_thumb(filepath, thumb_path, 475, 3, is_video)
                        if not os.path.exists(hq_path):
                            gen_thumb(filepath, hq_path, 800, 2, is_video)
                    except Exception as e:
                        print(f"[auto-scan] Failed to index {filepath}: {e}")

                if new_entries:
                    merged = items + new_entries
                    merged.sort(key=lambda x: x["date"], reverse=True)
                    _save_photo_index(merged)
                    _summary_cache["data"] = None
                    _summary_cache["mtime"] = 0
                    _month_cache["data"] = None
                    _month_json_cache["data"] = None
                    # Rebuild hash index for new photos
                    fresh = load_photo_index()
                    _build_hash_index(fresh)
                    print(f"[auto-scan] Added {len(new_entries)} photos. Index now has {len(merged)} entries.")
        except Exception as e:
            print(f"[auto-scan] Error: {e}")

        _time.sleep(_AUTO_SCAN_INTERVAL)

if _should_run_background():
    threading.Thread(target=_auto_scan_loop, daemon=True).start()


# ─── AI Search (CLIP semantic search + face clusters) ───

_AI_DIR = os.path.join(_APP_DIR, "ai_data")


def _atomic_write_json(path, data):
    """Atomically write JSON to path via tmp file + os.replace (same as _save_photo_index)."""
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
_ai = {
    "clip_hashes": None, "clip_emb": None, "hash_to_idx": {},
    "model": None, "tokenizer": None, "ready": False,
    "face_clusters": None, "face_index": None,
    "face_embs": None, "face_centroids": None, "emb_to_cluster": None,
    "screenshot_hashes": None,
    "duplicate_hashes": None,
    "name_to_cluster": {},
}


def _load_ai_index():
    """Load CLIP embeddings + face clusters from disk (no model yet)."""
    import numpy as np

    # Screenshot hashes are independent of CLIP and used to filter the gallery.
    ss_path_early = os.path.join(_AI_DIR, "screenshot_hashes.json")
    if os.path.exists(ss_path_early):
        with open(ss_path_early) as f:
            _ai["screenshot_hashes"] = set(json.load(f))
        ss_n = len(_ai["screenshot_hashes"])
        print(f"[ai] Loaded {ss_n} screenshot hashes.")

    dup_path_early = os.path.join(_AI_DIR, "duplicate_hashes.json")
    if os.path.exists(dup_path_early):
        with open(dup_path_early) as f:
            _ai["duplicate_hashes"] = set(json.load(f))
        dup_n = len(_ai["duplicate_hashes"])
        print(f"[ai] Loaded {dup_n} duplicate hashes.")

    hashes_path = os.path.join(_AI_DIR, "clip_hashes.json")
    emb_path = os.path.join(_AI_DIR, "clip_embeddings.npy")
    if not os.path.exists(hashes_path) or not os.path.exists(emb_path):
        return

    with open(hashes_path) as f:
        hashes = json.load(f)
    emb = np.load(emb_path)
    if len(hashes) != len(emb):
        # Interrupted indexer run can leave hashes/embeddings out of sync on
        # disk; truncate both to the common prefix so every idx is in bounds.
        n = min(len(hashes), len(emb))
        print(f"[ai] WARNING: {len(hashes)} clip hashes vs {len(emb)} embeddings; truncating to {n}.")
        hashes = hashes[:n]
        emb = emb[:n]
    _ai["clip_hashes"] = hashes
    _ai["clip_emb"] = emb
    _ai["hash_to_idx"] = {h: i for i, h in enumerate(hashes)}
    print(f"[ai] Loaded {len(hashes)} CLIP embeddings.")

    fc_path = os.path.join(_AI_DIR, "face_clusters.json")
    if os.path.exists(fc_path):
        with open(fc_path) as f:
            _ai["face_clusters"] = json.load(f)
        print(f"[ai] Loaded {len(_ai['face_clusters'])} face clusters.")
        # Only wipe avatar cache when face_clusters.json has changed since the
        # avatars were last generated.  Previously we wiped unconditionally on every
        # service restart, forcing 229 PIL crop requests on the first People tab open.
        avatar_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "face_avatars")
        fc_mtime = os.path.getmtime(fc_path)
        _stamp_path = os.path.join(avatar_dir, ".fc_mtime")
        _stamp_ok = False
        if os.path.isdir(avatar_dir) and os.path.exists(_stamp_path):
            try:
                with open(_stamp_path) as _sf:
                    _stamp_ok = abs(float(_sf.read().strip()) - fc_mtime) < 1.0
            except (OSError, ValueError):
                _stamp_ok = False
        if not _stamp_ok:
            # face_clusters.json changed — invalidate stale avatars
            if os.path.isdir(avatar_dir):
                for fn in os.listdir(avatar_dir):
                    if fn.endswith(".jpg"):
                        try:
                            os.unlink(os.path.join(avatar_dir, fn))
                        except OSError:
                            pass
            print("[ai] Avatar cache invalidated (face_clusters.json changed).")

    fi_path = os.path.join(_AI_DIR, "face_index.json")
    if os.path.exists(fi_path):
        with open(fi_path) as f:
            _ai["face_index"] = json.load(f)

    # Load face embeddings and build centroid / emb→cluster lookup
    fe_path = os.path.join(_AI_DIR, "face_embeddings.npy")
    MIN_DET_SCORE = 0.5
    if os.path.exists(fe_path) and _ai["face_index"] is not None:
        face_embs = np.load(fe_path)
        _ai["face_embs"] = face_embs
        emb_to_hash = {}
        score_map = {}
        for ph, faces in _ai["face_index"].items():
            for face in faces:
                emb_to_hash[face["emb_idx"]] = ph
                score_map[face["emb_idx"]] = face.get("det_score", 1.0)
        centroids = {}
        exemplar_data = {}  # cid → np array of exemplar embeddings
        for cid, c in _ai["face_clusters"].items():
            excluded = set(c.get("excluded_hashes", []))
            stored = c.get("emb_indices")
            if stored:
                # Use stored per-face indices (clean, no bystander pollution)
                idxs = [i for i in stored if i < len(face_embs)]
            else:
                # Legacy fallback: refine by keeping only the closest face
                # per photo to filter out bystanders in group photos
                hset = set(h for h in c.get("photo_hashes", []) if h not in excluded)
                all_idxs = [idx for idx, h in emb_to_hash.items() if h in hset]
                if not all_idxs:
                    continue
                rough_centroid = face_embs[all_idxs].mean(axis=0)
                # Group by photo, keep closest face per photo
                from collections import defaultdict
                photo_faces = defaultdict(list)
                for idx in all_idxs:
                    photo_faces[emb_to_hash[idx]].append(idx)
                idxs = []
                for ph, face_idxs in photo_faces.items():
                    if len(face_idxs) == 1:
                        idxs.append(face_idxs[0])
                    else:
                        dists = [(i, float(np.linalg.norm(face_embs[i] - rough_centroid)))
                                 for i in face_idxs]
                        idxs.append(min(dists, key=lambda x: x[1])[0])
            if idxs:
                # Quality-gate: only high-confidence faces for centroid
                hq_idxs = [i for i in idxs if score_map.get(i, 1.0) >= MIN_DET_SCORE]
                if not hq_idxs:
                    hq_idxs = idxs
                centroids[cid] = face_embs[hq_idxs].mean(axis=0)
            # Load exemplars if available (guard against int-index corruption)
            stored_ex = c.get("exemplars")
            if stored_ex and len(stored_ex) > 0:
                e0 = stored_ex[0]
                if isinstance(e0, (list, tuple)) and len(e0) == 512:
                    exemplar_data[cid] = np.array(stored_ex, dtype=np.float32)
                # else: skip corrupted exemplar (int indices written by bad indexer run)
        _ai["face_centroids"] = centroids
        _ai["face_exemplars"] = exemplar_data
        if centroids:
            cids = list(centroids.keys())
            # Build emb→cluster via vectorized distance computation.
            # Prior nested-loop exemplar approach: ~33s for 17k embs × 231 clusters.
            # Vectorized centroid matmul: ~1.6s. We use centroids here for speed;
            # exemplar matching is reserved for the search-time avatar path.
            t_etc0 = time.time()
            n_embs = len(face_embs)
            centroid_matrix = np.stack([centroids[cid] for cid in cids])  # (C, 512)
            valid_idxs = [idx for idx in emb_to_hash if idx < n_embs]
            emb_to_cluster = {}
            # Process in chunks of 4096 to bound peak RAM (17k × 231 × 4B = ~16 MB total)
            CHUNK = 4096
            i = 0
            max_iter = (len(valid_idxs) + CHUNK - 1) // CHUNK
            for _step in range(max_iter):
                batch_idxs = valid_idxs[i:i + CHUNK]
                if not batch_idxs:
                    break
                emb_batch = face_embs[batch_idxs]           # (B, 512)
                a2 = np.sum(emb_batch ** 2, axis=1, keepdims=True)    # (B, 1)
                b2 = np.sum(centroid_matrix ** 2, axis=1, keepdims=True)  # (C, 1)
                ab = emb_batch @ centroid_matrix.T            # (B, C)
                sq_dists = a2 + b2.T - 2 * ab               # (B, C)
                best = np.argmin(sq_dists, axis=1)           # (B,)
                for j, emb_idx in enumerate(batch_idxs):
                    emb_to_cluster[emb_idx] = cids[int(best[j])]
                i += CHUNK
            _ai["emb_to_cluster"] = emb_to_cluster
            t_etc1 = time.time()
            print(f"[ai] emb_to_cluster built ({len(emb_to_cluster)} entries) in {t_etc1-t_etc0:.2f}s")
        print(f"[ai] Face embeddings loaded, centroids for {len(centroids)} clusters, "
              f"exemplars for {len(exemplar_data)}.")

    # Load screenshot/document hashes
    ss_path = os.path.join(_AI_DIR, "screenshot_hashes.json")
    if os.path.exists(ss_path):
        with open(ss_path) as f:
            _ai["screenshot_hashes"] = set(json.load(f))
        print(f"[ai] Loaded {len(_ai['screenshot_hashes'])} screenshot hashes.")

    _rebuild_name_map()


def _rebuild_name_map():
    """Build lowercase name → cluster_id lookup from face_clusters."""
    clusters = _ai.get("face_clusters") or {}
    mapping = {}
    for cid, c in clusters.items():
        name = (c.get("name") or "").strip()
        if name and not c.get("hidden"):
            mapping[name.lower()] = cid
    _ai["name_to_cluster"] = mapping


def _parse_people_query(q):
    """Greedy longest-prefix tokenize: extract person names from the query and
    treat the rest as semantic CLIP text.

    Examples (assuming 'haadi' and 'mary jane' are known names):
        'haadi bald'              -> [('haadi', cid)],            'bald'
        'bald haadi'              -> [('haadi', cid)],            'bald'
        'mary jane park beach'    -> [('mary jane', cid)],        'park beach'
        'zain hamza beach'        -> [('zain', cid), ('hamza',cid)], 'beach'
        'zain and hamza'          -> [('zain', cid), ('hamza',cid)], ''
        'sunset over water'       -> [],                          'sunset over water'
    """
    name_map = _ai.get("name_to_cluster") or {}
    if not name_map or not q.strip():
        return [], q.strip()

    # Split on commas and ' and ' as soft separators; each segment is then
    # tokenized and greedy-matched.
    segments = re.split(r"\s*(?:,|\band\b)\s*", q.lower())
    matched = []
    seen_cids = set()
    leftover = []

    for segment in segments:
        tokens = segment.strip().split()
        i = 0
        n = len(tokens)
        while i < n:
            # try longest-prefix match starting at i
            best = 0
            for L in range(n - i, 0, -1):
                candidate = " ".join(tokens[i:i + L])
                cid = name_map.get(candidate)
                if cid is not None:
                    if cid not in seen_cids:
                        matched.append((candidate, cid))
                        seen_cids.add(cid)
                    best = L
                    break
            if best:
                i += best
            else:
                leftover.append(tokens[i])
                i += 1

    semantic = " ".join(leftover).strip()
    return matched, semantic


def _get_person_hashes(cluster_id):
    """Return set of photo_hashes minus excluded_hashes for a cluster."""
    clusters = _ai.get("face_clusters") or {}
    c = clusters.get(cluster_id, {})
    hashes = set(c.get("photo_hashes", []))
    excluded = set(c.get("excluded_hashes", []))
    return hashes - excluded


def _load_clip_model():
    """Load the CLIP text encoder in background for runtime search."""
    if _ai["clip_emb"] is None:
        return
    try:
        import torch
        import open_clip
        # Prefer GPU when available — RTX 3080 passthrough makes text encoding ~10× faster.
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model, _, _ = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="laion2b_s34b_b79k"
        )
        model.eval()
        model = model.to(device)
        _ai["model"] = model
        _ai["tokenizer"] = open_clip.get_tokenizer("ViT-B-32")
        _ai["device"] = device
        _ai["ready"] = True
        if device == "cuda":
            try:
                gpu_name = torch.cuda.get_device_name(0)
                print(f"[ai] CLIP text encoder loaded on GPU ({gpu_name}) — search is ready.")
            except Exception:
                print("[ai] CLIP text encoder loaded on GPU — search is ready.")
        else:
            print("[ai] CLIP text encoder loaded on CPU — search is ready.")
        # Bust summary cache so covers get re-picked with CLIP aesthetic scoring
        _summary_cache["data"] = None
        _summary_cache["mtime"] = 0
        print("[ai] Summary cache cleared — covers will use CLIP aesthetic scoring.")
    except ImportError:
        print("[ai] torch/open_clip not installed — search disabled.")
    except Exception as e:
        print(f"[ai] Failed to load CLIP model: {e}")


# ─── CLIP aesthetic cover selection ───

_aesthetic_emb = None
_aesthetic_emb_lock = threading.Lock()

AESTHETIC_QUERIES = [
    "beautiful scenic landscape photography",
    "stunning sunset golden hour sky clouds",
    "aesthetic nature photo mountains ocean",
    "travel photography breathtaking view",
    "vibrant outdoor scenery",
    "cinematic landscape photo",
]

PORTRAIT_QUERIES = [
    "a clear well-lit portrait photo of a person smiling",
    "a close-up selfie of a happy person looking at the camera",
    "a nice headshot photo with good lighting",
    "a person posing for the camera with a clear face",
]

_portrait_emb = None
_portrait_emb_lock = threading.Lock()


def _get_portrait_embedding():
    """Compute (once) a CLIP embedding representing 'good portrait photo'."""
    global _portrait_emb
    if _portrait_emb is not None:
        return _portrait_emb
    if not _ai["ready"]:
        return None
    import torch, numpy as np
    with _portrait_emb_lock:
        if _portrait_emb is not None:
            return _portrait_emb
        try:
            tokenizer = _ai["tokenizer"]
            model     = _ai["model"]
            device    = _ai.get("device", "cpu")
            texts = tokenizer(PORTRAIT_QUERIES)
            if device == "cuda":
                texts = texts.to(device)
            with torch.no_grad():
                embs = model.encode_text(texts)
                embs = embs / embs.norm(dim=-1, keepdim=True)
                avg  = embs.mean(dim=0)
                avg  = (avg / avg.norm()).cpu().numpy()
            _portrait_emb = avg
            print("[ai] Portrait embedding computed.")
        except Exception as e:
            print(f"[ai] Could not compute portrait embedding: {e}")
    return _portrait_emb


def _get_aesthetic_embedding():
    """Compute (once) a CLIP embedding representing 'aesthetic scenic photo'."""
    global _aesthetic_emb
    if _aesthetic_emb is not None:
        return _aesthetic_emb
    if not _ai["ready"]:
        return None
    import torch, numpy as np
    with _aesthetic_emb_lock:
        if _aesthetic_emb is not None:
            return _aesthetic_emb
        try:
            tokenizer = _ai["tokenizer"]
            model     = _ai["model"]
            device    = _ai.get("device", "cpu")
            texts = tokenizer(AESTHETIC_QUERIES)
            if device == "cuda":
                texts = texts.to(device)
            with torch.no_grad():
                embs = model.encode_text(texts)
                embs = embs / embs.norm(dim=-1, keepdim=True)
                avg  = embs.mean(dim=0)
                avg  = (avg / avg.norm()).cpu().numpy()
            _aesthetic_emb = avg
            print("[ai] Aesthetic embedding computed.")
        except Exception as e:
            print(f"[ai] Could not compute aesthetic embedding: {e}")
    return _aesthetic_emb


def _pick_aesthetic_covers(candidates, max_covers=6):
    """Pick cover photos using CLIP aesthetic scoring.

    Scores each candidate against an average 'aesthetic scenic' text embedding,
    then among the top scorers still prefers landscape orientation.
    Falls back to _pick_covers() if CLIP is not ready.
    """
    import numpy as np

    if not _ai["ready"] or _ai["clip_emb"] is None or not candidates:
        return _pick_covers(candidates, max_covers)

    aes_emb = _get_aesthetic_embedding()
    if aes_emb is None:
        return _pick_covers(candidates, max_covers)

    # Score every candidate by cosine similarity to the aesthetic embedding
    scored = []
    for c in candidates:
        thumb = c.get("thumb", "")
        h = thumb.rsplit("/", 1)[-1].replace(".jpg", "")
        idx = _ai["hash_to_idx"].get(h)
        if idx is not None:
            score = float(_ai["clip_emb"][idx] @ aes_emb)
            scored.append((score, c))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Take the top 3× pool, then within that prefer landscape orientation
    pool_size = max_covers * 3
    top = [c for _, c in scored[:pool_size]]

    landscape = [c for c in top if _is_landscape(c["thumb"])]
    non_landscape = [c for c in top if not _is_landscape(c["thumb"])]

    import random
    result = landscape[:max_covers]
    if len(result) < max_covers:
        result.extend(non_landscape[:max_covers - len(result)])
    if len(result) < max_covers:
        leftover = [c for _, c in scored[pool_size:]]
        random.shuffle(leftover)
        result.extend(leftover[:max_covers - len(result)])

    random.shuffle(result)
    return [c["thumb"] for c in result[:max_covers]]


def _startup_ai():
    # Serial: face index first (sets clip_emb which _load_clip_model checks),
    # then CLIP text encoder, then avatar prewarm.
    # With vectorized emb_to_cluster: face index ~1.6s, CLIP ~2.7s, total ~4.3s
    # (vs previous ~36s due to O(N×C) exemplar loop).
    try:
        _load_ai_index()
        _load_clip_model()
    except Exception as e:
        print(f"[ai] Startup error: {e}")
    try:
        _prewarm_avatars()
    except Exception as e:
        print(f"[ai] Avatar prewarm error: {e}")


if _should_run_background():
    threading.Thread(target=_startup_ai, daemon=True).start()


# ─── Search caches ───
# hash→item lookup is rebuilt whenever the photo index changes.
_hash_to_item_cache = {"sig": None, "map": None}

def _get_hash_to_item():
    items = load_photo_index()
    sig = len(items)  # cheap signature — photo index is append-only enough
    cached = _hash_to_item_cache
    if cached["sig"] == sig and cached["map"] is not None:
        return cached["map"]
    m = {}
    for item in items:
        url = item.get("thumb", "")
        if url:
            h = url.rsplit("/", 1)[-1].replace(".jpg", "")
            m[h] = item
    cached["sig"] = sig
    cached["map"] = m
    return m


# Cache query→embedding. Typing "foo" → "foo " shouldn't re-encode "foo".
_query_emb_cache = {}  # lowercased text → np.ndarray
_QUERY_EMB_CACHE_MAX = 256

def _encode_query(text):
    import numpy as np
    import torch
    key = text.strip().lower()
    cached = _query_emb_cache.get(key)
    if cached is not None:
        return cached
    model = _ai["model"]
    tokenizer = _ai["tokenizer"]
    device = _ai.get("device", "cpu")
    tokens = tokenizer([text])
    if device == "cuda":
        tokens = tokens.to(device)
    with torch.no_grad():
        tf = model.encode_text(tokens)
        tf = (tf / tf.norm(dim=-1, keepdim=True)).squeeze().cpu().numpy()
    if len(_query_emb_cache) >= _QUERY_EMB_CACHE_MAX:
        # Evict oldest (insertion-order dict)
        _query_emb_cache.pop(next(iter(_query_emb_cache)))
    _query_emb_cache[key] = tf
    return tf


@app.route("/api/photos/search")
@require_auth
def api_search_photos():
    """People-aware semantic photo search powered by CLIP + face clusters."""
    import numpy as np

    q = request.args.get("q", "").strip()
    limit = request.args.get("limit", 200, type=int)

    if not q:
        return jsonify({"results": [], "query": ""})

    matched_people, semantic_text = _parse_people_query(q)

    # Build people info for response
    clusters = _ai.get("face_clusters") or {}
    people_info = []
    for name, cid in matched_people:
        c = clusters.get(cid, {})
        people_info.append({
            "name": c.get("name", name),
            "cluster_id": cid,
            "photo_count": c.get("photo_count", 0),
            "sample_face": c.get("sample_face", ""),
        })

    # Build hash→item lookup (cached across requests, invalidated on index change)
    hash_to_item = _get_hash_to_item()
    # Honor the hardcoded hidden-name blocklist (and existing hidden /
    # screenshot / dedup exclusions) in search just like the gallery does.
    hidden = _get_hidden_hashes() | _get_screenshot_hashes() | _get_duplicate_hashes() | _get_vault_hashes()
    def _lookup(h):
        if h in hidden:
            return None
        return hash_to_item.get(h)

    # Case 1: People only (no semantic text)
    if matched_people and not semantic_text:
        # Intersect photo sets from all matched people
        person_sets = [_get_person_hashes(cid) for _, cid in matched_people]
        combined = person_sets[0]
        for s in person_sets[1:]:
            combined = combined & s

        results = []
        for h in combined:
            item = _lookup(h)
            if item:
                results.append(item)
        results.sort(key=lambda x: -x.get("date", 0))
        results = results[:limit]

        return jsonify({
            "results": results, "query": q,
            "people": people_info, "sort": "date",
        })

    # Case 2: People + semantic text (CLIP-score only that person's photos)
    # Case 3: Pure semantic (no people matched)
    clip_available = _ai["clip_emb"] is not None and _ai["ready"]
    if not clip_available and matched_people:
        # CLIP unavailable but we have matched people — fall back to people-only results
        person_sets = [_get_person_hashes(cid) for _, cid in matched_people]
        combined = person_sets[0]
        for s in person_sets[1:]:
            combined = combined & s
        results = []
        for h in combined:
            item = _lookup(h)
            if item:
                results.append(item)
        results.sort(key=lambda x: -x.get("date", 0))
        results = results[:limit]
        return jsonify({
            "results": results, "query": q,
            "people": people_info, "sort": "date",
        })
    if _ai["clip_emb"] is None:
        return jsonify({"error": "AI index not built. SSH into the NAS and run: python ai_indexer.py"}), 404
    if not _ai["ready"]:
        # CLIP text encoder is still loading in the background — return a soft warming signal
        # instead of a 503 error so the frontend can show a non-alarming status.
        return jsonify({
            "results": [], "query": q, "people": people_info,
            "warming": True, "sort": "date",
        })

    clip_query = semantic_text if semantic_text else q
    tf = _encode_query(clip_query)
    scores = _ai["clip_emb"] @ tf

    if matched_people:
        # Intersect person sets, then rank by CLIP score within that subset
        person_sets = [_get_person_hashes(cid) for _, cid in matched_people]
        allowed = person_sets[0]
        for s in person_sets[1:]:
            allowed = allowed & s

        # Guard against index/embedding desync: clip_hashes can drift longer
        # than scores when new photos are appended to the index between the
        # embedding matmul and this iteration.
        n = min(len(scores), len(_ai["clip_hashes"]))
        scored = []
        for i in range(n):
            h = _ai["clip_hashes"][i]
            if h in allowed:
                scored.append((i, float(scores[i])))
        scored.sort(key=lambda x: -x[1])
        scored = scored[:limit]

        results = []
        for idx, score in scored:
            h = _ai["clip_hashes"][idx]
            item = _lookup(h)
            if item and score > 0.15:
                results.append({**item, "score": round(score, 3)})

        return jsonify({
            "results": results, "query": q,
            "people": people_info, "sort": "relevance",
        })

    # Pure semantic search (no people)
    n = min(len(scores), len(_ai["clip_hashes"]))
    top_idx = np.argsort(scores[:n])[::-1][:limit]
    results = []
    for idx in top_idx:
        h = _ai["clip_hashes"][idx]
        item = _lookup(h)
        score = float(scores[idx])
        if item and score > 0.18:
            results.append({**item, "score": round(score, 3)})

    return jsonify({
        "results": results, "query": q,
        "people": [], "sort": "relevance",
    })


@app.route("/api/photos/search/status")
@require_auth
def api_search_status():
    """Check if AI search is available."""
    has_index = _ai["clip_emb"] is not None
    count = len(_ai["clip_hashes"]) if _ai["clip_hashes"] else 0
    faces = len(_ai["face_clusters"]) if _ai["face_clusters"] else 0
    return jsonify({
        "indexed": has_index,
        "ready": _ai["ready"],
        "count": count,
        "faces": faces,
    })


@app.route("/api/photos/people/names")
@require_auth
def api_people_names():
    """Return named, non-hidden people for search autocomplete."""
    clusters = _ai.get("face_clusters") or {}
    result = []
    for cid, c in clusters.items():
        name = (c.get("name") or "").strip()
        if name and not c.get("hidden"):
            result.append({
                "name": name,
                "cluster_id": cid,
                "photo_count": c.get("photo_count", 0),
                "sample_face": c.get("sample_face", ""),
            })
    result.sort(key=lambda x: x["name"].lower())
    return jsonify(result)


@app.route("/api/photos/people/create", methods=["POST"])
@require_auth
def api_people_create():
    """Create a new person cluster from seed photos. Expands to similar faces automatically."""
    data = request.json or {}
    name = (data.get("name") or "").strip()
    seed_hashes = list(data.get("photo_hashes", []))

    if not name:
        return jsonify({"error": "Name required"}), 400
    if not seed_hashes:
        return jsonify({"error": "No photos selected"}), 400

    face_index = _ai.get("face_index")
    face_embs = _ai.get("face_embs")
    clusters = _ai.get("face_clusters") or {}

    if face_index is None or face_embs is None or len(face_embs) == 0:
        return jsonify({"error": "Face data not loaded — run ai_indexer.py --rescan-faces first"}), 503

    # Check for duplicate name
    for c in clusters.values():
        if (c.get("name") or "").lower() == name.lower():
            return jsonify({"error": f"'{name}' already exists"}), 409

    # Collect face embeddings from seed photos
    seed_emb_indices = []
    emb_to_hash = {}
    for ph, faces in face_index.items():
        for face in faces:
            emb_to_hash[face["emb_idx"]] = ph
    for h in seed_hashes:
        for face in face_index.get(h, []):
            idx = face["emb_idx"]
            if idx < len(face_embs):
                seed_emb_indices.append(idx)

    if not seed_emb_indices:
        return jsonify({"error": "No faces detected in the selected photos"}), 400

    import numpy as np
    seed_arr = np.array(seed_emb_indices)

    # Refine centroid: seed photos may contain other people's faces (group photos).
    # Iteratively drop embeddings that are far from the centroid so we converge
    # on just this person's face cluster.
    active = seed_arr.copy()
    for _ in range(5):
        centroid = face_embs[active].mean(axis=0)
        dists_seed = np.linalg.norm(face_embs[active] - centroid, axis=1)
        cutoff = dists_seed.mean() + dists_seed.std()
        filtered = active[dists_seed <= cutoff]
        if len(filtered) < max(3, len(active) * 0.5):
            break  # don't over-prune
        if len(filtered) == len(active):
            break  # converged
        active = filtered

    centroid = face_embs[active].mean(axis=0)

    # Expand: assign all embeddings within threshold to this person
    EXPAND_THRESH = 0.75
    assigned = set(seed_hashes)
    dists = np.linalg.norm(face_embs - centroid, axis=1)
    for emb_idx, dist in enumerate(dists):
        if dist < EXPAND_THRESH:
            ph = emb_to_hash.get(emb_idx)
            if ph:
                assigned.add(ph)

    # Find best sample face (seed embedding closest to centroid)
    best_hash = seed_hashes[0]
    best_dist = float("inf")
    for idx in seed_emb_indices:
        d = float(np.linalg.norm(face_embs[idx] - centroid))
        if d < best_dist:
            best_dist = d
            best_hash = emb_to_hash.get(idx, seed_hashes[0])

    new_id = str(max((int(k) for k in clusters.keys()), default=-1) + 1)
    photo_hashes = sorted(assigned)
    clusters[new_id] = {
        "name": name,
        "photo_count": len(photo_hashes),
        "face_count": len(seed_emb_indices),
        "sample_face": best_hash,
        "photo_hashes": photo_hashes,
        "excluded_hashes": [],
    }

    fc_path = os.path.join(_AI_DIR, "face_clusters.json")
    _atomic_write_json(fc_path, clusters)
    _ai["face_clusters"] = clusters
    _rebuild_name_map()

    return jsonify({"status": "ok", "id": new_id, "name": name, "photo_count": len(photo_hashes)})


@app.route("/api/photos/faces")
@require_auth
def api_faces():
    """Return face clusters for people browsing."""
    clusters = _ai.get("face_clusters")
    ss = _get_screenshot_hashes()
    if not clusters:
        return jsonify({"clusters": [], "screenshot_count": len(ss)})

    result = []
    for cid, c in clusters.items():
        if _is_cluster_hidden(c):
            continue
        result.append({
            "id": cid,
            "name": c.get("name", ""),
            "photo_count": c.get("photo_count", 0),
            "face_count": c.get("face_count", 0),
            "sample_face": c.get("sample_face", 0),
        })
    return jsonify({"clusters": result, "screenshot_count": len(ss)})


@app.route("/api/photos/screenshots")
@require_auth
def api_screenshots():
    """Return all photos classified as screenshots/documents."""
    ss_hashes = _get_screenshot_hashes()
    if not ss_hashes:
        return jsonify([])
    items = load_photo_index()
    results = []
    for item in items:
        url = item.get("thumb", "")
        if url:
            h = url.rsplit("/", 1)[-1].replace(".jpg", "")
            if h in ss_hashes:
                results.append(item)
    results.sort(key=lambda x: -x.get("date", 0))
    return jsonify(results)


# ═══════════════════════════════════════════════════════════════════════════════
# FAVORITES  +  ON THIS DAY  +  EXIF PANEL   (Immich-style additions)
# Favorites are keyed by photo hash (the thumb md5 stem) — the same stable id
# used by screenshots, vault, and face clusters.
# ═══════════════════════════════════════════════════════════════════════════════

_FAVORITES_PATH = os.path.join(_AI_DIR, "favorites.json")
_favorites_lock = threading.Lock()
_favorites_cache = {"data": None, "mtime": -1.0}


def _item_hash(item):
    """Stable per-photo id = thumb filename stem. '' if absent."""
    url = item.get("thumb", "") if item else ""
    if not url:
        return ""
    return url.rsplit("/", 1)[-1].replace(".jpg", "")


def _get_favorite_hashes():
    """Return set of favorited photo hashes, reloaded when the file changes."""
    try:
        mtime = os.path.getmtime(_FAVORITES_PATH)
    except OSError:
        return set()
    if _favorites_cache["data"] is not None and _favorites_cache["mtime"] == mtime:
        return _favorites_cache["data"]
    try:
        with open(_FAVORITES_PATH) as f:
            data = set(json.load(f))
    except (OSError, ValueError):
        data = set()
    _favorites_cache["data"] = data
    _favorites_cache["mtime"] = mtime
    return data


def _save_favorite_hashes(hashes):
    """Atomically persist the favorites set. Returns the new count."""
    assert isinstance(hashes, (set, list)), "hashes must be a collection"
    payload = sorted(hashes)
    tmp = _FAVORITES_PATH + ".tmp"
    with _favorites_lock:
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, _FAVORITES_PATH)
    _favorites_cache["data"] = set(payload)
    try:
        _favorites_cache["mtime"] = os.path.getmtime(_FAVORITES_PATH)
    except OSError:
        _favorites_cache["mtime"] = -1.0
    return len(payload)


@app.route("/api/photos/favorites")
@require_auth
def api_favorites():
    """Return all favorited photos, newest first."""
    favs = _get_favorite_hashes()
    if not favs:
        return jsonify([])
    items = load_photo_index()
    results = [it for it in items if _item_hash(it) in favs]
    results.sort(key=lambda x: -x.get("date", 0))
    return jsonify(results)


@app.route("/api/photos/favorite", methods=["POST"])
@require_auth
def api_favorite_toggle():
    """Set or toggle favorite state for one photo hash.
    Body: {"hash": "<md5>", "favorite": true|false|null}.
    When "favorite" is omitted/null the state is toggled."""
    body = request.get_json(silent=True) or {}
    h = (body.get("hash") or "").strip()
    if not h or "/" in h or "." in h:
        return jsonify({"error": "invalid hash"}), 400
    favs = set(_get_favorite_hashes())
    want = body.get("favorite", None)
    new_state = (h not in favs) if want is None else bool(want)
    if new_state:
        favs.add(h)
    else:
        favs.discard(h)
    count = _save_favorite_hashes(favs)
    return jsonify({"ok": True, "hash": h, "favorited": new_state, "count": count})


@app.route("/api/photos/memories")
@require_auth
def api_memories():
    """On This Day: photos from the same month/day in prior years.
    Query: ?month=1-12&day=1-31 (defaults to today in the gallery timezone).
    Returns groups [{year, count, items:[...]}] newest year first, excluding
    the current year and any screenshot/hidden/vault/duplicate photos."""
    now = datetime.now(tz=_GALLERY_TZ)
    try:
        month = int(request.args.get("month", now.month))
        day = int(request.args.get("day", now.day))
    except (TypeError, ValueError):
        return jsonify({"error": "month and day must be integers"}), 400
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return jsonify({"error": "month/day out of range"}), 400

    exclude = (_get_screenshot_hashes() | _get_hidden_hashes()
               | _get_vault_hashes() | _get_duplicate_hashes())
    by_year = {}
    for it in load_photo_index():
        if _item_hash(it) in exclude:
            continue
        try:
            dt = datetime.fromtimestamp(it.get("date", 0), tz=_GALLERY_TZ)
        except (OSError, OverflowError, ValueError):
            continue
        if dt.month != month or dt.day != day or dt.year == now.year:
            continue
        by_year.setdefault(dt.year, []).append(it)

    groups = []
    for year in sorted(by_year, reverse=True):
        items = sorted(by_year[year], key=lambda x: -x.get("date", 0))
        groups.append({"year": year, "count": len(items),
                       "years_ago": now.year - year, "items": items})
    return jsonify({"month": month, "day": day, "groups": groups})


# Curated EXIF tags surfaced in the lightbox info panel, plus a small cache.
_EXIF_FIELDS = [
    "Make", "Model", "LensModel", "LensID", "LensInfo",
    "ImageWidth", "ImageHeight", "ImageSize", "Megapixels",
    "FNumber", "Aperture", "ExposureTime", "ShutterSpeed",
    "ISO", "FocalLength", "FocalLengthIn35mmFormat",
    "GPSLatitude", "GPSLongitude", "GPSPosition", "GPSAltitude",
    "DateTimeOriginal", "CreateDate", "FileType", "MIMEType",
    "FileSize", "Duration", "VideoFrameRate", "Software", "Orientation",
]
_exif_cache = {}  # hash -> dict
_exif_cache_lock = threading.Lock()


@app.route("/api/photos/exif")
@require_auth
def api_exif():
    """Return curated EXIF metadata for one photo (read on demand via exiftool).
    Query: ?path=<canonical index path>. Cached in memory by photo hash."""
    raw_path = request.args.get("path", "")
    if not raw_path or ".." in raw_path:
        return jsonify({"error": "invalid path"}), 400

    # Resolve to a readable file: literal index path first, then alias.
    real = raw_path if os.path.isfile(raw_path) else resolve_media_path(raw_path)
    if not real or not os.path.isfile(real):
        return jsonify({"error": "file not found", "fields": {}}), 404

    cache_key = real
    with _exif_cache_lock:
        if cache_key in _exif_cache:
            return jsonify(_exif_cache[cache_key])

    try:
        out = subprocess.run(
            ["exiftool", "-json", "-n", "-coordFormat", "%.6f", real],
            capture_output=True, text=True, timeout=15,
        )
        meta = (json.loads(out.stdout) or [{}])[0] if out.stdout.strip() else {}
    except (subprocess.SubprocessError, ValueError, OSError):
        meta = {}

    fields = {k: meta[k] for k in _EXIF_FIELDS if k in meta and meta[k] not in (None, "")}
    lat, lon = meta.get("GPSLatitude"), meta.get("GPSLongitude")
    gps = None
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        gps = {"lat": round(lat, 6), "lon": round(lon, 6)}
    result = {"fields": fields, "gps": gps,
              "name": os.path.basename(real), "path": raw_path}
    with _exif_cache_lock:
        if len(_exif_cache) < 5000:
            _exif_cache[cache_key] = result
    return jsonify(result)


@app.route("/api/photos/face/<cluster_id>")
@require_auth
def api_face_photos(cluster_id):
    """Return all photos for a specific face cluster."""
    clusters = _ai.get("face_clusters")
    if not clusters or cluster_id not in clusters:
        return jsonify([])

    photo_hashes = set(clusters[cluster_id].get("photo_hashes", []))
    new_hashes = set(clusters[cluster_id].get("new_hashes", [])) | set(clusters[cluster_id].get("review_hashes", []))
    exclude = _get_hidden_hashes() | _get_screenshot_hashes() | _get_duplicate_hashes() | _get_vault_hashes()
    items = load_photo_index()
    results = []
    for item in items:
        url = item.get("thumb", "")
        if url:
            h = url.rsplit("/", 1)[-1].replace(".jpg", "")
            if h in photo_hashes and h not in exclude:
                item_copy = dict(item)
                if h in new_hashes:
                    item_copy["is_new"] = True
                results.append(item_copy)
    # New photos first, then by date
    results.sort(key=lambda x: (0 if x.get("is_new") else 1, -x.get("date", 0)))
    return jsonify(results)


@app.route("/api/photos/<photo_hash>/faces")
@require_auth
def api_photo_faces(photo_hash):
    """Which faces the system detected in one photo and who it identifies each as.
    Returns [{bbox, det_score, name, distance, confident}]. name is the cluster that
    owns the face's emb_idx (confident); otherwise the nearest exemplar guess."""
    assert photo_hash and "/" not in photo_hash, "bad hash"
    import numpy as np
    fi_path = os.path.join(_AI_DIR, "face_index.json")
    fe_path = os.path.join(_AI_DIR, "face_embeddings.npy")
    clusters = _ai.get("face_clusters") or {}
    try:
        with open(fi_path) as f:
            faces = json.load(f).get(photo_hash, [])
    except (OSError, ValueError):
        faces = []
    if not faces:
        return jsonify([])

    # emb_idx -> confident owner name (from each cluster's clustered faces)
    owner = {}
    exemplars, ex_names = [], []
    for c in clusters.values():
        nm = c.get("name")
        for i in c.get("emb_indices", []):
            if nm:
                owner[i] = nm
        for ex in c.get("exemplars", []):
            exemplars.append(ex)
            ex_names.append(nm or "?")
    ex_arr = np.asarray(exemplars, dtype=np.float32) if exemplars else None
    embs = np.load(fe_path, mmap_mode="r") if os.path.exists(fe_path) else None

    ASSIGN_THRESH = 1.05  # matches expand_named_clusters
    out = []
    for fc in faces:
        ei = fc.get("emb_idx")
        row = {"bbox": fc.get("bbox"), "det_score": round(fc.get("det_score", 0), 3),
               "name": None, "distance": None, "confident": False}
        if isinstance(ei, int) and ei in owner:
            row["name"] = owner[ei]
            row["confident"] = True
        elif isinstance(ei, int) and ex_arr is not None and embs is not None and ei < len(embs):
            d = np.linalg.norm(ex_arr - np.asarray(embs[ei], dtype=np.float32), axis=1)
            j = int(np.argmin(d))
            row["distance"] = round(float(d[j]), 3)
            if d[j] < ASSIGN_THRESH:
                row["name"] = ex_names[j]
        out.append(row)
    out.sort(key=lambda r: -(r["det_score"] or 0))
    return jsonify(out)


@app.route("/api/photos/face/<cluster_id>/name", methods=["POST"])
@require_auth
def api_name_face(cluster_id):
    """Set a display name for a face cluster."""
    clusters = _ai.get("face_clusters")
    if not clusters or cluster_id not in clusters:
        return jsonify({"error": "Cluster not found"}), 404

    name = request.json.get("name", "").strip()
    clusters[cluster_id]["name"] = name

    fc_path = os.path.join(_AI_DIR, "face_clusters.json")
    _atomic_write_json(fc_path, clusters)
    _rebuild_name_map()

    return jsonify({"status": "ok", "name": name})


@app.route("/api/photos/faces/merge", methods=["POST"])
@require_auth
def api_merge_faces():
    """Merge two face clusters (source into target). Keeps target's name."""
    clusters = _ai.get("face_clusters")
    if not clusters:
        return jsonify({"error": "No face data"}), 404

    source_id = str(request.json.get("source", ""))
    target_id = str(request.json.get("target", ""))

    if source_id not in clusters or target_id not in clusters:
        return jsonify({"error": "Cluster not found"}), 404
    if source_id == target_id:
        return jsonify({"error": "Cannot merge cluster with itself"}), 400

    src = clusters[source_id]
    tgt = clusters[target_id]

    merged_hashes = list(set(tgt.get("photo_hashes", []) + src.get("photo_hashes", [])))
    tgt["photo_hashes"] = merged_hashes
    tgt["photo_count"] = len(merged_hashes)
    tgt["face_count"] = tgt.get("face_count", 0) + src.get("face_count", 0)
    if not tgt.get("name") and src.get("name"):
        tgt["name"] = src["name"]

    del clusters[source_id]

    fc_path = os.path.join(_AI_DIR, "face_clusters.json")
    _atomic_write_json(fc_path, clusters)
    _rebuild_name_map()
    _invalidate_avatar(source_id)
    _invalidate_avatar(target_id)

    return jsonify({
        "status": "ok",
        "target_id": target_id,
        "name": tgt["name"],
        "photo_count": tgt["photo_count"],
    })


@app.route("/api/photos/face/<cluster_id>/remove", methods=["POST"])
@require_auth
def api_remove_from_face(cluster_id):
    """Remove photos from a face cluster (they don't belong to this person)."""
    clusters = _ai.get("face_clusters")
    if not clusters or cluster_id not in clusters:
        return jsonify({"error": "Cluster not found"}), 404

    hashes_to_remove = set(request.json.get("hashes", []))
    if not hashes_to_remove:
        return jsonify({"error": "No hashes provided"}), 400

    c = clusters[cluster_id]
    before = len(c.get("photo_hashes", []))
    c["photo_hashes"] = [h for h in c["photo_hashes"] if h not in hashes_to_remove]
    c["photo_count"] = len(c["photo_hashes"])
    removed = before - c["photo_count"]

    # Persist excluded hashes so re-clustering doesn't bring them back
    excluded = set(c.get("excluded_hashes", []))
    excluded.update(hashes_to_remove)
    c["excluded_hashes"] = list(excluded)

    fc_path = os.path.join(_AI_DIR, "face_clusters.json")
    _atomic_write_json(fc_path, clusters)
    _invalidate_avatar(cluster_id)

    return jsonify({
        "status": "ok",
        "removed": removed,
        "remaining": c["photo_count"],
    })


@app.route("/api/photos/faces/suggestions")
@require_auth
def api_face_suggestions():
    """Return merge suggestions: pairs of clusters that might be the same person."""
    import numpy as np

    clusters = _ai.get("face_clusters")
    if not clusters:
        return jsonify({"suggestions": []})

    emb_path = os.path.join(_AI_DIR, "face_embeddings.npy")
    fi_path = os.path.join(_AI_DIR, "face_index.json")
    if not os.path.exists(emb_path) or not os.path.exists(fi_path):
        return jsonify({"suggestions": []})

    embs = np.load(emb_path)
    with open(fi_path) as f:
        face_data = json.load(f)

    emb_to_hash = {}
    for photo_hash, faces in face_data.items():
        for face in faces:
            emb_to_hash[face["emb_idx"]] = photo_hash

    # Compute centroids for each cluster
    centroids = {}
    for cid, c in clusters.items():
        hset = set(c.get("photo_hashes", []))
        idxs = [idx for idx, h in emb_to_hash.items() if h in hset]
        if idxs:
            centroids[cid] = embs[idxs].mean(axis=0)

    cids = list(centroids.keys())
    suggestions = []
    for i in range(len(cids)):
        for j in range(i + 1, len(cids)):
            a, b = clusters[cids[i]], clusters[cids[j]]
            na, nb = a.get("name", ""), b.get("name", "")
            # Skip if both are named differently -- obviously different people
            if na and nb and na != nb:
                continue
            dist = float(np.linalg.norm(centroids[cids[i]] - centroids[cids[j]]))
            if dist < 0.45:
                suggestions.append({
                    "cluster_a": cids[i],
                    "cluster_b": cids[j],
                    "name_a": na,
                    "name_b": nb,
                    "count_a": a.get("photo_count", 0),
                    "count_b": b.get("photo_count", 0),
                    "face_a": a.get("sample_face", 0),
                    "face_b": b.get("sample_face", 0),
                    "distance": round(dist, 3),
                })
    suggestions.sort(key=lambda x: x["distance"])
    return jsonify({"suggestions": suggestions[:30]})



@app.route("/api/photos/faces/in-photo")
@require_auth
def api_faces_in_photo():
    """Return all detected faces in a photo with their cluster assignments."""
    photo_hash = request.args.get("hash", "")
    face_index = _ai.get("face_index")
    clusters = _ai.get("face_clusters")
    emb_to_cluster = _ai.get("emb_to_cluster")

    if not face_index or not clusters:
        return jsonify([])

    faces = face_index.get(photo_hash, [])
    result = []
    for face in faces:
        emb_idx = face["emb_idx"]
        cid = emb_to_cluster.get(emb_idx) if emb_to_cluster else None
        c = clusters.get(cid, {}) if cid else {}
        result.append({
            "emb_idx": emb_idx,
            "bbox": face["bbox"],
            "crop_url": f"/static/faces/{emb_idx}.jpg",
            "cluster_id": cid,
            "name": c.get("name") or "Unknown",
        })
    return jsonify(result)


@app.route("/api/photos/people/in-photo")
@require_auth
def api_people_in_photo():
    """Return named people in a photo by cluster membership lookup."""
    photo_hash = request.args.get("hash", "")
    clusters = _ai.get("face_clusters") or {}
    if not photo_hash or not clusters:
        return jsonify([])
    result = []
    for cid, c in clusters.items():
        if c.get("hidden"):
            continue
        name = (c.get("name") or "").strip()
        if not name:
            continue
        excluded = set(c.get("excluded_hashes", []))
        if photo_hash in set(c.get("photo_hashes", [])) and photo_hash not in excluded:
            result.append({
                "name": name,
                "cluster_id": cid,
                "sample_face": c.get("sample_face", ""),
            })
    return jsonify(result)


def _square_face_crop(thumb_path, bbox, size=200):
    """Crop a square region centered on a face from a thumbnail."""
    from PIL import Image
    import io
    img = Image.open(thumb_path).convert("RGB")
    tw, th = img.size
    top, right, bottom, left = bbox
    # Scale bbox if detected on 1024px original, not thumbnail
    if right > tw or bottom > th:
        if tw >= th:
            ow, oh = 1024, round(th * 1024 / tw)
        else:
            oh, ow = 1024, round(tw * 1024 / th)
        sx, sy = tw / ow, th / oh
        top, bottom = int(top * sy), int(bottom * sy)
        left, right = int(left * sx), int(right * sx)
    cx = (left + right) / 2
    cy = (top + bottom) / 2
    side = max(right - left, bottom - top) * 1.6  # face + context
    # Shrink side if it can't fit in the image at all
    side = min(side, tw, th)
    half = side / 2
    # Clamp center so the square stays within image bounds
    cx = max(half, min(tw - half, cx))
    cy = max(half, min(th - half, cy))
    x1, y1 = int(cx - half), int(cy - half)
    x2, y2 = int(cx + half), int(cy + half)
    crop = img.crop((x1, y1, x2, y2)).resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    crop.save(buf, "JPEG", quality=88)
    buf.seek(0)
    return buf


_AVATAR_DIR = os.path.join(_APP_DIR, "static", "face_avatars")
os.makedirs(_AVATAR_DIR, exist_ok=True)


def _prewarm_avatars():
    """Pre-generate all missing avatar crops after startup so the first People tab
    open does not pay PIL crop latency for each of the 200+ clusters.
    Runs in a background thread; writes face_avatars/.fc_mtime stamp on completion."""
    clusters = _ai.get("face_clusters")
    face_embs = _ai.get("face_embs")
    if not clusters or face_embs is None:
        return
    import numpy as np
    from PIL import Image
    import io as _io
    done = 0
    skipped = 0
    fc_path = os.path.join(_AI_DIR, "face_clusters.json")
    for cluster_id, c in clusters.items():
        avatar_hash = c.get("avatar_hash")
        v = "11"
        if avatar_hash:
            cache_path = os.path.join(_AVATAR_DIR, f"{cluster_id}_v{v}_{avatar_hash[:8]}.jpg")
        else:
            cache_path = os.path.join(_AVATAR_DIR, f"{cluster_id}_v{v}.jpg")
        if os.path.exists(cache_path):
            skipped += 1
            continue
        # Try pre-cropped face images first (fast — just a resize)
        centroids = _ai.get("face_centroids") or {}
        centroid = centroids.get(cluster_id)
        stored_indices = c.get("emb_indices")
        generated = False
        if stored_indices and centroid is not None:
            valid = [i for i in stored_indices if i < len(face_embs)]
            if valid:
                dists = [(i, float(np.linalg.norm(face_embs[i] - centroid))) for i in valid]
                dists.sort(key=lambda x: x[1])
                for best_idx, _ in dists[:10]:
                    face_path = os.path.join(_APP_DIR, "static", "faces", f"{best_idx}.jpg")
                    if os.path.exists(face_path):
                        try:
                            img = Image.open(face_path).convert("RGB")
                            sq = img.resize((300, 300), Image.LANCZOS)
                            buf = _io.BytesIO()
                            sq.save(buf, "JPEG", quality=90)
                            buf.seek(0)
                            with open(cache_path, "wb") as fout:
                                fout.write(buf.read())
                            generated = True
                            done += 1
                            break
                        except Exception:
                            continue
        if not generated:
            skipped += 1
    # Write mtime stamp so next restart skips wipe if clusters unchanged
    _stamp_path = os.path.join(_AVATAR_DIR, ".fc_mtime")
    try:
        fc_mtime = os.path.getmtime(fc_path) if os.path.exists(fc_path) else 0.0
        with open(_stamp_path, "w") as sf:
            sf.write(str(fc_mtime))
    except OSError:
        pass
    print(f"[ai] Avatar prewarm complete: {done} generated, {skipped} skipped.")


def _invalidate_avatar(cluster_id):
    """Delete cached avatar for a cluster so it gets regenerated."""
    import glob as _glob
    for f in _glob.glob(os.path.join(_AVATAR_DIR, f"{cluster_id}_v*.jpg")):
        try:
            os.unlink(f)
        except OSError:
            pass


@app.route("/api/photos/people/<cluster_id>/avatar", methods=["GET", "POST"])
@require_auth
def api_person_avatar(cluster_id):
    """GET: serve avatar. POST: set manual avatar from a photo hash."""
    import numpy as np
    from PIL import Image
    import io

    clusters = _ai.get("face_clusters") or {}
    face_index = _ai.get("face_index")
    c = clusters.get(cluster_id)
    if not c:
        return jsonify({"error": "Not found"}), 404

    # --- POST: set manual avatar ---
    if request.method == "POST":
        data = request.json or {}
        photo_hash = data.get("hash")
        if not photo_hash or not face_index:
            return jsonify({"error": "Invalid request"}), 400

        emb_to_cluster = _ai.get("emb_to_cluster") or {}
        face_embs = _ai.get("face_embs")
        centroids = _ai.get("face_centroids") or {}
        centroid = centroids.get(cluster_id)
        faces = face_index.get(photo_hash, [])

        # Find this person's face — try emb_to_cluster, then nearest to centroid
        best_bbox = None
        best_size = 0
        for face in faces:
            if emb_to_cluster.get(face["emb_idx"]) == cluster_id:
                top, right, bottom, left = face["bbox"]
                sz = max(right - left, bottom - top)
                if sz > best_size:
                    best_size = sz
                    best_bbox = face["bbox"]

        # Fallback: pick the face closest to this person's centroid
        if not best_bbox and centroid is not None and face_embs is not None:
            best_dist = 999.0
            for face in faces:
                idx = face["emb_idx"]
                if idx < len(face_embs):
                    dist = float(np.linalg.norm(face_embs[idx] - centroid))
                    if dist < best_dist:
                        best_dist = dist
                        best_bbox = face["bbox"]

        # Last resort: largest face
        if not best_bbox:
            best_size = 0
            for face in faces:
                top, right, bottom, left = face["bbox"]
                sz = max(right - left, bottom - top)
                if sz > best_size:
                    best_size = sz
                    best_bbox = face["bbox"]

        chosen_bbox = best_bbox
        if not chosen_bbox:
            # No detected face at all — center crop the thumbnail
            thumb_path = os.path.join(_STATIC_DIR, "thumbs", photo_hash + ".jpg")
            if not os.path.exists(thumb_path):
                return jsonify({"error": "Photo not found"}), 404
            c["avatar_hash"] = photo_hash
            c["avatar_bbox"] = None
            fc_path = os.path.join(_AI_DIR, "face_clusters.json")
            _atomic_write_json(fc_path, clusters)
            _invalidate_avatar(cluster_id)
            try:
                img = Image.open(thumb_path).convert("RGB")
                w, h = img.size
                side = min(w, h)
                left = (w - side) // 2
                top = (h - side) // 2
                sq = img.crop((left, top, left + side, top + side))
                sq = sq.resize((300, 300), Image.LANCZOS)
                buf = io.BytesIO()
                sq.save(buf, "JPEG", quality=90)
                buf.seek(0)
                v = request.args.get("v", "11")
                cache_path = os.path.join(_AVATAR_DIR, f"{cluster_id}_v{v}_{photo_hash[:8]}.jpg")
                with open(cache_path, "wb") as fout:
                    fout.write(buf.read())
            except Exception:
                pass
            return jsonify({"status": "ok"})

        # Save the override
        c["avatar_hash"] = photo_hash
        c["avatar_bbox"] = list(chosen_bbox)
        fc_path = os.path.join(_AI_DIR, "face_clusters.json")
        _atomic_write_json(fc_path, clusters)

        # Generate and cache the avatar crop
        _invalidate_avatar(cluster_id)
        thumb_path = os.path.join(_STATIC_DIR, "thumbs", photo_hash + ".jpg")
        if os.path.exists(thumb_path):
            v = request.args.get("v", "11")
            cache_path = os.path.join(_AVATAR_DIR, f"{cluster_id}_v{v}_{photo_hash[:8]}.jpg")
            try:
                buf = _square_face_crop(thumb_path, chosen_bbox, size=300)
                data_bytes = buf.read()
                with open(cache_path, "wb") as fout:
                    fout.write(data_bytes)
            except Exception:
                pass

        return jsonify({"status": "ok"})

    # --- GET: serve avatar ---

    v = request.args.get("v", "11")
    avatar_hash = c.get("avatar_hash")

    # Cache key includes avatar_hash so manual PFPs never get stale algorithmic cache
    if avatar_hash:
        cache_path = os.path.join(_AVATAR_DIR, f"{cluster_id}_v{v}_{avatar_hash[:8]}.jpg")
    else:
        cache_path = os.path.join(_AVATAR_DIR, f"{cluster_id}_v{v}.jpg")

    if os.path.exists(cache_path):
        return send_file(cache_path, mimetype="image/jpeg",
                         max_age=86400, conditional=True)

    # Manual avatar override — use the pinned photo + stored bbox
    if avatar_hash:
        thumb_path = os.path.join(_STATIC_DIR, "thumbs", avatar_hash + ".jpg")
        if os.path.exists(thumb_path):
            # Use stored bbox, or find face via emb_to_cluster
            avatar_bbox = c.get("avatar_bbox")
            if not avatar_bbox and face_index:
                emb_to_cluster = _ai.get("emb_to_cluster") or {}
                faces = face_index.get(avatar_hash, [])
                best_sz = 0
                for face in faces:
                    top, right, bottom, left = face["bbox"]
                    sz = max(right - left, bottom - top)
                    if emb_to_cluster.get(face["emb_idx"]) == cluster_id and sz > best_sz:
                        best_sz = sz
                        avatar_bbox = face["bbox"]
                # Fallback: largest face in photo
                if not avatar_bbox:
                    for face in faces:
                        top, right, bottom, left = face["bbox"]
                        sz = max(right - left, bottom - top)
                        if sz > best_sz:
                            best_sz = sz
                            avatar_bbox = face["bbox"]
            if avatar_bbox:
                try:
                    buf = _square_face_crop(thumb_path, avatar_bbox, size=300)
                    data_bytes = buf.read()
                    with open(cache_path, "wb") as fout:
                        fout.write(data_bytes)
                    return send_file(cache_path, mimetype="image/jpeg",
                                     max_age=86400, conditional=True)
                except Exception:
                    pass

    if not face_index:
        return jsonify({"error": "Not found"}), 404

    face_embs = _ai.get("face_embs")
    centroids = _ai.get("face_centroids") or {}
    centroid = centroids.get(cluster_id)
    excluded = set(c.get("excluded_hashes", []))
    photo_hashes = [h for h in c.get("photo_hashes", []) if h not in excluded]

    # --- Direct face-crop approach ---
    # Find the face embedding that best represents this cluster and serve
    # its pre-cropped face image from /static/faces/{emb_idx}.jpg.
    # This is reliable because each face crop is guaranteed to be one person.
    stored_indices = c.get("emb_indices")
    if stored_indices and face_embs is not None and centroid is not None:
        # Use stored embedding indices (authoritative from clustering)
        valid = [i for i in stored_indices if i < len(face_embs)]
        if valid:
            dists = [(i, float(np.linalg.norm(face_embs[i] - centroid))) for i in valid]
            dists.sort(key=lambda x: x[1])
            for best_idx, _ in dists[:10]:
                face_path = os.path.join(_STATIC_DIR, "faces", f"{best_idx}.jpg")
                if os.path.exists(face_path):
                    try:
                        img = Image.open(face_path).convert("RGB")
                        sq = img.resize((300, 300), Image.LANCZOS)
                        buf = io.BytesIO()
                        sq.save(buf, "JPEG", quality=90)
                        buf.seek(0)
                        data = buf.read()
                        with open(cache_path, "wb") as f:
                            f.write(data)
                        return send_file(cache_path, mimetype="image/jpeg",
                                         max_age=86400, conditional=True)
                    except Exception:
                        continue

    # Fallback: find best face from cluster's photos using centroid distance
    if face_embs is not None and centroid is not None:
        photo_set = set(photo_hashes)
        emb_to_hash_local = {}
        for ph in photo_hashes:
            for face in face_index.get(ph, []):
                emb_to_hash_local[face["emb_idx"]] = ph

        # For each photo, pick the face closest to centroid (this person's face)
        best_per_photo = []
        from collections import defaultdict
        photo_faces = defaultdict(list)
        for idx, ph in emb_to_hash_local.items():
            photo_faces[ph].append(idx)

        for ph, idxs in photo_faces.items():
            if len(idxs) == 1:
                idx = idxs[0]
            else:
                idx = min(idxs, key=lambda i: float(np.linalg.norm(face_embs[i] - centroid))
                          if i < len(face_embs) else 999.0)
            if idx < len(face_embs):
                d = float(np.linalg.norm(face_embs[idx] - centroid))
                best_per_photo.append((d, idx))

        # Sort by distance to centroid — closest = most representative
        best_per_photo.sort(key=lambda x: x[0])
        for _, best_idx in best_per_photo[:10]:
            face_path = os.path.join(_STATIC_DIR, "faces", f"{best_idx}.jpg")
            if os.path.exists(face_path):
                try:
                    img = Image.open(face_path).convert("RGB")
                    sq = img.resize((300, 300), Image.LANCZOS)
                    buf = io.BytesIO()
                    sq.save(buf, "JPEG", quality=90)
                    buf.seek(0)
                    data = buf.read()
                    with open(cache_path, "wb") as f:
                        f.write(data)
                    return send_file(cache_path, mimetype="image/jpeg",
                                     max_age=86400, conditional=True)
                except Exception:
                    continue

    return jsonify({"error": "No usable face found"}), 404


@app.route("/api/photos/face-crop/<photo_hash>/<int:emb_idx>")
@require_auth
def api_face_crop(photo_hash, emb_idx):
    """Serve a cropped face image with padding."""
    face_index = _ai.get("face_index")
    if not face_index:
        return jsonify({"error": "No face index"}), 404

    faces = face_index.get(photo_hash, [])
    face = next((f for f in faces if f["emb_idx"] == emb_idx), None)
    if not face:
        return jsonify({"error": "Face not found"}), 404

    thumb_path = os.path.join(_STATIC_DIR, "thumbs", photo_hash + ".jpg")
    if not os.path.exists(thumb_path):
        return jsonify({"error": "Photo not found"}), 404

    try:
        buf = _square_face_crop(thumb_path, face["bbox"], size=180)
        return send_file(buf, mimetype="image/jpeg",
                         max_age=86400, conditional=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/photos/face/move", methods=["POST"])
@require_auth
def api_move_face():
    """Move a photo from one face cluster to another."""
    data = request.json or {}
    photo_hash = data.get("hash")
    from_id = data.get("from_cluster")
    to_id = data.get("to_cluster")

    clusters = _ai.get("face_clusters")
    if not clusters:
        return jsonify({"error": "No face clusters"}), 404
    if not photo_hash or from_id not in clusters or to_id not in clusters:
        return jsonify({"error": "Invalid request"}), 400
    if from_id == to_id:
        return jsonify({"status": "ok", "moved": False})

    from_c = clusters[from_id]
    to_c = clusters[to_id]

    # Remove from source + add to excluded so re-cluster doesn't restore it
    if photo_hash in from_c.get("photo_hashes", []):
        from_c["photo_hashes"] = [h for h in from_c["photo_hashes"] if h != photo_hash]
        from_c["photo_count"] = len(from_c["photo_hashes"])
        excl = set(from_c.get("excluded_hashes", []))
        excl.add(photo_hash)
        from_c["excluded_hashes"] = list(excl)

    # Add to destination (avoid duplicates)
    if photo_hash not in to_c.get("photo_hashes", []):
        to_c.setdefault("photo_hashes", []).append(photo_hash)
        to_c["photo_count"] = len(to_c["photo_hashes"])
        # Un-exclude from destination if it was previously removed
        excl_to = set(to_c.get("excluded_hashes", []))
        if photo_hash in excl_to:
            excl_to.discard(photo_hash)
            if excl_to:
                to_c["excluded_hashes"] = list(excl_to)
            else:
                to_c.pop("excluded_hashes", None)

    fc_path = os.path.join(_AI_DIR, "face_clusters.json")
    _atomic_write_json(fc_path, clusters)
    _invalidate_avatar(from_id)
    _invalidate_avatar(to_id)

    return jsonify({"status": "ok", "moved": True,
                    "from_count": from_c["photo_count"],
                    "to_count": to_c["photo_count"]})


@app.route("/api/photos/face/review")
@require_auth
def api_face_review():
    """Return a batch of ambiguous faces for user review (Google Photos style)."""
    clusters = _ai.get("face_clusters")
    face_index = _ai.get("face_index")
    embs = _ai.get("face_embs")
    if not clusters or face_index is None or embs is None:
        return jsonify({"items": [], "remaining": 0})

    named = {cid: c for cid, c in clusters.items() if c.get("name")}
    if not named:
        return jsonify({"items": [], "remaining": 0})

    assigned = set()
    for c in clusters.values():
        assigned.update(c.get("photo_hashes", []))

    # Build per-cluster excluded sets so we skip already-rejected pairs
    cluster_excluded = {}
    for cid, c in clusters.items():
        excl = c.get("excluded_hashes")
        if excl:
            cluster_excluded[cid] = set(excl)

    emb_to_hash = {}
    for ph, faces in face_index.items():
        for face in faces:
            emb_to_hash[face["emb_idx"]] = ph

    # Compute centroids
    centroid_cids, centroid_vecs = [], []
    for cid, c in named.items():
        excluded = set(c.get("excluded_hashes", []))
        ph_set = set(c.get("photo_hashes", [])) - excluded
        idxs = [idx for idx, ph in emb_to_hash.items() if ph in ph_set and idx < len(embs)]
        if idxs:
            centroid_cids.append(cid)
            centroid_vecs.append(embs[idxs].mean(axis=0))

    if not centroid_vecs:
        return jsonify({"items": [], "remaining": 0})

    import numpy as _np
    centroid_mat = _np.array(centroid_vecs)

    THRESH = 1.05
    items = load_photo_index()
    hash_to_item = {}
    for item in items:
        url = item.get("thumb", "")
        if url:
            h = url.rsplit("/", 1)[-1].replace(".jpg", "")
            hash_to_item[h] = item

    candidates = []
    for emb_idx, ph in emb_to_hash.items():
        if ph in assigned or emb_idx >= len(embs):
            continue
        dists = _np.linalg.norm(centroid_mat - embs[emb_idx], axis=1)
        order = _np.argsort(dists)
        # Walk down candidates until we find one not excluded
        best_cid = None
        best_d = None
        second_d = 999
        for rank, oi in enumerate(order):
            cid = centroid_cids[int(oi)]
            d = float(dists[oi])
            excl = cluster_excluded.get(cid)
            if excl and ph in excl:
                continue  # already rejected for this person
            if best_cid is None:
                best_cid = cid
                best_d = d
            elif second_d == 999:
                second_d = d
                break
        if best_cid is None or best_d >= THRESH:
            continue
        candidates.append({
            "photo_hash": ph,
            "emb_idx": emb_idx,
            "best_cid": best_cid,
            "best_name": named[best_cid].get("name", ""),
            "best_dist": best_d,
            "margin": second_d - best_d,
            "sample_face": named[best_cid].get("sample_face", ""),
        })

    # Sort: highest confidence first (lowest distance, highest margin)
    candidates.sort(key=lambda x: (x["best_dist"] - x["margin"] * 2))

    # Skip already-reviewed photos (sent from frontend)
    skip_param = request.args.get("skip", "")
    skip_hashes = set(skip_param.split(",")) if skip_param else set()

    # Dedupe by photo hash (one review per photo)
    seen = set()
    unique = []
    for c in candidates:
        if c["photo_hash"] not in seen and c["photo_hash"] not in skip_hashes:
            seen.add(c["photo_hash"])
            unique.append(c)

    batch_size = int(request.args.get("batch", 20))
    batch = unique[:batch_size]

    result = []
    for c in batch:
        item = hash_to_item.get(c["photo_hash"])
        if not item:
            continue
        result.append({
            "photo_hash": c["photo_hash"],
            "thumb": item.get("thumb_hq") or item.get("thumb", ""),
            "path": item.get("path", ""),
            "suggested_cid": c["best_cid"],
            "suggested_name": c["best_name"],
            "suggested_avatar": c["sample_face"],
            "confidence": round(max(0, min(100, (1.05 - c["best_dist"]) / 1.05 * 100)), 1),
            "margin": round(c["margin"], 3),
        })

    return jsonify({"items": result, "remaining": len(unique)})


@app.route("/api/photos/face/review", methods=["POST"])
@require_auth
def api_face_review_submit():
    """Accept or reject a face review suggestion."""
    data = request.json or {}
    photo_hash = data.get("photo_hash")
    cluster_id = data.get("cluster_id")
    accept = data.get("accept", False)

    clusters = _ai.get("face_clusters")
    if not clusters or not photo_hash:
        return jsonify({"error": "Invalid request"}), 400

    if accept and cluster_id in clusters:
        c = clusters[cluster_id]
        if photo_hash not in c.get("photo_hashes", []):
            c.setdefault("photo_hashes", []).append(photo_hash)
            c["photo_count"] = len(c["photo_hashes"])
        # Un-exclude if previously excluded
        excl = set(c.get("excluded_hashes", []))
        if photo_hash in excl:
            excl.discard(photo_hash)
            c["excluded_hashes"] = list(excl) if excl else []
    elif not accept and cluster_id in clusters:
        # Mark as excluded from this cluster so it doesn't get suggested again
        c = clusters[cluster_id]
        excl = set(c.get("excluded_hashes", []))
        excl.add(photo_hash)
        c["excluded_hashes"] = list(excl)

    fc_path = os.path.join(_AI_DIR, "face_clusters.json")
    _atomic_write_json(fc_path, clusters)

    return jsonify({"status": "ok"})




# DUPES REVIEW ROUTES
_DUPES_STATE = {"components": None, "resolved": None, "index_mtime": 0}
_DUPES_LOCK = threading.Lock()
_DUPES_REVIEW_STATE_PATH = os.path.join(_AI_DIR, "dupes_review_state.json")
_DUPES_TRASH_BASE = "/mnt/data/.ares-trash"
_PRIOR_DEDUP_MANIFESTS_BASE = [
    os.path.join(_AI_DIR, "dedup-manifest-20260419-230520.jsonl"),
    os.path.join(_AI_DIR, "dedup-manifest-v2-20260419-234555.jsonl"),
]

def _dupes_load_review_state():
    try:
        if os.path.exists(_DUPES_REVIEW_STATE_PATH):
            with open(_DUPES_REVIEW_STATE_PATH) as _f:
                return set(json.load(_f).get("resolved", []))
    except Exception:
        pass
    return set()

def _dupes_save_review_state(resolved_set):
    tmp = _DUPES_REVIEW_STATE_PATH + ".tmp"
    with open(tmp, "w") as _f:
        json.dump({"resolved": list(resolved_set)}, _f)
    os.replace(tmp, _DUPES_REVIEW_STATE_PATH)

def _dupes_covered_paths():
    covered = set()
    mfs = list(_PRIOR_DEDUP_MANIFESTS_BASE)
    try:
        for fn in os.listdir(_AI_DIR):
            if fn.startswith("dedup-manifest-") and fn.endswith(".jsonl"):
                p = os.path.join(_AI_DIR, fn)
                if p not in mfs:
                    mfs.append(p)
    except Exception:
        pass
    for mf in mfs:
        if not os.path.exists(mf):
            continue
        try:
            with open(mf) as _f:
                for line in _f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    for k in ("trashed_path", "keeper_path"):
                        pp = rec.get(k, "")
                        if pp:
                            covered.add(pp)
        except Exception:
            pass
    return covered

def _build_dupes_components_internal():
    import numpy as _np, hashlib as _hl
    items = load_photo_index()
    if not items:
        return []
    ch = _ai.get("clip_hashes")
    ce = _ai.get("clip_emb")
    h2i = _ai.get("hash_to_idx", {})
    if not ch or ce is None or len(ch) == 0:
        return []
    def _hrel(rel_path):
        return _hl.md5(rel_path.encode()).hexdigest()
    h2item = {}
    for it in items:
        p = it.get("path", "")
        if not p:
            continue
        try:
            rel = os.path.relpath(p, PHOTOS_ROOT)
            h = _hrel(rel)
        except Exception:
            continue
        h2item[h] = it
    covered = _dupes_covered_paths()
    vhashes = [h for h in ch if h in h2item and h in h2i]
    if len(vhashes) < 2:
        return []
    vidxs = [h2i[h] for h in vhashes]
    em = ce[vidxs]
    norms = _np.linalg.norm(em, axis=1, keepdims=True)
    norms = _np.where(norms == 0, 1, norms)
    en = em / norms
    THR = 0.97
    n = len(vhashes)
    CHUNK = 500
    pairs = []
    for i in range(0, n, CHUNK):
        chunk = en[i:i+CHUNK]
        sims = chunk @ en.T
        rows, cols = _np.where(sims >= THR)
        for r, c in zip(rows.tolist(), cols.tolist()):
            gi = i + r
            if gi >= c:
                continue
            phi = vhashes[gi]
            phj = vhashes[c]
            iti = h2item.get(phi)
            itj = h2item.get(phj)
            if iti is None or itj is None:
                continue
            if iti["path"] in covered and itj["path"] in covered:
                continue
            pairs.append((phi, phj, float(sims[r, c])))
    parent = {}
    def _find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def _union(a, b):
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[rb] = ra
    for phi, phj, sim in pairs:
        _union(phi, phj)
    groups = {}
    for phi, phj, sim in pairs:
        r = _find(phi)
        groups.setdefault(r, set()).update([phi, phj])
    components = []
    for root_ph, member_set in groups.items():
        members = list(member_set)
        if len(members) < 2:
            continue
        mitems = [h2item[h] for h in members if h in h2item]
        if not mitems:
            continue
        if all(it["path"] in covered for it in mitems):
            continue
        mhashes = [h for h in members if h in h2i]
        max_sim = 0.0
        if len(mhashes) >= 2:
            midxs = [h2i[h] for h in mhashes]
            e2 = en[midxs]
            s2 = e2 @ e2.T
            _np.fill_diagonal(s2, 0)
            max_sim = float(s2.max())
        enriched = []
        for it in mitems:
            ph2 = None
            try:
                rel2 = os.path.relpath(it["path"], PHOTOS_ROOT)
                ph2 = _hrel(rel2)
            except Exception:
                pass
            fsz = 0
            try:
                if os.path.exists(it["path"]):
                    fsz = os.path.getsize(it["path"])
            except Exception:
                pass
            enriched.append({"path": it["path"], "thumb": it.get("thumb",""), "thumb_hq": it.get("thumb_hq","") or it.get("thumb",""), "date": it.get("date",0), "type": it.get("type","image"), "size": fsz, "_ph": ph2, "sim_to_first": 0.0})
        enriched.sort(key=lambda x: -x["size"])
        fp = enriched[0].get("_ph")
        enriched[0]["sim_to_first"] = 1.0
        for m in enriched[1:]:
            mp = m.get("_ph")
            if mp and fp and mp in h2i and fp in h2i:
                m["sim_to_first"] = round(float(en[h2i[mp]] @ en[h2i[fp]]), 4)
        gid = "clip97-" + root_ph
        components.append({"group_id": gid, "members": enriched, "max_sim": max_sim, "size": len(enriched)})
    components.sort(key=lambda c: (-c["size"], -c["max_sim"]))
    return components

def _get_dupes_components(force_rebuild=False):
    with _DUPES_LOCK:
        mtime = photo_db.version()
        if force_rebuild or _DUPES_STATE["components"] is None or _DUPES_STATE["index_mtime"] != mtime:
            _DUPES_STATE["components"] = _build_dupes_components_internal()
            _DUPES_STATE["index_mtime"] = mtime
            if _DUPES_STATE["resolved"] is None:
                _DUPES_STATE["resolved"] = _dupes_load_review_state()
        return _DUPES_STATE["components"], _DUPES_STATE["resolved"]


@app.route("/dupes")
@require_auth
def dupes_review_page():
    return render_template("dupes_review.html")


@app.route("/photos/recycle")
@require_auth
def photos_recycle_page():
    return render_template("recycle_bin.html")


@app.route("/api/photos/dupes/count")
@require_auth
def api_dupes_count():
    components, resolved = _get_dupes_components()
    pending = [c for c in components if c["group_id"] not in resolved]
    return jsonify({"pending": len(pending)})


@app.route("/api/photos/dupes/pending")
@require_auth
def api_dupes_pending():
    cursor = int(request.args.get("cursor", 0))
    components, resolved = _get_dupes_components()
    pending = [c for c in components if c["group_id"] not in resolved]
    total = len(pending)
    if cursor >= total:
        return jsonify({"done": True, "total_remaining": 0})
    c = pending[cursor]
    members_out = []
    for m in c["members"]:
        members_out.append({
            "path": m["path"],
            "thumb": m.get("thumb", ""),
            "thumb_hq": m.get("thumb_hq", "") or m.get("thumb", ""),
            "date": m.get("date", 0),
            "size": m.get("size", 0),
            "type": m.get("type", "image"),
            "sim_to_first": round(m.get("sim_to_first", 0.0), 4),
            "is_video": m.get("type") == "video",
        })
    return jsonify({
        "group_id": c["group_id"],
        "members": members_out,
        "total_remaining": total - cursor,
        "cursor": cursor,
        "done": False,
    })


@app.route("/api/photos/dupes/resolve", methods=["POST"])
@require_auth
def api_dupes_resolve():
    import shutil as _shu
    data = request.json or {}
    group_id = data.get("group_id", "")
    keep_paths = data.get("keep", [])
    trash_paths = data.get("trash", [])
    if not group_id:
        return jsonify({"error": "no group_id"}), 400
    allowed = ["/mnt/data/PHOTOS/", "/srv/mergerfs/PROMETHEUS/PHOTOS/"]
    run_ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    trash_dir = os.path.join(_DUPES_TRASH_BASE, run_ts + "-dedup-review")
    manifest_date = datetime.utcnow().strftime("%Y%m%d")
    manifest_path = os.path.join(_AI_DIR, "dedup-manifest-review-" + manifest_date + ".jsonl")
    trashed = []
    errors = []
    for p in trash_paths:
        abs_p = os.path.abspath(p)
        if not any(abs_p.startswith(pfx) for pfx in allowed):
            errors.append(p + ": not allowed")
            continue
        if not os.path.exists(abs_p):
            trashed.append(p)
            continue
        rel = os.path.relpath(abs_p, "/")
        dest = os.path.join(trash_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            _shu.move(abs_p, dest)
            trashed.append(p)
        except Exception as e:
            errors.append(p + ": " + str(e))
    if trashed:
        trash_set = set(trashed)
        items = load_photo_index()
        items_clean = [it for it in items if it["path"] not in trash_set]
        _save_photo_index(items_clean)
        try:
            keeper = keep_paths[0] if keep_paths else ""
            with open(manifest_path, "a") as mf:
                for pp in trashed:
                    mf.write(json.dumps({"trashed_path": pp, "keeper_path": keeper, "reason": "review", "group_id": group_id}) + chr(10))
        except Exception:
            pass
    with _DUPES_LOCK:
        if _DUPES_STATE["resolved"] is None:
            _DUPES_STATE["resolved"] = _dupes_load_review_state()
        _DUPES_STATE["resolved"].add(group_id)
        _dupes_save_review_state(_DUPES_STATE["resolved"])
        _DUPES_STATE["components"] = None
    return jsonify({"status": "ok", "trashed": len(trashed), "errors": errors})


def _mc_auto_shutdown():
    """Background loop: shut down Minecraft server if no players for 10 minutes."""
    MC_JAR = "forge-1.7.10-10.13.4.1614-1.7.10-universal.jar"
    import socket, struct as _st
    empty_since = None  # timestamp when we first saw 0 players
    IDLE_LIMIT = 600  # 10 minutes

    while True:
        time.sleep(60)  # check every minute
        # Is server even running?
        try:
            check = subprocess.run(["pgrep", "-f", MC_JAR], capture_output=True)
            if check.returncode != 0:
                empty_since = None
                continue
        except Exception:
            continue
        # Query player count
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(("127.0.0.1", 25565))
            host = b"127.0.0.1"
            handshake = b"\x00\x05" + bytes([len(host)]) + host + _st.pack(">H", 25565) + b"\x01"
            sock.sendall(bytes([len(handshake)]) + handshake)
            sock.sendall(b"\x01\x00")
            raw = sock.recv(4096)
            sock.close()
            # Parse varint-prefixed JSON
            idx = 0
            while idx < len(raw) and raw[idx] & 0x80:
                idx += 1
            idx += 1
            while idx < len(raw) and raw[idx] & 0x80:
                idx += 1
            idx += 1
            str_len = 0
            shift = 0
            while idx < len(raw):
                b = raw[idx]
                str_len |= (b & 0x7F) << shift
                idx += 1
                shift += 7
                if not (b & 0x80):
                    break
            status = json.loads(raw[idx:idx+str_len].decode("utf-8", errors="replace"))
            online = status.get("players", {}).get("online", 0)
        except Exception:
            # Can't reach server — might still be starting up, don't kill it
            empty_since = None
            continue

        if online == 0:
            if empty_since is None:
                empty_since = time.time()
                print(f"[mc-auto] Server idle, starting 10min countdown")
            elif time.time() - empty_since >= IDLE_LIMIT:
                print(f"[mc-auto] No players for {IDLE_LIMIT}s — shutting down")
                from ai.safe_executor import minecraft_server
                minecraft_server("off")
                empty_since = None
        else:
            if empty_since is not None:
                print(f"[mc-auto] Players online ({online}), cancelling shutdown")
            empty_since = None


def _run_startup_tasks():
    """Run once at startup (as root via systemd): refresh sudoers + tailscale serve."""
    import subprocess as _sp
    base = os.path.dirname(os.path.abspath(__file__))

    # Reinstall sudoers rules from the NAS mount (picks up any edits)
    sudoers_src = os.path.join(base, "ares-sudoers")
    sudoers_dst = "/etc/sudoers.d/ares"
    try:
        import shutil
        shutil.copy2(sudoers_src, sudoers_dst)
        os.chmod(sudoers_dst, 0o440)
        print("[startup] sudoers updated")
    except Exception as e:
        print(f"[startup] sudoers update skipped: {e}")

    # Allow zain to run tailscale without sudo in future
    try:
        _sp.run(["tailscale", "set", "--operator=zain"], timeout=10, capture_output=True)
        print("[startup] tailscale operator=zain set")
    except Exception as e:
        print(f"[startup] tailscale operator: {e}")

    # Set up tailscale serve so the app is reachable at https://prometheus
    try:
        _sp.run(["tailscale", "serve", "--bg", "http://localhost:8080"],
                timeout=15, capture_output=True)
        print("[startup] tailscale serve configured (https://prometheus)")
    except Exception as e:
        print(f"[startup] tailscale serve: {e}")

    # Enable tailscale funnel so /api/minecraft is reachable from the public internet
    try:
        _sp.run(["tailscale", "funnel", "--bg", "http://localhost:8080"],
                timeout=15, capture_output=True)
        print("[startup] tailscale funnel enabled (public access)")
    except Exception as e:
        print(f"[startup] tailscale funnel: {e}")

    # Provision/renew TLS certificate
    try:
        _sp.run(["tailscale", "cert", "prometheus.tail3045df.ts.net"],
                timeout=30, capture_output=True)
        print("[startup] TLS cert refreshed")
    except Exception as e:
        print(f"[startup] TLS cert: {e}")


@app.route("/windows")
@require_auth
def windows_view():
    return render_template("windows.html", vnc_password=os.getenv("WINDOWS_VNC_PASSWORD", ""))


def _start_background_threads():
    """Startup/watchdog threads. Called once per serving process — under
    gunicorn via ARES_BG=1 (single worker), under dev app.run via __main__."""
    threading.Thread(target=_run_startup_tasks, daemon=True).start()
    threading.Thread(target=_mc_auto_shutdown, daemon=True).start()
    threading.Thread(target=_video_prewarm_loop, daemon=True).start()
    # One-shot: warm the system-info caches in the serving worker so the first
    # dashboard load is instant (caches are stale-while-revalidate thereafter).
    threading.Thread(target=_sysinfo_prewarm, daemon=True).start()


if os.environ.get("ARES_BG") == "1":
    _start_background_threads()


if __name__ == "__main__":
    print("\n  ╔═══════════════════════════════════════╗")
    print("  ║       ARES NAS Terminal AI       ║")
    print("  ║       https://prometheus               ║")
    print("  ╚═══════════════════════════════════════╝\n")
    if os.environ.get("ARES_BG") != "1" and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        _start_background_threads()
    print("  [mc-auto] Auto-shutdown watchdog started (10min idle → off)")
    import sys
    use_debug = "--no-debug" not in sys.argv
    extra_files = []
    for root, dirs, files in os.walk(os.path.join(_APP_DIR, "templates")):
        for f in files:
            extra_files.append(os.path.join(root, f))
    for root, dirs, files in os.walk(os.path.join(_APP_DIR, "static")):
        for f in files:
            if f.endswith((".css", ".js", ".svg")):
                extra_files.append(os.path.join(root, f))
    app.run(
        host=os.getenv("ARES_HOST", "0.0.0.0"),
        port=int(os.getenv("ARES_PORT", "8080")),
        threaded=True,
        debug=use_debug, use_reloader=use_debug,
        extra_files=extra_files,
    )
