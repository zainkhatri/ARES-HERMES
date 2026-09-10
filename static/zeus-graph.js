window.KGGraph = function (el, opts) {
  if (!el) return null;
  opts = opts || {};
  var BOX = (opts.box == null ? null : opts.box);
  var ACCENT = opts.accent || '125,205,255';
  var LIMIT = opts.limit || 90;
  var onNodeCb = null, rafId = null, alive = true, api = null, pollId = null;
  var cv = document.createElement('canvas'); el.appendChild(cv);
  var ctx = cv.getContext('2d'), dpr = 1;                 // 1 = far fewer pixels to clear/draw each frame
  var N = [], L = [], byId = {}, spin = 0, hover = null, mx = -1, my = -1, selId = null;
  var zoom = 1, userYaw = 0, pitch = .42, panX = 0, panY = 0, drag = false, pan = false, lx = 0, ly = 0;
  var alpha = 1, fitted = false;                          // alpha cools the sim to a stop; fitted = auto-fit done
  var COL = {project:'245,158,11', folder:'56,189,248', 'file-cluster':'34,211,238', vault:'255,90,90',
             box:'220,240,255', host:'220,240,255', dataset:'129,140,248', database:'129,140,248',
             service:'74,222,128', vm:'250,204,21', disk:'34,211,238'};
  function col(k){ return COL[k] || '160,190,210'; }
  function esc(s){ return String(s==null?'':s).replace(/[&<>]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[c];}); }
  function size(){ var r=el.getBoundingClientRect(); var w=Math.max(1,Math.round(r.width*dpr)), h=Math.max(1,Math.round(r.height*dpr));
    if (cv.width!==w||cv.height!==h){ cv.width=w; cv.height=h; } return [cv.width, cv.height]; }
  function ingest(d){
    var prev={}; N.forEach(function(n){ prev[n.id]=n; });
    N = d.nodes.map(function(nd){ var p=prev[nd.id]||{};
      return { id:nd.id, kind:nd.kind, name:nd.name, u:nd.understanding, depth:nd.depth,
        x:(p.x!=null?p.x:(Math.random()-.5)*6), y:(p.y!=null?p.y:(Math.random()-.5)*6),
        z:(p.z!=null?p.z:(Math.random()-.5)*6), vx:0, vy:0, vz:0 }; });
    byId={}; N.forEach(function(n){ byId[n.id]=n; });
    L = d.edges.filter(function(e){ return byId[e.src]&&byId[e.dst]; }).map(function(e){ return { a:byId[e.src], b:byId[e.dst] }; });
    var kinds={}; N.forEach(function(n){ kinds[n.kind]=(kinds[n.kind]||0)+1; });
    var lg=document.getElementById('zg-legend'); if (lg) lg.innerHTML = Object.keys(kinds).map(function(k){ return '<span><i style="background:rgb('+col(k)+')"></i>'+k+'</span>'; }).join('');
    var cc=document.getElementById('zg-count'); if (cc) cc.textContent = d.shown+'/'+d.total_nodes+' nodes · '+d.total_edges+' edges';
    settle(170); alpha = 0; fitView(); fitted = true;     // pre-settle + fit synchronously, then freeze
  }
  function reload(){
    var url = '/api/kg?limit=' + LIMIT + (BOX ? '&box=' + encodeURIComponent(BOX) : '');
    fetch(url).then(function(r){ return r.json(); }).then(function(d){
      if (!d.ok || !d.nodes || !d.nodes.length) throw 0; ingest(d);
    }).catch(function(){ fetch('/static/_kg_sample.json').then(function(r){ return r.json(); }).then(ingest).catch(function(){
      var cc=document.getElementById('zg-count'); if (cc) cc.textContent='graph offline'; }); });
  }
  function mergeChildren(d){
    var cur={}; N.forEach(function(n){ cur[n.id]=n; });
    var anchor=cur[d.edges.length?d.edges[0].src:null];
    d.nodes.forEach(function(nd){ if (cur[nd.id]) return;
      N.push({ id:nd.id, kind:nd.kind, name:nd.name, u:nd.understanding, depth:nd.depth,
        x:(anchor?anchor.x:0)+(Math.random()-.5)*2, y:(anchor?anchor.y:0)+(Math.random()-.5)*2,
        z:(anchor?anchor.z:0)+(Math.random()-.5)*2, vx:0,vy:0,vz:0 }); });
    byId={}; N.forEach(function(n){ byId[n.id]=n; });
    d.edges.forEach(function(e){ if (byId[e.src]&&byId[e.dst]) L.push({ a:byId[e.src], b:byId[e.dst] }); });
    settle(60);                                           // settle the new children (keep current view)
  }
  function step(){
    var i,j;
    for (i=0;i<N.length;i++){ N[i].fx=0; N[i].fy=0; N[i].fz=0; }
    for (i=0;i<N.length;i++) for (j=i+1;j<N.length;j++){ var a=N[i],b=N[j];
      var dx=a.x-b.x,dy=a.y-b.y,dz=a.z-b.z,d2=dx*dx+dy*dy+dz*dz+.5,dd=Math.sqrt(d2),f=1.1/d2;
      dx/=dd;dy/=dd;dz/=dd; a.fx+=dx*f;a.fy+=dy*f;a.fz+=dz*f; b.fx-=dx*f;b.fy-=dy*f;b.fz-=dz*f; }
    for (var k=0;k<L.length;k++){ var e=L[k],dx=e.b.x-e.a.x,dy=e.b.y-e.a.y,dz=e.b.z-e.a.z,dd=Math.sqrt(dx*dx+dy*dy+dz*dz)+.001,f=.09*(dd-1.2)/dd;
      e.a.fx+=dx*f;e.a.fy+=dy*f;e.a.fz+=dz*f; e.b.fx-=dx*f;e.b.fy-=dy*f;e.b.fz-=dz*f; }
    for (i=0;i<N.length;i++){ var n=N[i]; n.fx+=-n.x*.02; n.fy+=-n.y*.02; n.fz+=-n.z*.02;
      n.vx=(n.vx+n.fx*.1)*.85; n.vy=(n.vy+n.fy*.1)*.85; n.vz=(n.vz+n.fz*.1)*.85; n.x+=n.vx; n.y+=n.vy; n.z+=n.vz;
      var rr=Math.sqrt(n.x*n.x+n.y*n.y+n.z*n.z); if (rr>7){ var sc=7/rr; n.x*=sc; n.y*=sc; n.z*=sc; } }  // cap spread — no fly-aways
  }
  function settle(iters){ for (var s=0;s<iters;s++) step(); }   // run the sim to rest synchronously
  function proj(n,W,H){ var yaw=spin+userYaw,c=Math.cos(yaw),s=Math.sin(yaw),x1=n.x*c-n.z*s,z1=n.x*s+n.z*c;
    var y1=n.y*Math.cos(pitch)-z1*Math.sin(pitch),z2=n.y*Math.sin(pitch)+z1*Math.cos(pitch),K2=9,ooz=1/(K2+z2),Kp=Math.min(W,H)*0.9*zoom;
    return [W/2+panX+Kp*ooz*x1, H/2+panY-Kp*ooz*y1, ooz]; }
  // zoom+pan so the entire graph fits the viewport (the "big picture")
  function fitView(){
    if (!N.length) return;
    var d=size(),W=d[0]/dpr,H=d[1]/dpr;
    zoom=1; panX=0; panY=0;
    var minx=1e9,maxx=-1e9,miny=1e9,maxy=-1e9;
    for (var i=0;i<N.length;i++){ var p=proj(N[i],W,H); if(p[0]<minx)minx=p[0]; if(p[0]>maxx)maxx=p[0]; if(p[1]<miny)miny=p[1]; if(p[1]>maxy)maxy=p[1]; }
    var z=Math.min(W*0.94/Math.max(1,maxx-minx), H*0.90/Math.max(1,maxy-miny)); z=Math.max(.04,Math.min(3,z));
    var cx=(minx+maxx)/2, cy=(miny+maxy)/2;
    zoom=z; panX=-(cx-W/2)*z; panY=-(cy-H/2)*z;
  }
  function frame(){
    if (!alive) return;
    rafId = requestAnimationFrame(frame);
    var d=size(),W=d[0]/dpr,H=d[1]/dpr; ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,W,H);
    if (N.length && alpha>0.02){ step(); alpha*=0.97; if (!fitted && alpha<0.25){ fitView(); fitted=true; } }
    // edges — depth-shaded strokes, NO shadowBlur (that was the lag)
    for (var k=0;k<L.length;k++){ var pa=proj(L[k].a,W,H),pb=proj(L[k].b,W,H),oz=(pa[2]+pb[2])/2,ea=Math.max(.12,Math.min(.6,(oz-0.05)*7));
      ctx.strokeStyle='rgba('+ACCENT+','+ea+')'; ctx.lineWidth=Math.max(.5,oz*10*zoom);
      ctx.beginPath(); ctx.moveTo(pa[0],pa[1]); ctx.lineTo(pb[0],pb[1]); ctx.stroke(); }
    var order=N.map(function(n){ return {n:n,p:proj(n,W,H)}; }).sort(function(a,b){ return a.p[2]-b.p[2]; });
    hover=null; var best=280;
    if (mx>=0) order.forEach(function(o){ var dxp=o.p[0]-mx,dyp=o.p[1]-my,dm=dxp*dxp+dyp*dyp; if (dm<best){ best=dm; hover=o.n; } });
    for (var oi=0; oi<order.length; oi++){ var o=order[oi], n=o.n, p=o.p, c=col(n.kind), sel=(n.id===selId), hot=(n===hover||sel);
      var r=(hot?4.8:(n.kind==='project'?3.2:2.0))*(p[2]*10);
      if (hot){ ctx.shadowColor='rgb('+c+')'; ctx.shadowBlur=14; }     // glow only on the 1-2 hot nodes
      ctx.globalAlpha=1; ctx.fillStyle='rgb('+c+')';
      ctx.beginPath(); ctx.arc(p[0],p[1],Math.max(1.2,r),0,6.29); ctx.fill();
      if (hot) ctx.shadowBlur=0;
      if (sel){ ctx.strokeStyle='rgba('+c+',.85)'; ctx.lineWidth=1.5; ctx.beginPath(); ctx.arc(p[0],p[1],Math.max(1.2,r)+4,0,6.29); ctx.stroke(); }
      if (n.kind==='project'||n.kind==='box'||n.kind==='dataset'||hot){ ctx.globalAlpha=hot?1:.72; ctx.fillStyle='rgb('+c+')';
        ctx.font=(hot?'bold 12px':'9px')+" 'JetBrains Mono',ui-monospace,monospace"; ctx.textAlign='center'; ctx.fillText(n.name,p[0],p[1]-9); ctx.textAlign='start'; ctx.globalAlpha=1; } }
    var tip=document.getElementById('zg-tip');
    if (tip){ if (hover && !drag){ var pp=proj(hover,W,H); tip.style.display='block'; tip.style.left=Math.min(window.innerWidth-290,pp[0]+12)+'px'; tip.style.top=(pp[1]+12)+'px';
      tip.innerHTML='<div class="kk">'+esc(hover.kind)+'</div><b>'+esc(hover.name)+'</b><br>'+esc(hover.u||'—'); }
    else tip.style.display='none'; }
  }
  function nodeAt(cx,cy){ var d=size(),W=d[0]/dpr,H=d[1]/dpr,best=300,found=null;
    N.forEach(function(n){ var p=proj(n,W,H),dx=p[0]-cx,dy=p[1]-cy,dm=dx*dx+dy*dy; if (dm<best){ best=dm; found=n; } }); return found; }
  cv.addEventListener('mousemove', function(e){ var r=cv.getBoundingClientRect();
    if (drag){ userYaw+=(e.clientX-lx)*0.008; pitch=Math.max(-0.25,Math.min(1.45,pitch+(e.clientY-ly)*0.006)); lx=e.clientX; ly=e.clientY; mx=-1; return; }
    if (pan){ panX+=(e.clientX-lx); panY+=(e.clientY-ly); lx=e.clientX; ly=e.clientY; mx=-1; return; }
    mx=e.clientX-r.left; my=e.clientY-r.top; });
  cv.addEventListener('mouseleave', function(){ mx=-1; my=-1; });
  cv.addEventListener('mousedown', function(e){ if (e.shiftKey||e.button===2){ pan=true; } else { drag=true; } lx=e.clientX; ly=e.clientY; cv.style.cursor='grabbing'; });
  window.addEventListener('mouseup', function(){ drag=false; pan=false; cv.style.cursor='grab'; });
  cv.addEventListener('contextmenu', function(e){ e.preventDefault(); });
  cv.addEventListener('wheel', function(e){ e.preventDefault(); zoom*=(e.deltaY<0?1.12:0.89); zoom=Math.max(.06,Math.min(8,zoom)); }, {passive:false});
  cv.addEventListener('dblclick', function(e){ var r=cv.getBoundingClientRect(),n=nodeAt(e.clientX-r.left,e.clientY-r.top);
    if (n && api && api.expand){ api.expand(n.id); } else { userYaw=0; pitch=.42; fitView(); } });
  cv.addEventListener('click', function(e){ var r=cv.getBoundingClientRect(),n=nodeAt(e.clientX-r.left,e.clientY-r.top);
    if (n){ if (onNodeCb) onNodeCb(n.id); } });
  api = {
    reload: reload, fitView: fitView, mergeChildren: mergeChildren,
    select: function (id) { selId = id; },
    _setSel: function (id) { selId = id; },   // ponytail: kept for detail-panel IIFE compat
    focus: function (id) { var n = byId[id]; if (n) { selId = id; panX = 0; panY = 0; zoom = Math.max(zoom, 1.4); } },
    expand: function (id) { fetch('/api/kg/children?id=' + encodeURIComponent(id)).then(function(r){return r.json();}).then(function(d){ if (d.ok) mergeChildren(d); }); },
    byId: function (id) { return byId[id]; },
    _byId: function (id) { return byId[id]; }, // ponytail: kept for any legacy callers
    onNode: function (cb) { onNodeCb = cb; },
    destroy: function () { alive = false; if (rafId) cancelAnimationFrame(rafId); if (pollId) clearInterval(pollId); if (el.contains(cv)) el.removeChild(cv); }
  };
  reload(); pollId = setInterval(reload, 60000); rafId = requestAnimationFrame(frame);
  // refit when the host element resizes (e.g. KG card grows via flex after fitScale reveals the HUD)
  if (typeof ResizeObserver !== 'undefined') {
    var ro = new ResizeObserver(function(){ if (fitted) fitView(); });
    ro.observe(el);
  }
  return api;
};
// backward-compat auto-init for the ZEUS full-screen view
(function () {
  var z = document.getElementById('zeus-graph');
  if (z) window.KG = window.KGGraph(z, { box: null, accent: '56,189,248' });
})();

(function(){
  function esc(s){ return String(s==null?'':s).replace(/[&<>]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[c];}); }
  var panel=document.getElementById('zg-detail');
  var COL={project:'245,158,11',folder:'56,189,248','file-cluster':'34,211,238',vault:'255,90,90',box:'220,240,255',dataset:'129,140,248',service:'74,222,128',vm:'250,204,21',disk:'34,211,238'};
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
  function row(e,tag){ var c=COL[e.kind]||'160,190,210';
    return '<span class="nb" data-id="'+esc(e.id)+'" style="color:rgb('+c+')" title="'+esc(tag)+'">'+esc(e.name)+' <span style="color:#6f8a9c;font-size:9px">'+esc(tag)+'</span></span>'; }
  document.getElementById('zg-close').onclick=function(){ panel.classList.remove('open'); if (window.KG) window.KG._setSel(null); };
  document.getElementById('zd-copy').onclick=function(){ if (navigator.clipboard) navigator.clipboard.writeText(curPath); };
  var _sel=(window.KG&&window.KG.select);
  window.KG = window.KG || {};
  window.KG.select = open;   // graph click → open detail
})();
(function(){
  function esc(s){ return String(s==null?'':s).replace(/[&<>]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[c];}); }
  // expand: pull children and merge into the live graph
  window.KG = window.KG || {};
  window.KG.expand = function(id){
    fetch('/api/kg/children?id='+encodeURIComponent(id)).then(function(r){ return r.json(); }).then(function(d){
      if (d.ok && window.KG.mergeChildren) window.KG.mergeChildren(d);
    });
  };
  // search: query -> dropdown -> focus+select
  var q=document.getElementById('zg-q'), box=document.getElementById('zg-results'), t=null;
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
  pollFleet(); setInterval(pollFleet, 60000); wake(); setInterval(wake, 1000);
})();
