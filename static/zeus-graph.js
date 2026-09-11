window.KGGraph = function (el, opts) {
  if (!el) return null;
  opts = opts || {};
  var BOX = (opts.box == null ? null : opts.box);
  var ACCENT = opts.accent || '125,205,255';
  var LIMIT = opts.limit || 90;
  var TRAVERSABLE = !!opts.traversable;  // fullscreen only gets focus/spotlight
  var DETAIL_INLINE = opts.detailInline !== false;  // false = host renders detail elsewhere (a slot) via onNode
  var onNodeCb = null, rafId = null, alive = true, api = null, pollId = null;

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
    if (BRAND !== 'ZEUS' && k === 'folder' && depth != null && depth >= 6) {
      return BRAND === 'EROS' ? '180,120,40' : '180,60,40';  // ember for deep folders
    }
    return PAL[k] || PAL._default;
  }

  function esc(s){ return String(s==null?'':s).replace(/[&<>]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[c];}); }

  var cv = document.createElement('canvas');
  cv.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;cursor:grab';
  // ponytail: don't override el.style.position — caller's HTML sets it (absolute/relative)
  if (!el.style.position) el.style.position = 'relative';
  el.appendChild(cv);
  var ctx = cv.getContext('2d'), dpr = 1;
  var N = [], L = [], byId = {}, nbr = new Map();
  var spin = 0, hover = null, mx = -1, my = -1, selId = null;
  var zoom = 1, userYaw = 0, pitch = .42, panX = 0, panY = 0;
  var drag = false, pan = false, lx = 0, ly = 0;
  var pdx = 0, pdy = 0;  // pointer delta for click-vs-drag
  var alpha = 1, fitted = false;
  var lastInteract = -Infinity;
  // camera ease target (for focus)
  var camTarget = null;
  var focusId = null;
  var litSet = null;

  // --- legend DOM (always; card + fullscreen) ---
  var legEl = document.createElement('div');
  legEl.style.cssText = 'position:absolute;bottom:8px;left:8px;z-index:10;display:flex;flex-wrap:wrap;gap:4px 8px;pointer-events:none;max-width:55%';
  el.appendChild(legEl);

  // --- hint line (fullscreen only) ---
  var hintEl = null;
  if (TRAVERSABLE) {
    hintEl = document.createElement('div');
    hintEl.style.cssText = 'position:absolute;bottom:8px;left:50%;transform:translateX(-50%);z-index:10;color:rgba(255,255,255,0.38);font:10px ui-monospace,monospace;pointer-events:none;white-space:nowrap';
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

  function updateLegend() {
    var kinds = {};
    N.forEach(function(n){ kinds[n.kind] = (kinds[n.kind]||0)+1; });
    legEl.innerHTML = Object.keys(kinds).map(function(k){
      return '<span style="display:flex;align-items:center;gap:4px;color:rgba(255,255,255,0.7);font:10px ui-monospace,monospace">' +
        '<i style="display:inline-block;width:8px;height:8px;border-radius:50%;background:rgb('+col(k)+')" ></i>' + k + '</span>';
    }).join('');
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
    L = d.edges.filter(function(e){ return byId[e.src] && byId[e.dst]; })
               .map(function(e){ return { a:byId[e.src], b:byId[e.dst] }; });
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
    updateLegend();
    settle(N.length > 250 ? 95 : 170); alpha = 0; fitView(); fitted = true;
  }

  function reload() {
    var url = '/api/kg?limit=' + LIMIT + (BOX ? '&box=' + encodeURIComponent(BOX) : '');
    fetch(url).then(function(r){ return r.json(); }).then(function(d){
      if (!d.ok || !d.nodes || !d.nodes.length) throw 0; ingest(d);
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
    settle(60);
  }

  function step() {
    var i, j;
    for (i=0;i<N.length;i++){ N[i].fx=0; N[i].fy=0; N[i].fz=0; }
    for (i=0;i<N.length;i++) for (j=i+1;j<N.length;j++){ var a=N[i],b=N[j];
      var dx=a.x-b.x,dy=a.y-b.y,dz=a.z-b.z,d2=dx*dx+dy*dy+dz*dz+.5,dd=Math.sqrt(d2),f=1.1/d2;
      dx/=dd;dy/=dd;dz/=dd; a.fx+=dx*f;a.fy+=dy*f;a.fz+=dz*f; b.fx-=dx*f;b.fy-=dy*f;b.fz-=dz*f; }
    for (var k=0;k<L.length;k++){ var e=L[k],edx=e.b.x-e.a.x,edy=e.b.y-e.a.y,edz=e.b.z-e.a.z,dd=Math.sqrt(edx*edx+edy*edy+edz*edz)+.001,f=.09*(dd-1.2)/dd;
      e.a.fx+=edx*f;e.a.fy+=edy*f;e.a.fz+=edz*f; e.b.fx-=edx*f;e.b.fy-=edy*f;e.b.fz-=edz*f; }
    for (i=0;i<N.length;i++){ var n=N[i]; n.fx+=-n.x*.02; n.fy+=-n.y*.02; n.fz+=-n.z*.02;
      n.vx=(n.vx+n.fx*.1)*.85; n.vy=(n.vy+n.fy*.1)*.85; n.vz=(n.vz+n.fz*.1)*.85;
      n.x+=n.vx; n.y+=n.vy; n.z+=n.vz;
      var rr=Math.sqrt(n.x*n.x+n.y*n.y+n.z*n.z); if (rr>11){ var sc=11/rr; n.x*=sc; n.y*=sc; n.z*=sc; } }
  }

  function settle(iters){ for (var s=0;s<iters;s++) step(); }

  function proj(n, W, H){
    var yaw = spin + userYaw, c = Math.cos(yaw), s = Math.sin(yaw);
    var x1 = n.x*c - n.z*s, z1 = n.x*s + n.z*c;
    var y1 = n.y*Math.cos(pitch) - z1*Math.sin(pitch), z2 = n.y*Math.sin(pitch) + z1*Math.cos(pitch);
    var K2 = 15, ooz = 1/(K2+z2), Kp = Math.min(W,H)*0.9*zoom;
    return [W/2+panX+Kp*ooz*x1, H/2+panY-Kp*ooz*y1, ooz];
  }

  function fitView() {
    if (!N.length) return;
    var d = size(), W = d[0]/dpr, H = d[1]/dpr;
    zoom = 1; panX = 0; panY = 0;
    var minx=1e9,maxx=-1e9,miny=1e9,maxy=-1e9;
    for (var i=0;i<N.length;i++){ var p=proj(N[i],W,H); if(p[0]<minx)minx=p[0]; if(p[0]>maxx)maxx=p[0]; if(p[1]<miny)miny=p[1]; if(p[1]>maxy)maxy=p[1]; }
    // card = fit with margin (fully visible, not edge-clipped, lighter); fullscreen fills
    var fw = TRAVERSABLE ? 0.94 : 0.80, fh = TRAVERSABLE ? 0.92 : 0.76;
    var z = Math.min(W*fw/Math.max(1,maxx-minx), H*fh/Math.max(1,maxy-miny));
    z = Math.max(0.02, Math.min(6, z));
    var cx=(minx+maxx)/2, cy=(miny+maxy)/2;
    zoom=z; panX=-(cx-W/2)*z; panY=-(cy-H/2)*z;
    camTarget = null;
  }

  function frame() {
    if (!alive) return;
    rafId = requestAnimationFrame(frame);
    var d = size(), W = d[0]/dpr, H = d[1]/dpr;
    ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,W,H);

    // sim step while hot
    if (N.length && alpha > 0.02){ step(); alpha *= 0.97; if (!fitted && alpha < 0.25){ fitView(); fitted = true; } }

    // auto-spin: advances every frame regardless of alpha; pauses 5s after interaction
    var now = performance.now();
    if (now - lastInteract > 5000) { spin += 0.0016; }

    // camera ease toward target
    if (camTarget) {
      var tz = camTarget.zoom != null ? camTarget.zoom : zoom;
      zoom   += (tz - zoom)   * 0.09;
      panX   += (camTarget.panX - panX) * 0.09;
      panY   += (camTarget.panY - panY) * 0.09;
      if (Math.abs(tz-zoom)<0.001 && Math.abs(camTarget.panX-panX)<0.5 && Math.abs(camTarget.panY-panY)<0.5) {
        zoom = tz; panX = camTarget.panX; panY = camTarget.panY; camTarget = null;
      }
    }

    // compute lit set alpha for spotlight
    var hasFocus = TRAVERSABLE && focusId != null && litSet != null;

    // edges
    for (var k=0;k<L.length;k++){
      var pa = proj(L[k].a,W,H), pb = proj(L[k].b,W,H);
      var oz = (pa[2]+pb[2])/2, ea = Math.max(.12, Math.min(.6,(oz-0.05)*7));
      var dimEdge = hasFocus && !(litSet.has(L[k].a.id) && litSet.has(L[k].b.id));
      ctx.globalAlpha = dimEdge ? 0.06 : ea;
      ctx.strokeStyle = 'rgba('+ACCENT+',1)';
      ctx.lineWidth = Math.max(.5, oz*10*zoom);
      ctx.beginPath(); ctx.moveTo(pa[0],pa[1]); ctx.lineTo(pb[0],pb[1]); ctx.stroke();
    }
    ctx.globalAlpha = 1;

    // nodes
    var order = N.map(function(n){ return {n:n, p:proj(n,W,H)}; }).sort(function(a,b){ return a.p[2]-b.p[2]; });
    hover = null; var best = 280;
    if (mx >= 0) order.forEach(function(o){ var dxp=o.p[0]-mx,dyp=o.p[1]-my,dm=dxp*dxp+dyp*dyp; if(dm<best){best=dm;hover=o.n;} });

    for (var oi=0; oi<order.length; oi++){
      var o = order[oi], n = o.n, p = o.p;
      var c = col(n.kind, n.depth);
      var sel = (n.id === selId), hot = (n === hover || sel);
      var dim = hasFocus && !litSet.has(n.id);
      var bk = n.kind, ds = n.depth || 5;
      var base = bk==='file-cluster' ? 1.35 : bk==='vault' ? 2.3 : (bk==='box'||bk==='host') ? 5.4
               : bk==='project' ? 3.0 : ds<=3 ? 5.0 : ds===4 ? 4.2 : ds===5 ? 3.1 : ds===6 ? 2.3 : 1.7;
      var r = Math.min(26, (hot ? base*1.5 : base) * (p[2]*10));
      ctx.globalAlpha = dim ? 0.10 : 1;
      if (hot && !dim){ ctx.shadowColor = 'rgb('+c+')'; ctx.shadowBlur = 14; }
      ctx.fillStyle = 'rgb('+c+')';
      ctx.beginPath(); ctx.arc(p[0],p[1],Math.max(1.2,r),0,6.29); ctx.fill();
      if (hot && !dim) ctx.shadowBlur = 0;
      if (sel && !dim){
        ctx.strokeStyle = 'rgba('+c+',.85)'; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(p[0],p[1],Math.max(1.2,r)+4,0,6.29); ctx.stroke();
      }
      var showLabel = !dim && (n.kind==='project'||n.kind==='box'||n.kind==='dataset'||(n.depth||9)<=4||hot||(hasFocus&&litSet.has(n.id)));
      if (showLabel){
        ctx.globalAlpha = hot ? 1 : (dim ? 0 : 0.72);
        ctx.fillStyle = 'rgb('+c+')';
        ctx.font = (hot?'bold 12px':'9px')+" 'JetBrains Mono',ui-monospace,monospace";
        ctx.textAlign = 'center'; ctx.fillText(n.name, p[0], p[1]-9); ctx.textAlign = 'start';
      }
      ctx.globalAlpha = 1;
    }

    // hover tooltip (legacy element if present)
    var tip = document.getElementById('zg-tip');
    if (tip){ if (hover && !drag){ var pp=proj(hover,W,H); tip.style.display='block'; tip.style.left=Math.min(window.innerWidth-290,pp[0]+12)+'px'; tip.style.top=(pp[1]+12)+'px';
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
    var p = proj(n, W, H);
    var tpanX = panX + (W/2 - p[0]) * zoom;
    var tpanY = panY + (H/2 - p[1]) * zoom;
    camTarget = { zoom: Math.max(zoom, 1.2), panX: tpanX, panY: tpanY };
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
    else { userYaw=0; pitch=.42; fitView(); }
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

  reload(); pollId = setInterval(reload, 60000); rafId = requestAnimationFrame(frame);

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
