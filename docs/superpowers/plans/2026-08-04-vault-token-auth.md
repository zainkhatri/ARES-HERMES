# Vault Token Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace broken cookie-based vault auth with signed URL vault tokens (`?vt=<token>`) so iOS thumbnails load and AVPlayer videos play without cookie wrangling.

**Architecture:** `POST /api/vault/unlock` now returns a `vault_token` (itsdangerous-signed, 1-hour TTL) alongside `{"success": true}`. Every vault endpoint accepts `?vt=<token>` in addition to the existing Flask session (web browser still works). iOS stores the token in a `VaultAuth` singleton and appends it to every vault URL; AVPlayer streams by direct URL — no custom headers needed.

**Tech Stack:** Python/Flask + itsdangerous (already installed), Swift/SwiftUI, AVKit, URLSession.

## Global Constraints

- No new Python packages — itsdangerous is already imported in app.py (line 437).
- ARES runs a single gunicorn worker, 32 threads; `_vault_keys` is already thread-safe via `_vault_keys_lock`.
- `@require_auth` (session cookie) stays on every vault endpoint — iOS still sends the ARES session cookie for that gate.
- Vault decryption key never leaves server memory. The vault token carries only `kid` (pointer to the in-memory key), not the key itself.
- iOS: `VaultAuth.shared.token` is memory-only (not persisted) — token is re-obtained on every unlock.
- No changes to vault encryption, vault.json, or any vault upload logic.
- `ARES_URL = "http://100.77.42.110:8080"` — base URL already in scope everywhere.
- Power of Ten rules: no dynamic allocation after init, no recursion, all return values checked, ≤60 lines per function.
- No Co-Authored-By trailer in commits. Subject line only.

---

## File Map

| File | Change |
|---|---|
| `app.py` | `vault_unlock` → emit `vault_token`; new `_vault_token_key()`; `_serve_vault_thumb`, `vault_stream`, `vault_items`, `vault_regen_thumbs` → accept `?vt=`; remove `_debug_log` |
| `ARES/PhotoSync.swift` | New `VaultAuth` singleton; `doUnlock` stores token; all vault URLs append `?vt=`; `VaultVideoPlayer` drops custom headers; `MyEyesOnlyView` resets on appear |
| `ARES/ImageCache.swift` | Vault fetch branch: replace Cookie header with `?vt=` appended to URL |

---

### Task 1: Server — vault token issuing and validation

**Files:**
- Modify: `app.py` (lines 36-44 _debug_log, 3778-3818 vault_unlock, 4237-4261 vault_items, 4351-4360 vault_regen_thumbs, 4401-4454 _serve_vault_thumb, 4585-4618 vault_stream)

**Interfaces:**
- Produces: `GET /api/vault/thumb/<key>?vt=<token>`, `GET /api/vault/stream/<key>?vt=<token>`, `GET /api/vault/items?vt=<token>`, `GET /api/vault/regen_thumbs?vt=<token>` — all return 401 `{"error": "vault_key_expired"}` when token is absent/invalid/expired, 403 when key not in memory.
- Produces: `POST /api/vault/unlock` returns `{"success": true, "vault_token": "<signed-token>"}`.

- [ ] **Step 1: Remove the debug logging hook**

  In `app.py`, delete lines 36-44 (the `_debug_log` after_request hook). It logs vault URLs to journald in plaintext.

  ```python
  # DELETE THIS ENTIRE BLOCK (lines 36–44):
  @app.after_request
  def _debug_log(resp):
      p = request.path
      if "/vault" in p or "/login" in p:
          app.logger.warning("[DBG] %s %s → %d cookie=%s ua=%s",
              request.method, p, resp.status_code,
              request.cookies.get("session", "NONE")[:20] if request.cookies.get("session") else "NONE",
              request.headers.get("User-Agent", "")[:30])
      return resp
  ```

  After deleting, the next `@app.after_request` at line 46 (`_gzip_json`) becomes line 36.

- [ ] **Step 2: Add `_vault_token_key()` helper near the existing `_vault_session_key` block**

  Add immediately after `_vault_clear_session_key` (currently around line 3314, will shift by ~9 after step 1). The helper validates a `?vt=` token and returns the in-memory decryption key, or `None`:

  ```python
  _vault_token_ser = None  # module-level, lazy-init below

  def _vault_token_key(vt):
      """Validate a signed vault token from ?vt= and return the decryption key, or None."""
      assert isinstance(vt, str) and len(vt) <= 512, "bad vt param"
      global _vault_token_ser
      if _vault_token_ser is None:
          _vault_token_ser = URLSafeTimedSerializer(app.secret_key, salt="vault-token")
      try:
          payload = _vault_token_ser.loads(vt, max_age=3600)
      except Exception:
          return None
      kid = payload.get("kid") if isinstance(payload, dict) else None
      if not kid:
          return None
      with _vault_keys_lock:
          return _vault_keys.get(kid)
  ```

  Place this block right after `_vault_clear_session_key` so it's grouped with the other `_vault_keys` helpers.

- [ ] **Step 3: Update `vault_unlock` to return `vault_token`**

  The existing success branch (around current line 3808) returns `jsonify({"success": True})`. Replace that line with:

  ```python
  if match:
      auth["fails"] = 0
      _vault_auth_save(auth)
      session["vault_unlocked_at"] = time.time()
      session["vault_last_active"] = time.time()
      session.modified = True
      key = _vault_derive_key(pin, auth["salt"])
      kid = _vault_set_session_key(key)
      token = URLSafeTimedSerializer(app.secret_key, salt="vault-token").dumps({"kid": kid})
      return jsonify({"success": True, "vault_token": token})
  ```

  `_vault_set_session_key` currently returns `None` — change it to return `kid`:

  ```python
  def _vault_set_session_key(key):
      kid = _cry_secrets.token_hex(16)
      with _vault_keys_lock:
          _vault_keys[kid] = key
      session["vault_kid"] = kid
      return kid                    # <-- add this return
  ```

- [ ] **Step 4: Update `_serve_vault_thumb` to accept `?vt=`**

  Replace the auth block at the top of `_serve_vault_thumb` (the `_vault_session_active` + `_vault_touch` + `_vault_session_key` calls, plus the `enc` branch key lookup):

  ```python
  def _serve_vault_thumb(tier, thumb_key):
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

      # Legacy plaintext branch (unchanged below this point)
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
  ```

- [ ] **Step 5: Update `vault_stream` to accept `?vt=`**

  Replace the auth block at the top of `vault_stream`:

  ```python
  @app.route("/api/vault/stream/<thumb_key>")
  @require_auth
  def vault_stream(thumb_key):
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
      mime = _VIDEO_MIMES.get(ext, "video/mp4")
      import io as _io
      resp = send_file(_io.BytesIO(plain), mimetype=mime, as_attachment=False,
                       conditional=True,
                       download_name=os.path.basename(entry.get("path") or thumb_key))
      resp.headers["Accept-Ranges"] = "bytes"
      resp.headers["Content-Length"] = len(plain)
      return resp
  ```

- [ ] **Step 6: Update `vault_items` to accept `?vt=`**

  Replace the auth block in `vault_items`:

  ```python
  @app.route("/api/vault/items")
  @require_auth
  def vault_items():
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
              continue
          out = dict(entry)
          out["key"]      = tk
          out["thumb"]    = f"/api/vault/thumb/{tk}"
          out["thumb_hq"] = f"/api/vault/thumb_hq/{tk}"
          out["_in_vault"] = True
          result.append(out)

      result.sort(key=lambda x: -x.get("date", 0))
      return jsonify(result)
  ```

- [ ] **Step 7: Update `vault_regen_thumbs` to accept `?vt=`**

  Replace the auth block at the top of `vault_regen_thumbs` (lines 3353-3360). The function body below the auth check stays identical:

  ```python
  @app.route("/api/vault/regen_thumbs")
  @require_auth
  def vault_regen_thumbs():
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
      # ... rest of function unchanged ...
  ```

- [ ] **Step 8: Restart and smoke-test server**

  ```bash
  pct exec 101 -- systemctl restart ares
  sleep 3
  # Check logs for errors
  pct exec 101 -- journalctl -u ares -n 30 --no-pager
  ```

  Then test the token flow manually:

  ```bash
  # Login and get cookie
  COOKIE=$(curl -s -c /tmp/jar -b /tmp/jar -X POST http://192.168.20.213:8080/api/login \
    -H "Content-Type: application/json" \
    -d '{"username":"zainkhatri","password":"prometheus"}' \
    -D - | grep Set-Cookie | awk '{print $2}' | head -1)
  echo "COOKIE: $COOKIE"

  # Unlock and get vault_token
  VT=$(curl -s -b /tmp/jar -X POST http://192.168.20.213:8080/api/vault/unlock \
    -H "Content-Type: application/json" \
    -d '{"pin":"<your-pin>"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('vault_token','MISSING'))")
  echo "VT: $VT"

  # Fetch items with token
  curl -s -b /tmp/jar "http://192.168.20.213:8080/api/vault/items?vt=$VT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{len(d)} items')"

  # Fetch a thumbnail (use a key from the items response)
  FIRST_KEY=$(curl -s -b /tmp/jar "http://192.168.20.213:8080/api/vault/items?vt=$VT" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['key'])")
  curl -s -o /tmp/test-thumb.jpg -w "%{http_code}" -b /tmp/jar "http://192.168.20.213:8080/api/vault/thumb/$FIRST_KEY?vt=$VT"
  # Expected: 200 and /tmp/test-thumb.jpg is a valid JPEG
  file /tmp/test-thumb.jpg
  ```

  Expected output: `<N> items`, `200`, `JPEG image data`.

- [ ] **Step 9: Commit**

  ```bash
  git add app.py
  git commit -m "vault: replace session-only auth with signed vault_token (?vt=) for iOS"
  ```

---

### Task 2: iOS — VaultAuth singleton and token-based URLs

**Files:**
- Modify: `ARES/PhotoSync.swift` (VaultItem, VaultTransfer.doUnlock, VaultTransfer.fetchItems, VaultTransfer.regenMissingThumbs, VaultTransfer.uploadOne, VaultTransfer.gridSections, VaultThumb, VaultFullImage, VaultVideoPlayer, MyEyesOnlyView)
- Modify: `ARES/ImageCache.swift` (vault fetch branch in `fetch()` and `downloadToDisk()`)

**Interfaces:**
- Consumes: `vault_token` string from Task 1's unlock response JSON.
- Produces: `VaultAuth.shared.token: String` — readable by `ImageCache`.

- [ ] **Step 1: Add `VaultAuth` singleton to `PhotoSync.swift`**

  Add this near the top of the vault section, right after `VaultPINKeychain` (around line 696):

  ```swift
  // Holds the signed vault token returned by /api/vault/unlock.
  // Memory-only — not persisted. Re-obtained on every unlock.
  final class VaultAuth {
      static let shared = VaultAuth()
      var token: String = ""
      private init() {}
  }
  ```

- [ ] **Step 2: Update `VaultItem` computed URL properties to embed the token**

  Replace the two computed properties on `VaultItem`:

  ```swift
  struct VaultItem: Identifiable {
      let key: String
      let isVideo: Bool
      let date: Double
      var id: String { key }
      var thumbURL: String   { ARES_URL + "/api/vault/thumb/" + key + "?vt=" + VaultAuth.shared.token }
      var thumbHQURL: String { ARES_URL + "/api/vault/thumb_hq/" + key + "?vt=" + VaultAuth.shared.token }
  }
  ```

- [ ] **Step 3: Update `doUnlock` to parse and store `vault_token`**

  Replace the existing `doUnlock` function:

  ```swift
  private func doUnlock(_ pin: String) async -> Bool {
      guard let url = URL(string: base + "/api/vault/unlock") else { return false }
      var req = URLRequest(url: url, timeoutInterval: 15)
      req.httpMethod = "POST"
      req.setValue("application/json", forHTTPHeaderField: "Content-Type")
      let ck = ARESAuth.shared.cookie
      if !ck.isEmpty { req.setValue(ck, forHTTPHeaderField: "Cookie") }
      req.httpBody = try? JSONSerialization.data(withJSONObject: ["pin": pin])
      guard let (data, resp) = try? await URLSession.shared.data(for: req),
            let http = resp as? HTTPURLResponse,
            http.statusCode == 200,
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let vt = json["vault_token"] as? String, !vt.isEmpty else { return false }
      VaultAuth.shared.token = vt
      return true
  }
  ```

- [ ] **Step 4: Update `fetchItems` to use `?vt=`**

  Replace the `fetchItems` function:

  ```swift
  func fetchItems() async {
      let vt = VaultAuth.shared.token
      guard !vt.isEmpty, let url = URL(string: base + "/api/vault/items?vt=" + vt) else { return }
      var req = URLRequest(url: url)
      let ck = ARESAuth.shared.cookie
      if !ck.isEmpty { req.setValue(ck, forHTTPHeaderField: "Cookie") }
      guard let (data, resp) = try? await URLSession.shared.data(for: req),
            (resp as? HTTPURLResponse)?.statusCode == 200,
            let arr = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return }
      items = arr.compactMap { e in
          guard let k = e["key"] as? String else { return nil }
          return VaultItem(key: k, isVideo: (e["type"] as? String) == "video",
                           date: (e["date"] as? Double) ?? 0)
      }
  }
  ```

- [ ] **Step 5: Update `regenMissingThumbs` to use `?vt=`**

  Replace the `regenMissingThumbs` function:

  ```swift
  private func regenMissingThumbs() async {
      let vt = VaultAuth.shared.token
      guard !vt.isEmpty,
            let url = URL(string: base + "/api/vault/regen_thumbs?vt=" + vt) else { return }
      var req = URLRequest(url: url, timeoutInterval: 300)
      let ck = ARESAuth.shared.cookie
      if !ck.isEmpty { req.setValue(ck, forHTTPHeaderField: "Cookie") }
      guard let (data, _) = try? await URLSession.shared.data(for: req),
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            (json["done"] as? Int ?? 0) > 0 else { return }
      await fetchItems()
  }
  ```

- [ ] **Step 6: Update `uploadOne` to use `?vt=`**

  Replace the first URL construction line in `uploadOne`:

  ```swift
  private func uploadOne(_ data: Data, filename: String, date: Date) async -> Bool {
      let vt = VaultAuth.shared.token
      guard !vt.isEmpty,
            let url = URL(string: base + "/api/vault/upload?vt=" + vt) else { return false }
      var req = URLRequest(url: url, timeoutInterval: 300)
      req.httpMethod = "POST"
      let ck = ARESAuth.shared.cookie
      if !ck.isEmpty { req.setValue(ck, forHTTPHeaderField: "Cookie") }
      let boundary = "B-\(UUID().uuidString)"
      req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
      var body = Data()
      let pre = "--\(boundary)\r\nContent-Disposition: form-data; name=\"date\"\r\n\r\n\(date.timeIntervalSince1970)\r\n"
          + "--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n"
          + "Content-Type: application/octet-stream\r\n\r\n"
      body.append(pre.data(using: .utf8)!)
      body.append(data)
      body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)
      req.httpBody = body
      guard let (_, resp) = try? await URLSession.shared.data(for: req),
            (resp as? HTTPURLResponse)?.statusCode == 200 else { return false }
      return true
  }
  ```

  Note: `/api/vault/upload` also needs `?vt=` support on the server. Add it in Task 1 after the plan review, or add it now. The `vault_upload` function at line 3397 has identical auth block — apply the same `vt_param` pattern there too.

  **Server addition for `vault_upload` (add to Task 1 step 7 or here):**

  ```python
  @app.route("/api/vault/upload", methods=["POST"])
  @require_auth
  def vault_upload():
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
              return jsonify({"error": "Vault locked"}), 403
          _vault_touch()
      # ... rest of vault_upload unchanged, replace the existing `key = _vault_session_key()` call ...
  ```

- [ ] **Step 7: Update `gridSections` to embed `?vt=` in TimelineItem URLs**

  Replace the `gridSections` computed property body (the two vault thumb URL lines):

  ```swift
  var gridSections: [TimelineSection] {
      let vt = VaultAuth.shared.token
      let mk = DateFormatter(); mk.dateFormat = "yyyy-MM"
      let ml = DateFormatter(); ml.dateFormat = "MMMM yyyy"
      let groups = Dictionary(grouping: items) { mk.string(from: Date(timeIntervalSince1970: $0.date)) }
      return groups.map { key, its -> TimelineSection in
          let sorted = its.sorted { $0.date > $1.date }
          let t = sorted.map { v in
              TimelineItem(path: v.key,
                           thumb: ARES_URL + "/api/vault/thumb/\(v.key)?vt=\(vt)",
                           thumbHQ: ARES_URL + "/api/vault/thumb_hq/\(v.key)?vt=\(vt)",
                           date: v.date, isVideo: v.isVideo, color: "0f0f14")
          }
          return TimelineSection(id: key,
                                 label: ml.string(from: Date(timeIntervalSince1970: sorted.first?.date ?? 0)),
                                 items: t)
      }.sorted { $0.id > $1.id }
  }
  ```

- [ ] **Step 8: Update `VaultThumb` to drop the Cookie header (URL has `?vt=`)**

  `item.thumbURL` now includes `?vt=`, so the Cookie header for vault auth is gone. Only the ARES session cookie remains (via `ARESAuth.shared.cookie`) for `@require_auth`. Replace the `.task` block:

  ```swift
  .task {
      guard img == nil, let url = URL(string: item.thumbURL) else { return }
      var req = URLRequest(url: url)
      let ck = ARESAuth.shared.cookie
      if !ck.isEmpty { req.setValue(ck, forHTTPHeaderField: "Cookie") }
      if let (d, resp) = try? await URLSession.shared.data(for: req),
         (resp as? HTTPURLResponse)?.statusCode == 200,
         let ui = UIImage(data: d) { img = ui }
  }
  ```

  (Cookie header is still needed for `@require_auth`. Vault auth is now via `?vt=` in the URL itself.)

- [ ] **Step 9: Update `VaultFullImage` to drop the Cookie header**

  Replace the `.task` block (same pattern as step 8):

  ```swift
  .task {
      guard img == nil, let url = URL(string: item.thumbHQURL) else { return }
      var req = URLRequest(url: url)
      let ck = ARESAuth.shared.cookie
      if !ck.isEmpty { req.setValue(ck, forHTTPHeaderField: "Cookie") }
      if let (d, resp) = try? await URLSession.shared.data(for: req),
         (resp as? HTTPURLResponse)?.statusCode == 200,
         let ui = UIImage(data: d) { img = ui }
  }
  ```

- [ ] **Step 10: Update `VaultVideoPlayer` to use plain URL (no custom headers)**

  Replace the `.onAppear` body:

  ```swift
  .onAppear {
      guard player == nil else { return }
      let vt = VaultAuth.shared.token
      guard !vt.isEmpty,
            let url = URL(string: Self.base + "/api/vault/stream/\(item.key)?vt=\(vt)") else { return }
      let asset = AVURLAsset(url: url)
      let p = AVPlayer(playerItem: AVPlayerItem(asset: asset))
      p.play()
      player = p
  }
  ```

  AVPlayer gets the token in the URL directly — no `AVURLAssetHTTPHeaderFieldsKey` needed. `@require_auth` is satisfied by the ARES session cookie in `HTTPCookieStorage.shared` (set at login via `URLSession.shared`).

- [ ] **Step 11: Reset vault on MyEyesOnlyView appear**

  In `MyEyesOnlyView`, add an `.onAppear` that resets `unlocked` so the vault always re-authenticates when opened. Find the `MyEyesOnlyView` body and add:

  ```swift
  .onAppear {
      if vt.unlocked {
          vt.unlocked = false
          VaultAuth.shared.token = ""
      }
  }
  ```

  Attach this to the outermost view in `MyEyesOnlyView.body` (the NavigationView or ZStack).

- [ ] **Step 12: Update `ImageCache` vault fetch branch**

  In `ImageCache.swift`, the vault branch in `fetch()` (lines 155-165) still sets a Cookie header. Replace it to just use the URL as-is (which now contains `?vt=`):

  ```swift
  if urlString.contains("/api/vault/") {
      guard let url = URL(string: urlString) else { return nil }
      var req = URLRequest(url: url, timeoutInterval: 30)
      let ck = ARESAuth.shared.cookie
      if !ck.isEmpty { req.setValue(ck, forHTTPHeaderField: "Cookie") }
      // ponytail: vault token is in the URL already (?vt=); no separate auth header needed
      guard let (data, resp) = try? await URLSession.shared.data(for: req),
            (resp as? HTTPURLResponse).map({ $0.statusCode < 400 }) ?? true,
            let img = downsample(data) else { return nil }
      store(img, key: urlString)
      return img
  }
  ```

  Also update `downloadToDisk` (lines 113-124) — the vault path check isn't present there (it writes to disk, which vault images must not). Keep the existing guard: vault images won't flow through `downloadToDisk` since `warmAllToDisk` is only called with non-vault URLs (from `PhotoStore`). No change needed here, but verify: `warmAllToDisk` is called with `PhotoStore` thumb URLs, not vault URLs. ✓

- [ ] **Step 13: Build and install to iPhone**

  On the Mac (open terminal in the A&N project directory):

  ```bash
  cd "/path/to/A&N"
  xcodegen generate
  ```

  Then build and install:

  ```bash
  # Build
  xcodebuild -project ARES.xcodeproj -scheme ARES \
    -destination "generic/platform=iOS" \
    -configuration Debug \
    build 2>&1 | tail -20

  # Install via devicectl (find device UUID with: xcrun devicectl list devices)
  xcrun devicectl device install app \
    --device <DEVICE_UUID> \
    "$(find ~/Library/Developer/Xcode/DerivedData -name 'ARES.app' -path '*/Debug-iphoneos/*' | head -1)"
  ```

- [ ] **Step 14: End-to-end test on device**

  1. Open the app → navigate to My Eyes Only
  2. Enter PIN (or use Face ID)
  3. Verify thumbnails load in the grid
  4. Tap a video → verify it plays
  5. Tap an image → verify full-size view loads
  6. On server, confirm vault requests in logs:

  ```bash
  pct exec 101 -- journalctl -u ares -f --no-pager | grep vault
  ```

  Expected: `GET /api/vault/thumb/<key>` returning 200, no 401/403.

- [ ] **Step 15: Commit**

  ```bash
  git add "ARES/PhotoSync.swift" "ARES/ImageCache.swift"
  git commit -m "vault: use signed vault_token in URLs; drop cookie-based vault auth on iOS"
  ```

---

## Self-Review

**Spec coverage:**
- ✅ `vault_unlock` returns `vault_token` — Task 1 Step 3
- ✅ All vault endpoints accept `?vt=` — Task 1 Steps 4-7 (thumb, stream, items, regen_thumbs, upload)
- ✅ 401 `vault_key_expired` on expired/invalid token — Steps 4-7
- ✅ Session auth preserved for web browser — fallback `else` branch in each endpoint
- ✅ `VaultAuth.shared.token` singleton — Task 2 Step 1
- ✅ `doUnlock` stores token — Task 2 Step 3
- ✅ All vault URLs get `?vt=` — Steps 2, 4, 5, 6, 7, 8, 9, 10, 12
- ✅ AVPlayer uses plain URL — Task 2 Step 10
- ✅ `MyEyesOnlyView` resets on appear — Task 2 Step 11
- ✅ Debug logging removed — Task 1 Step 1
- ✅ `_vault_set_session_key` returns `kid` — Task 1 Step 3

**Placeholder scan:** None found.

**Type consistency:** `kid: str` (Python), `vt: String` (Swift) — consistent. `vault_token` key name matches in server output and iOS JSON parse.

**Missing:** `vault_upload` needs `?vt=` on the server — added inline in Task 2 Step 6. Apply this during Task 1 implementation before the restart/smoke-test.
