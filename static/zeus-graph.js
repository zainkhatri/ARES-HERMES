window.KGGraph = function (el, opts) {
  if (!el) return null;
  opts = opts || {};
  var BOX = (opts.box == null ? null : opts.box);
  var ACCENT = opts.accent || '125,205,255';
  var LIMIT = opts.limit || 90;
  var TRAVERSABLE = !!opts.traversable;  // fullscreen only gets focus/spotlight
  var DETAIL_INLINE = opts.detailInline !== false;  // false = host renders detail elsewhere (a slot) via onNode
  var onNodeCb = null, rafId = null, alive = true, api = null, pollId = null;

  // ---- Look = the ARES iPhone app's graph (Vitals/KGSpin.swift): only linked nodes, sized by
  // degree, laid out once in 3D (seeded Fruchterman–Reingold), slowly spinning with a fixed tilt
  // and perspective, depth read as 3 opacity bands, thin edges in the source node's colour,
  // over a soft accent glow. No ambient labels; hover/selection show one. ----
  var TILT = 0.38, CAMERA = 3.2, SPIN_RAD_PER_S = 6 * Math.PI / 180;
  var MAX_EDGES = opts.maxEdges || Math.round(LIMIT * 1.5);
  var BAND_ALPHA = [0.30, 0.62, 1.0];
  var FPS_MS = 1000 / 30;          // a slow spin needs no more (the app runs at 30 fps too)
  var sceneKey = '';

  // read brand once at construction time
  var BRAND = (document.documentElement.getAttribute('data-brand') || 'ARES').toUpperCase();

  // per-brand palettes — no hardcoded blue for ARES/EROS
  var PAL_ARES = { box:'250,204,21', host:'250,204,21', project:'249,115,22',
    folder:'239,110,60', vault:'220,50,50', 'file-cluster':'253,224,120',
    dataset:'234,88,12', database:'234,88,12', service:'251,146,60',
    vm:'253,186,116', disk:'253,186,116', _default:'245,158,11' };
  var PAL_EROS = { box:'253,224,71', host:'253,224,71', project:'245,158,11',
    folder:'217,160,60', vault:'234,88,12', 'file-cluster':'254,240,138',
    dataset:'202,138,4', database:'202,138,4', service:'250,204,21',
    vm:'253,186,116', disk:'253,186,116', _default:'234,179,8' };
  var PAL_ZEUS = { box:'220,240,255', host:'220,240,255', project:'245,158,11',
    folder:'56,189,248', vault:'255,90,90', 'file-cluster':'34,211,238',
    dataset:'129,140,248', database:'129,140,248', service:'74,222,128',
    vm:'250,204,21', disk:'34,211,238', _default:'160,190,210' };
  var PAL = BRAND === 'EROS' ? PAL_EROS : BRAND === 'ZEUS' ? PAL_ZEUS : PAL_ARES;

  function col(k, depth) {
    if (k === 'chat') return '196,181,253';                 // Claude Code chats — lavender, brand-agnostic
    if (k === 'gpt-chat') return '110,231,183';             // ChatGPT archive — teal-green
    if (k === 'claude-chat') return '251,146,110';          // claude.ai web chats — coral
    if (k === 'skill') return '52,211,153';                 // Claude skills — emerald
    if (k === 'mcp') return '96,165,250';                   // MCP servers — sky-blue
    if (k === 'agent') return '244,114,182';                // sub-agents — pink
    return PAL[k] || PAL._default;   // depth is encoded by node size, not color — keeps legend == canvas
  }

  // ---- group coloring: color each node by its TOP-LEVEL group (root folder or
  // chat/tool category), so the legend reads "Claude Code / GPT / PROJECTS / WORK…"
  // instead of generic node kinds. Group is derived from the node's id (a path)
  // and kind. ----
  var GRP = {}, GRPCOL = {};
  // Fixed category colors (stable across reindexes — NOT ranked by node count, which
  // used to reshuffle every group's color whenever the biggest group changed size).
  // Claude ecosystem = orange family, GPT archive = white, ZEUS = blue, EROS = yellow.
  var CAT_COL = {
    'Claude Code': '249,115,22',   // orange-500
    'Skills':      '251,146,60',   // orange-400
    'MCP':         '234,88,12',    // orange-600
    'Agents':      '253,186,116',  // orange-300
    'Claude.ai':   '194,65,12',    // orange-700
    'GPT':         '240,240,240',  // white
    'ZEUS':        '56,189,248',   // blue
    'EROS':        '250,204,21',   // yellow
  };
  // Everything else is a real folder/file group (PHOTOS, PROJECTS, WORK, PERSONAL,
  // MORDOR, …) — red family, cycled alphabetically so a given folder keeps its shade
  // across reindexes instead of jumping around with node-count rank.
  var FOLDER_RED = ['239,68,68',   // red
                     '124,20,20',  // maroon
                     '185,28,28',  // red-700
                     '252,165,165',// red-300
                     '159,18,57',  // crimson
                     '220,38,38',  // red-600
                     '127,29,29'];// red-900
  function groupOf(n){
    if ((n.id||'').indexOf('EROS:') === 0) return 'EROS';   // EROS nodes folded into the ARES graph → one group
    var k = n.kind, nm = (n.name||'').toLowerCase();
    if (k==='chat') return 'Claude Code';
    if (k==='gpt-chat') return 'GPT';
    if (k==='claude-chat') return 'Claude.ai';
    if (k==='skill') return 'Skills';
    if (k==='mcp') return 'MCP';
    if (k==='agent') return 'Agents';
    if (k==='folder' || k==='file-cluster' || k==='project') {
      if (nm.indexOf('chatgpt')>=0) return 'GPT';
      if (nm.indexOf('claude code')>=0) return 'Claude Code';
      if (nm.indexOf('claude.ai')>=0) return 'Claude.ai';
      if (nm.indexOf('skill')>=0) return 'Skills';
      if (nm.indexOf('mcp server')>=0) return 'MCP';
      if (nm.indexOf('sub-agent')>=0) return 'Agents';
    }
    var id = n.id || '';
    var m = id.match(/PROMETHEUS\/([^\/]+)/);           // first segment under the pool root
    if (m) return m[1].replace(/^\.+/,'') || m[1];       // strip leading dot on hidden dirs
    return n.name || 'root';
  }
  function assignGroups(){
    var cnt = {};
    for (var i=0;i<N.length;i++){ var g = groupOf(N[i]); GRP[N[i].id] = g; cnt[g] = (cnt[g]||0)+1; }
    // Known categories get their fixed color; anything left is a real folder/file
    // group and gets a stable red shade (alphabetical, not size-ranked — a group's
    // color no longer shuffles just because another group grew past it).
    var folders = Object.keys(cnt).filter(function(g){ return !CAT_COL[g]; }).sort();
    GRPCOL = {};
    Object.keys(CAT_COL).forEach(function(g){ if (cnt[g]) GRPCOL[g] = CAT_COL[g]; });
    folders.forEach(function(g, i){ GRPCOL[g] = FOLDER_RED[i % FOLDER_RED.length]; });
  }
  function gcol(n){ return GRPCOL[GRP[n.id]] || PAL._default; }

  function esc(s){ return String(s==null?'':s).replace(/[&<>]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[c];}); }

  var cv = document.createElement('canvas');
  cv.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;cursor:grab';
  // ponytail: don't override el.style.position — caller's HTML sets it (absolute/relative)
  if (!el.style.position) el.style.position = 'relative';
  el.appendChild(cv);
  cv.style.willChange = 'transform';   // own compositor layer: redraws never re-raster the page
  var ctx = cv.getContext('2d'), dpr = Math.min(2, window.devicePixelRatio || 1);
  var N = [], L = [], byId = {}, nbr = new Map();
  var spin = 0, hover = null, mx = -1, my = -1, selId = null;
  var zoom = 1, userYaw = 0, pitch = TILT, panX = 0, panY = 0;
  var drag = false, pan = false, lx = 0, ly = 0;
  var pdx = 0, pdy = 0;  // pointer delta for click-vs-drag
  var alpha = 1, fitted = false;
  var lastInteract = -Infinity;
  // camera ease target (for focus)
  var camTarget = null;
  var focusId = null;
  var litSet = null;

  // --- legend DOM (collapsed pill by default; click to expand) ---
  var legEl = document.createElement('div');
  legEl.style.cssText = 'position:absolute;bottom:8px;left:8px;right:8px;z-index:10;font:10px ui-monospace,monospace;pointer-events:none';
  el.appendChild(legEl);
  var legOpen = false;

  // --- hint line (fullscreen only) ---
  var hintEl = null;
  if (TRAVERSABLE) {
    hintEl = document.createElement('div');
    hintEl.style.cssText = 'position:absolute;bottom:40px;left:50%;transform:translateX(-50%);z-index:10;color:rgba(255,255,255,0.38);font:10px ui-monospace,monospace;pointer-events:none;white-space:nowrap';
    hintEl.textContent = 'drag rotate · scroll zoom · click a node · Esc exit';
    el.appendChild(hintEl);
  }

  // --- detail panel (fullscreen only) ---
  // detail/summary panel — on BOTH card and fullscreen (card: summary only, no camera/spotlight)
  var panel = document.createElement('div');
  panel.style.cssText = [
    'position:absolute;top:44px;right:0;width:260px;max-width:66%;max-height:calc(100% - 52px)',
    'overflow-y:auto;z-index:20;background:rgba(0,0,0,0.86)',
    'border-left:2px solid rgb('+ACCENT+');padding:12px 14px 14px',
    'font:12px ui-monospace,monospace;color:#d0d0d0;display:none'
  ].join(';');
  el.appendChild(panel);

  // 5 buckets only — Claude Code/Skills/MCP/Agents/Claude.ai all read as one "Claude"
  // entry, every real folder/file group reads as one "Files" entry. Individual nodes
  // still get their own shade within the bucket (see CAT_COL/FOLDER_RED above) so the
  // graph itself has visual variety, but the legend stays exactly the 5 lines asked for.
  var BUCKET_ORDER = ['Claude', 'Files', 'GPT', 'ZEUS', 'EROS'];
  var BUCKET_COL = { Claude:'249,115,22', Files:'239,68,68', GPT:'240,240,240',
                      ZEUS:'56,189,248', EROS:'250,204,21' };
  function bucketOf(g) {
    if (g === 'GPT' || g === 'ZEUS' || g === 'EROS') return g;
    if (CAT_COL[g]) return 'Claude';
    return 'Files';
  }
  function updateLegend() {
    var present = {};
    N.forEach(function(n){ var g = GRP[n.id]; if (g) present[bucketOf(g)] = true; });
    var chips = BUCKET_ORDER.filter(function(b){ return present[b]; }).map(function(b){
      return '<span style="display:inline-flex;align-items:center;gap:4px;color:rgba(255,255,255,0.82);white-space:nowrap">' +
        '<i style="width:8px;height:8px;flex:none;background:rgb('+BUCKET_COL[b]+')"></i>' + esc(b) + '</span>';
    }).join('');
    // legend is ALWAYS visible (no collapse) — the graph is only readable with the key present
    legEl.innerHTML =
      '<div style="display:flex;flex-wrap:wrap;gap:3px 10px;max-width:'+(TRAVERSABLE?'62vw':'100%')+';' +
      'background:rgba(0,0,0,0.62);border:1px solid rgba(255,255,255,0.1);padding:5px 8px">' + chips + '</div>';
  }

  function showPanel(n, childCount) {
    if (!panel) return;
    var und = n.u && n.u.trim() ? n.u : (n.kind + ' · ' + childCount + ' children · ' + (n.path||n.id));
    panel.style.display = 'block';
    panel.innerHTML = [
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">',
        '<span style="color:rgb('+ACCENT+');font-size:11px;font-weight:700">'+esc(n.kind.toUpperCase())+'</span>',
        '<span id="kg-panel-x" style="cursor:pointer;color:#888;font-size:16px;line-height:1">✕</span>',
      '</div>',
      '<div style="font-size:13px;font-weight:600;color:#fff;margin-bottom:6px;word-break:break-word">'+esc(n.name)+'</div>',
      '<div style="color:#888;font-size:10px;margin-bottom:10px">depth '+esc(n.depth)+'</div>',
      '<div style="color:#aaa;font-size:10px;word-break:break-all;margin-bottom:10px">'+esc(n.path||n.id)+'</div>',
      '<div style="color:#ccc;font-size:11px;line-height:1.5;border-top:1px solid rgba(255,255,255,0.1);padding-top:10px">'+esc(und)+'</div>'
    ].join('');
    var x = panel.querySelector('#kg-panel-x');
    if (x) x.onclick = function(){ dismiss(); };
  }

  function hidePanel() { if (panel) panel.style.display = 'none'; }

  function dismiss() {
    hidePanel();
    selId = null;
    if (TRAVERSABLE) {                              // fullscreen: also drop spotlight + ease camera back
      focusId = null;
      litSet = null;
      camTarget = { zoom: null, panX: 0, panY: 0 };
    }
  }
  var clearFocus = dismiss;                          // alias (fullscreen exit path)

  function size(){
    var r = el.getBoundingClientRect();
    var w = Math.max(1, Math.round(r.width * dpr)), h = Math.max(1, Math.round(r.height * dpr));
    if (cv.width !== w || cv.height !== h){ cv.width = w; cv.height = h; }
    return [cv.width, cv.height];
  }

  function countChildren(id) {
    var s = nbr.get(id);
    if (!s) return 0;
    var n = byId[id], cnt = 0;
    s.forEach(function(nid){ var nb = byId[nid]; if (nb && (nb.depth||0) > (n&&n.depth||0)) cnt++; });
    return cnt;
  }

  function ingest(d) {
    var prev = {}; N.forEach(function(n){ prev[n.id] = n; });
    N = d.nodes.map(function(nd){ var p = prev[nd.id] || {};
      return { id:nd.id, kind:nd.kind, name:nd.name, u:nd.understanding,
        depth:nd.depth, path:nd.path,
        x:(p.x!=null?p.x:(Math.random()-.5)*6), y:(p.y!=null?p.y:(Math.random()-.5)*6),
        z:(p.z!=null?p.z:(Math.random()-.5)*6), vx:0, vy:0, vz:0 }; });
    byId = {}; N.forEach(function(n){ byId[n.id] = n; });
    L = linkedEdges(d.edges);
    // Only nodes with a link in the sample (a bare dot reads as noise), as in the app.
    var linked = {}; L.forEach(function(e){ linked[e.a.id] = 1; linked[e.b.id] = 1; });
    if (L.length) { N = N.filter(function(n){ return linked[n.id]; }); byId = {}; N.forEach(function(n){ byId[n.id] = n; }); }
    // build neighbor index
    nbr = new Map();
    N.forEach(function(n){ nbr.set(n.id, new Set()); });
    L.forEach(function(e){
      var s = nbr.get(e.a.id), t = nbr.get(e.b.id);
      if (s) s.add(e.b.id);
      if (t) t.add(e.a.id);
    });
    // also update legacy count/legend elements if they exist on the page
    var cc = document.getElementById('zg-count'); if (cc) cc.textContent = d.shown+'/'+d.total_nodes+' nodes · '+d.total_edges+' edges';
    assignGroups(); updateLegend();
    relayout(); fitView(); fitted = true;
  }

  // Deduped edges between known nodes, capped at MAX_EDGES (first come, like the app).
  function linkedEdges(edges) {
    var out = [], seen = {};
    for (var i = 0; i < edges.length && out.length < MAX_EDGES; i++) {
      var e = edges[i], a = byId[e.src], b = byId[e.dst];
      if (!a || !b || a === b) continue;
      var key = a.id < b.id ? a.id + '\u0001' + b.id : b.id + '\u0001' + a.id;
      if (seen[key]) continue;
      seen[key] = 1; out.push({ a:a, b:b });
    }
    return out;
  }

  // Degree + 3D layout. Skipped when the graph did not change (the 60 s poll re-sends it).
  function relayout() {
    var deg = {}; L.forEach(function(e){ deg[e.a.id] = (deg[e.a.id]||0) + 1; deg[e.b.id] = (deg[e.b.id]||0) + 1; });
    N.forEach(function(n){ n.deg = deg[n.id] || 0; n.r = 1.3 + Math.min(3.2, Math.sqrt(n.deg) * 0.45); });
    var key = N.length + ':' + L.length + ':' + (N.length ? N[0].id + N[N.length-1].id : '');
    if (key === sceneKey && N.every(function(n){ return n.px != null; })) return;
    sceneKey = key;
    var idx = {}; N.forEach(function(n, i){ idx[n.id] = i; });
    var pairs = L.map(function(e){ return [idx[e.a.id], idx[e.b.id]]; });
    var pos = layout3d(N.length, pairs, N.length > 500 ? 110 : 160, 11);
    N.forEach(function(n, i){ n.px = pos[i][0]; n.py = pos[i][1]; n.pz = pos[i][2]; });
  }

  // per-box localStorage cache so a box's graph paints INSTANTLY from the last
  // visit instead of waiting on the query (matters for EROS/ZEUS, queried across boxes).
  var CKEY = 'kgcache_' + BRAND + '_' + LIMIT;   // key by VIEW (ZEUS fetches box=null union — must not collide with ARES's box=ARES)
  function loadCache() {
    try { var c = localStorage.getItem(CKEY); if (!c) return false;
      var d = JSON.parse(c); if (d && d.nodes && d.nodes.length) { ingest(d); return true; } } catch (e) {}
    return false;
  }
  function saveCache(d) { try { localStorage.setItem(CKEY, JSON.stringify(d)); } catch (e) {} }

  function reload() {
    var url = '/api/kg?limit=' + LIMIT + (BOX ? '&box=' + encodeURIComponent(BOX) : '');
    fetch(url).then(function(r){ return r.json(); }).then(function(d){
      if (!d.ok || !d.nodes || !d.nodes.length) throw 0; saveCache(d); ingest(d);
    }).catch(function(){
      fetch('/static/_kg_sample.json').then(function(r){ return r.json(); }).then(ingest).catch(function(){
        var cc = document.getElementById('zg-count'); if (cc) cc.textContent = 'graph offline';
      });
    });
  }

  function mergeChildren(d) {
    var cur = {}; N.forEach(function(n){ cur[n.id] = n; });
    var anchor = cur[d.edges.length ? d.edges[0].src : null];
    d.nodes.forEach(function(nd){ if (cur[nd.id]) return;
      N.push({ id:nd.id, kind:nd.kind, name:nd.name, u:nd.understanding, depth:nd.depth, path:nd.path,
        x:(anchor?anchor.x:0)+(Math.random()-.5)*2, y:(anchor?anchor.y:0)+(Math.random()-.5)*2,
        z:(anchor?anchor.z:0)+(Math.random()-.5)*2, vx:0,vy:0,vz:0 }); });
    byId = {}; N.forEach(function(n){ byId[n.id] = n; });
    // rebuild nbr fully
    nbr = new Map(); N.forEach(function(n){ nbr.set(n.id, new Set()); });
    d.edges.forEach(function(e){ if (byId[e.src]&&byId[e.dst]) L.push({ a:byId[e.src], b:byId[e.dst] }); });
    L.forEach(function(e){ var s=nbr.get(e.a.id),t=nbr.get(e.b.id); if(s)s.add(e.b.id); if(t)t.add(e.a.id); });
    sceneKey = '';                     // new nodes: lay the whole (deterministic) scene out again
    assignGroups(); updateLegend(); relayout();
  }

  // Seeded Fruchterman–Reingold in 3D (port of KGSpin.layout): gravity to the centre,
  // pairwise repulsion, edge attraction, capped moves with cooling. Deterministic per seed.
  function layout3d(n, pairs, iters, seed) {
    var st = (seed >>> 0) || 0x9E3779B9;
    function rand() { st ^= st << 13; st >>>= 0; st ^= st >>> 17; st ^= st << 5; st >>>= 0; return (st % 20000) / 10000 - 1; }
    var x = new Float64Array(n), y = new Float64Array(n), z = new Float64Array(n);
    var fx = new Float64Array(n), fy = new Float64Array(n), fz = new Float64Array(n);
    for (var i = 0; i < n; i++) { x[i] = rand(); y[i] = rand(); z[i] = rand(); }
    var k = 2 / Math.sqrt(Math.max(1, n)), k2 = k * k, temp = 0.25, cool = temp / (iters + 1);
    for (var it = 0; it < iters; it++) {
      for (i = 0; i < n; i++) { fx[i] = -x[i] * 0.6; fy[i] = -y[i] * 0.6; fz[i] = -z[i] * 0.6; }
      for (i = 0; i < n; i++) {
        var xi = x[i], yi = y[i], zi = z[i], ax = 0, ay = 0, az = 0;
        for (var j = i + 1; j < n; j++) {
          var dx = xi - x[j], dy = yi - y[j], dz = zi - z[j];
          var s2 = k2 / Math.max(1e-6, dx*dx + dy*dy + dz*dz);
          ax += dx*s2; ay += dy*s2; az += dz*s2; fx[j] -= dx*s2; fy[j] -= dy*s2; fz[j] -= dz*s2;
        }
        fx[i] += ax; fy[i] += ay; fz[i] += az;
      }
      for (var q = 0; q < pairs.length; q++) {
        var a = pairs[q][0], b = pairs[q][1];
        var ex = x[a]-x[b], ey = y[a]-y[b], ez = z[a]-z[b];
        var len = Math.max(1e-3, Math.sqrt(ex*ex + ey*ey + ez*ez)), m = len / k;
        fx[a] -= ex*m; fy[a] -= ey*m; fz[a] -= ez*m; fx[b] += ex*m; fy[b] += ey*m; fz[b] += ez*m;
      }
      for (i = 0; i < n; i++) {
        var fl = Math.max(1e-3, Math.sqrt(fx[i]*fx[i] + fy[i]*fy[i] + fz[i]*fz[i])), sc = Math.min(fl, temp) / fl;
        x[i] += fx[i]*sc; y[i] += fy[i]*sc; z[i] += fz[i]*sc;
      }
      temp = Math.max(0.002, temp - cool);
    }
    return normalize3d(x, y, z);
  }

  // Centre on the mean; the 85th-percentile radius sits at 0.92 and outliers are clamped onto the
  // unit sphere as a halo, so the dense core fills the panel (port of KGSpin.normalized).
  function normalize3d(x, y, z) {
    var n = x.length, mx0 = 0, my0 = 0, mz0 = 0, i;
    if (!n) return [];
    for (i = 0; i < n; i++) { mx0 += x[i]; my0 += y[i]; mz0 += z[i]; }
    mx0 /= n; my0 /= n; mz0 /= n;
    var radii = [];
    for (i = 0; i < n; i++) radii.push(Math.hypot(x[i]-mx0, y[i]-my0, z[i]-mz0));
    radii.sort(function(a, b){ return a - b; });
    var r85 = radii[Math.min(n - 1, Math.floor(n * 0.85))], ref = r85 > 1e-9 ? r85 / 0.92 : radii[n - 1];
    var s = ref > 1e-9 && isFinite(ref) ? 1 / ref : 0, out = [];
    for (i = 0; i < n; i++) {
      var px = (x[i]-mx0)*s, py = (y[i]-my0)*s, pz = (z[i]-mz0)*s, l = Math.hypot(px, py, pz);
      if (l > 1) { px /= l; py /= l; pz /= l; }
      out.push(isFinite(px + py + pz) ? [px, py, pz] : [0, 0, 0]);
    }
    return out;
  }

  // Rotate around Y (spin + drag), tilt around X, perspective (port of KGSpin.project).
  // Returns [x, y, perspective scale, depth in -1..1 (1 = nearest)].
  var FIT_K = CAMERA / Math.sqrt(CAMERA * CAMERA - 1);
  function proj(n, W, H){
    var yaw = spin + userYaw, ca = Math.cos(yaw), sa = Math.sin(yaw);
    var x = n.px || 0, y = n.py || 0, z = n.pz || 0;
    var x1 = x*ca + z*sa, z1 = -x*sa + z*ca;
    var ct = Math.cos(pitch), st = Math.sin(pitch);
    var y2 = y*ct - z1*st, z2 = y*st + z1*ct;
    var sc = CAMERA / (CAMERA - z2), fit = Math.min(W, H) * 0.48 / FIT_K * zoom;
    return [W/2 + panX + x1*sc*fit, H/2 + panY + y2*sc*fit, sc, Math.max(-1, Math.min(1, z2))];
  }

  // The unit sphere always fits (the projection is sized for it); just centre above the legend.
  function fitView() {
    var reserve = (legEl && legEl.offsetHeight ? legEl.offsetHeight : 22) + 16;
    zoom = 1; panX = 0; panY = TRAVERSABLE ? 0 : -reserve / 2;
    camTarget = null;
  }

  // One frame, drawn like the app's KGSpinCanvas: glow, one stroked path per colour for edges,
  // one filled path per (depth band, colour) for nodes; then the hovered/selected node on top.
  var lastFrame = 0, lastSpinAt = 0, onScreen = true;
  function frame(now) {
    if (!alive) return;
    rafId = requestAnimationFrame(frame);
    now = now || performance.now();
    if (!onScreen || document.hidden || now - lastFrame < FPS_MS) return;
    var dt = lastSpinAt ? Math.min(0.1, (now - lastSpinAt) / 1000) : 0;
    lastFrame = now; lastSpinAt = now;
    if (now - lastInteract > 5000) spin += dt * SPIN_RAD_PER_S;   // pauses 5 s after interaction

    if (camTarget) {
      var tz = camTarget.zoom != null ? camTarget.zoom : zoom;
      zoom += (tz - zoom) * 0.18; panX += (camTarget.panX - panX) * 0.18; panY += (camTarget.panY - panY) * 0.18;
      if (Math.abs(tz-zoom) < 0.001 && Math.abs(camTarget.panX-panX) < 0.5 && Math.abs(camTarget.panY-panY) < 0.5) {
        zoom = tz; panX = camTarget.panX; panY = camTarget.panY; camTarget = null;
      }
    }

    var d = size(), W = d[0]/dpr, H = d[1]/dpr;
    ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,W,H);
    var glow = ctx.createRadialGradient(W/2, H/2 + panY, 0, W/2, H/2 + panY, Math.min(W, H) * 0.57);
    glow.addColorStop(0, 'rgba(' + ACCENT + ',0.10)'); glow.addColorStop(1, 'rgba(' + ACCENT + ',0)');
    ctx.fillStyle = glow; ctx.fillRect(0, 0, W, H);
    if (!N.length) return;

    var hasFocus = TRAVERSABLE && focusId != null && litSet != null;
    var P = {}; for (var i = 0; i < N.length; i++) P[N[i].id] = proj(N[i], W, H);

    // edges: source node's colour, 0.42 alpha, 0.9 px (dimmed outside the focus spotlight)
    var ep = {}, dimEdges = new Path2D();
    for (var k = 0; k < L.length; k++) {
      var a = L[k].a, b = L[k].b, pa = P[a.id], pb = P[b.id];
      var path;
      if (hasFocus && !(litSet.has(a.id) && litSet.has(b.id))) path = dimEdges;
      else { var ck = gcol(a); path = ep[ck] || (ep[ck] = new Path2D()); }
      path.moveTo(pa[0], pa[1]); path.lineTo(pb[0], pb[1]);
    }
    ctx.lineWidth = 0.9;
    ctx.globalAlpha = 0.05; ctx.strokeStyle = 'rgb(' + ACCENT + ')'; ctx.stroke(dimEdges);
    ctx.globalAlpha = 0.42;
    for (var ck2 in ep) { ctx.strokeStyle = 'rgb(' + ck2 + ')'; ctx.stroke(ep[ck2]); }

    // nodes: radius by degree × perspective, batched per depth band and colour
    hover = null; var best = 280;
    var np = [{}, {}, {}], dimNodes = new Path2D(), grow = Math.sqrt(zoom);
    for (i = 0; i < N.length; i++) {
      var n = N[i], p = P[n.id], r = n.r * p[2] * grow;
      if (mx >= 0) { var dxp = p[0]-mx, dyp = p[1]-my, dm = dxp*dxp + dyp*dyp; if (dm < best) { best = dm; hover = n; } }
      var target;
      if (hasFocus && !litSet.has(n.id)) target = dimNodes;
      else { var band = p[3] < -0.33 ? 0 : (p[3] < 0.33 ? 1 : 2), c = gcol(n); target = np[band][c] || (np[band][c] = new Path2D()); }
      target.moveTo(p[0] + r, p[1]); target.arc(p[0], p[1], r, 0, 6.2832);
    }
    ctx.globalAlpha = 0.10; ctx.fillStyle = 'rgb(' + ACCENT + ')'; ctx.fill(dimNodes);
    for (var bnd = 0; bnd < 3; bnd++) {
      ctx.globalAlpha = BAND_ALPHA[bnd];
      for (var c2 in np[bnd]) { ctx.fillStyle = 'rgb(' + c2 + ')'; ctx.fill(np[bnd][c2]); }
    }

    // labels: fullscreen spotlight neighbours (small), then hovered / selected (bold, glowing)
    ctx.textAlign = 'center';
    if (hasFocus) {
      ctx.globalAlpha = 0.72; ctx.font = "9px 'JetBrains Mono',ui-monospace,monospace";
      litSet.forEach(function(id){ var ln = byId[id], lp = P[id]; if (ln && lp && ln !== hover && id !== selId) { ctx.fillStyle = 'rgb(' + gcol(ln) + ')'; ctx.fillText(ln.name, lp[0], lp[1] - 8); } });
    }
    [hover, selId ? byId[selId] : null].forEach(function(hn, ix){
      if (!hn || (ix === 1 && hn === hover)) return;
      var hp = P[hn.id]; if (!hp) return;
      var hc = gcol(hn), hr = Math.max(2.4, hn.r * hp[2] * grow * 1.5);
      ctx.globalAlpha = 1; ctx.shadowColor = 'rgb(' + hc + ')'; ctx.shadowBlur = 14;
      ctx.fillStyle = 'rgb(' + hc + ')'; ctx.beginPath(); ctx.arc(hp[0], hp[1], hr, 0, 6.2832); ctx.fill();
      ctx.shadowBlur = 0;
      if (hn.id === selId) { ctx.strokeStyle = 'rgba(' + hc + ',.85)'; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(hp[0], hp[1], hr + 4, 0, 6.2832); ctx.stroke(); }
      ctx.font = "bold 12px 'JetBrains Mono',ui-monospace,monospace"; ctx.fillText(hn.name, hp[0], hp[1] - hr - 6);
    });
    ctx.textAlign = 'start'; ctx.globalAlpha = 1;

    var tip = document.getElementById('zg-tip');
    if (tip){ if (hover && !drag){ var pp = P[hover.id]; tip.style.display='block'; tip.style.left=Math.min(window.innerWidth-290,pp[0]+12)+'px'; tip.style.top=(pp[1]+12)+'px';
      tip.innerHTML='<div class="kk">'+esc(hover.kind)+'</div><b>'+esc(hover.name)+'</b><br>'+esc(hover.u||'—'); }
    else tip.style.display='none'; }
  }

  function nodeAt(cx, cy){
    var d = size(), W = d[0]/dpr, H = d[1]/dpr, best = 300, found = null;
    N.forEach(function(n){ var p=proj(n,W,H),dx=p[0]-cx,dy=p[1]-cy,dm=dx*dx+dy*dy; if(dm<best){best=dm;found=n;} });
    return found;
  }

  function setFocus(id) {
    var n = byId[id]; if (!n) return;
    focusId = id; selId = id;
    litSet = new Set([id]);
    var ns = nbr.get(id); if (ns) ns.forEach(function(nid){ litSet.add(nid); });
    // compute screen position to ease camera toward focused node
    var d = size(), W = d[0]/dpr, H = d[1]/dpr;
    // Offset of the node from the view centre scales with zoom, so solve the pan that puts it
    // dead centre at the target zoom.
    var p = proj(n, W, H), tz = Math.max(zoom, 1.2), g = tz / zoom;
    var tpanX = -(p[0] - W/2 - panX) * g;
    var tpanY = -(p[1] - H/2 - panY) * g;
    camTarget = { zoom: tz, panX: tpanX, panY: tpanY };
    var cc = countChildren(id);
    showPanel(n, cc);
    if (onNodeCb) onNodeCb(id);
  }

  function markInteract() { lastInteract = performance.now(); }

  // pointer event handling — unified for click-vs-drag detection
  var pdStartX = 0, pdStartY = 0;
  cv.addEventListener('pointerdown', function(e){
    markInteract();
    pdStartX = e.clientX; pdStartY = e.clientY;
    if (e.shiftKey || e.button === 2){ pan = true; } else { drag = true; }
    lx = e.clientX; ly = e.clientY;
    cv.style.cursor = 'grabbing';
  });
  cv.addEventListener('pointermove', function(e){
    var r = cv.getBoundingClientRect();
    if (drag){ userYaw += (e.clientX-lx)*0.008; pitch = Math.max(-0.25,Math.min(1.45,pitch+(e.clientY-ly)*0.006)); lx=e.clientX; ly=e.clientY; mx=-1; return; }
    if (pan){ panX += (e.clientX-lx); panY += (e.clientY-ly); lx=e.clientX; ly=e.clientY; mx=-1; return; }
    mx = e.clientX - r.left; my = e.clientY - r.top;
  });
  window.addEventListener('pointerup', function(e){
    var movedX = Math.abs(e.clientX - pdStartX), movedY = Math.abs(e.clientY - pdStartY);
    var wasClick = movedX < 4 && movedY < 4;
    var wasDrag = drag || pan;
    drag = false; pan = false; cv.style.cursor = 'grab';
    if (!wasClick || !wasDrag) return;  // if actually dragged, skip click logic
  });
  cv.addEventListener('click', function(e){
    markInteract();
    var movedX = Math.abs(e.clientX - pdStartX), movedY = Math.abs(e.clientY - pdStartY);
    if (movedX >= 4 || movedY >= 4) return;  // drag, not click
    var r = cv.getBoundingClientRect();
    var n = nodeAt(e.clientX - r.left, e.clientY - r.top);
    if (n && TRAVERSABLE) {
      setFocus(n.id);                                 // fullscreen: fly + spotlight + panel
    } else if (n) {
      selId = n.id;                                    // card: highlight; detail goes inline OR to a host slot
      if (DETAIL_INLINE) showPanel(n, countChildren(n.id));
      if (onNodeCb) onNodeCb(n.id);
    } else if (focusId != null || selId != null) {
      dismiss();                                      // click empty = dismiss summary / exit focus
    }
  });
  cv.addEventListener('mousemove', function(e){
    var r = cv.getBoundingClientRect(); mx = e.clientX-r.left; my = e.clientY-r.top;
  });
  cv.addEventListener('mouseleave', function(){ mx = -1; my = -1; });
  cv.addEventListener('contextmenu', function(e){ e.preventDefault(); });
  cv.addEventListener('wheel', function(e){
    markInteract();
    e.preventDefault(); zoom *= (e.deltaY<0 ? 1.12 : 0.89);
    zoom = Math.max(0.02, Math.min(10, zoom));
  }, {passive:false});
  cv.addEventListener('dblclick', function(e){
    markInteract();
    var r = cv.getBoundingClientRect(), n = nodeAt(e.clientX-r.left, e.clientY-r.top);
    if (n && api && api.expand){ api.expand(n.id); }
    else { userYaw=0; pitch=TILT; fitView(); }
  });
  document.addEventListener('keydown', function(e){
    if (e.key === 'Escape' && TRAVERSABLE && focusId != null){ clearFocus(); }
  });

  api = {
    reload: reload, fitView: fitView, mergeChildren: mergeChildren,
    select: function(id){ selId = id; },
    _setSel: function(id){ selId = id; },
    focus: function(id){
      var n = byId[id];
      if (n){ if (TRAVERSABLE){ setFocus(id); } else { selId=id; panX=0; panY=0; zoom=Math.max(zoom,1.4); } }
    },
    expand: function(id){ fetch('/api/kg/children?id='+encodeURIComponent(id)).then(function(r){return r.json();}).then(function(d){ if(d.ok) mergeChildren(d); }); },
    byId: function(id){ return byId[id]; },
    _byId: function(id){ return byId[id]; },
    onNode: function(cb){ onNodeCb = cb; },
    destroy: function(){ alive=false; if(rafId) cancelAnimationFrame(rafId); if(pollId) clearInterval(pollId); if(el.contains(cv)) el.removeChild(cv); }
  };

  loadCache();   // instant paint from last visit; reload() then refreshes in the background
  reload(); pollId = setInterval(reload, 60000); rafId = requestAnimationFrame(frame);

  // Draw only while the graph is on screen (the page keeps scrolling/animating smoothly).
  if (typeof IntersectionObserver !== 'undefined') {
    new IntersectionObserver(function(es){ onScreen = es[0].isIntersecting; }).observe(el);
  }
  if (typeof ResizeObserver !== 'undefined') {
    var ro = new ResizeObserver(function(){ if (fitted) fitView(); });
    ro.observe(el);
  }
  return api;
};

// backward-compat auto-init for any page with #zeus-graph
(function () {
  var z = document.getElementById('zeus-graph');
  if (z) window.KG = window.KGGraph(z, { box: null, accent: '56,189,248' });
})();

// legacy ZEUS detail-panel wiring (home.html has #zg-detail, #zg-close, etc.)
(function(){
  function esc(s){ return String(s==null?'':s).replace(/[&<>]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[c];}); }
  var panel=document.getElementById('zg-detail');
  if (!panel) return;
  var BRAND=(document.documentElement.getAttribute('data-brand')||'ARES').toUpperCase();
  var PAL={project:'245,158,11',folder:'56,189,248','file-cluster':'34,211,238',vault:'255,90,90',box:'220,240,255',dataset:'129,140,248',service:'74,222,128',vm:'250,204,21',disk:'34,211,238'};
  var curPath='';
  function open(id){
    if (window.KG && window.KG._setSel) window.KG._setSel(id);
    fetch('/api/kg/node?id='+encodeURIComponent(id)).then(function(r){ return r.json(); }).then(function(d){
      if (!d.ok) return;
      document.getElementById('zd-name').textContent=d.node.name;
      document.getElementById('zd-path').textContent=d.node.path; curPath=d.node.path;
      document.getElementById('zd-u').textContent=d.node.understanding||'—';
      var nb=document.getElementById('zd-nbrs'); var rows=[];
      d['in'].forEach(function(e){ rows.push(row(e,'▲ parent')); });
      d.out.forEach(function(e){ rows.push(row(e,'▼ child')); });
      nb.innerHTML=rows.join('')||'<span style="color:#6f8a9c;font-size:11px">no neighbors</span>';
      Array.prototype.forEach.call(nb.querySelectorAll('.nb'), function(x){ x.onclick=function(){ open(x.getAttribute('data-id')); }; });
      var ex=document.getElementById('zd-expand'); ex.onclick=function(){ if (window.KG) window.KG.expand(id); };
      panel.classList.add('open');
    });
  }
  function row(e,tag){ var c=PAL[e.kind]||'160,190,210';
    return '<span class="nb" data-id="'+esc(e.id)+'" style="color:rgb('+c+')" title="'+esc(tag)+'">'+esc(e.name)+' <span style="color:#6f8a9c;font-size:9px">'+esc(tag)+'</span></span>'; }
  document.getElementById('zg-close').onclick=function(){ panel.classList.remove('open'); if (window.KG) window.KG._setSel(null); };
  document.getElementById('zd-copy').onclick=function(){ if (navigator.clipboard) navigator.clipboard.writeText(curPath); };
  window.KG = window.KG || {}; window.KG.select = open;
})();

(function(){
  function esc(s){ return String(s==null?'':s).replace(/[&<>]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[c];}); }
  window.KG = window.KG || {};
  window.KG.expand = function(id){
    fetch('/api/kg/children?id='+encodeURIComponent(id)).then(function(r){ return r.json(); }).then(function(d){
      if (d.ok && window.KG.mergeChildren) window.KG.mergeChildren(d);
    });
  };
  var q=document.getElementById('zg-q'), box=document.getElementById('zg-results');
  if (!q) return;
  var t=null;
  function run(){
    var v=q.value.trim(); if (!v){ box.style.display='none'; box.innerHTML=''; return; }
    fetch('/api/kg/search?q='+encodeURIComponent(v)).then(function(r){ return r.json(); }).then(function(d){
      if (!d.ok || !d.results.length){ box.innerHTML='<div style="color:#6f8a9c">no matches</div>'; box.style.display='block'; return; }
      box.innerHTML=d.results.map(function(x){ return '<div data-id="'+esc(x.id)+'"><span class="rk">'+esc(x.kind)+'</span> '+esc(x.name)+'</div>'; }).join('');
      box.style.display='block';
      Array.prototype.forEach.call(box.querySelectorAll('div[data-id]'), function(el){ el.onclick=function(){
        var id=el.getAttribute('data-id'); box.style.display='none'; q.value=el.textContent.trim();
        if (window.KG.focus) window.KG.focus(id); if (window.KG.select) window.KG.select(id); }; });
    });
  }
  q.addEventListener('input', function(){ clearTimeout(t); t=setTimeout(run, 200); });
  q.addEventListener('keydown', function(e){ if (e.key==='Escape'){ box.style.display='none'; q.blur(); } });
})();

(function(){
  function ssdName(n){ var m=String(n||'').match(/T[0-9]+/i); return m?m[0].toUpperCase():(/portable|samsung/i.test(n)?'T5':(n||'SSD')); }
  function fmtAgo(ep){ if (!ep) return '—'; var s=Math.floor(Date.now()/1000)-ep; if (s<3600) return Math.floor(s/60)+'m'; if (s<86400) return Math.floor(s/3600)+'h'; return Math.floor(s/86400)+'d'; }
  function pollFleet(){
    fetch('/api/fleet').then(function(r){ return r.json(); }).then(function(d){
      var b=d.backups||{}, sy=function(o){ return (o&&o.state==='OK')?'✓':'⚠'; };
      var bk=document.getElementById('zg-bkp'); if (bk) bk.textContent = sy(b.ares_to_zeus)+' / '+sy(b.zeus_to_ares)+'  ·  '+fmtAgo((b.ares_to_zeus||{}).epoch)+' ago';
      var ss=document.getElementById('zg-ssd'); var disks=b.disks||[];
      if (ss) ss.innerHTML = disks.length ? ['T5','T9','T7'].map(function(nm){ var dr=disks.find(function(x){ return ssdName(x.name)===nm; })||{};
        return nm+' '+(dr.use!=null?dr.use+'%':'—')+(dr.temp!=null?' '+dr.temp+'°':'')+(/PASS|OK/i.test(dr.health||'')?' ✓':''); }).join('&nbsp; ') : 'asleep · reads at 4am';
    }).catch(function(){});
  }
  function wake(){ var el=document.getElementById('zg-wake'); if (!el) return;
    var now=new Date(), w=new Date(now); w.setHours(4,0,0,0); if (w<=now) w.setDate(w.getDate()+1);
    var r=Math.max(0,Math.floor((w-now)/1000)), z=function(n){ return (n<10?'0':'')+n; };
    el.textContent=z(Math.floor(r/3600))+':'+z(Math.floor(r%3600/60))+':'+z(r%60); }
  if (!document.getElementById('zg-bkp') && !document.getElementById('zg-ssd') && !document.getElementById('zg-wake')) return;
  pollFleet(); setInterval(pollFleet, 60000); wake(); setInterval(wake, 1000);
})();
