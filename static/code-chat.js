// code-chat.js — the Code page: the ARES iPhone app's Claude chat on the web. It talks to the
// same ares-code server (Caddy /code/ws) with the same ops, so a chat here is a real Claude tab
// on ARES, the one the phone and the Mac see. Auth: a 60 s ticket from /api/code/ticket.
(function () {
  'use strict';
  const S = window.CodeStore, MD = window.CodeMD, esc = MD.esc;
  const ROOT = '/mnt/nvme/PROMETHEUS';
  const PHONE_HOST = 'ares.tail3045df.ts.net';
  const $ = (id) => document.getElementById(id);
  const el = { list: $('chats-list'), meta: $('chats-meta'), search: $('chats-search'), col: $('tx-col'), tx: $('transcript'),
    title: $('convo-title'), chip: $('convo-project'), state: $('convo-state'), linkText: $('link-text'), working: $('working'),
    workingText: $('working-text'), tprompt: $('tprompt'), tpromptText: $('tprompt-text'), notice: $('notice'), input: $('input'),
    send: $('send'), form: $('composer'), file: $('file'), attach: $('attach-row'), skills: $('skills'), skillsList: $('skills-list'),
    jump: $('jump'), chats: $('chats'), scrim: $('scrim'), ctx: $('ctx'), drop: $('drop'), convo: $('convo') };
  const store = new S.Store();
  const st = { ws: null, connected: false, reqs: new Map(), nextId: 1, backoff: 600, sessions: [], sid: null, cwd: ROOT,
    row: null, isTerminal: false, opening: false, buffered: [], sending: false, images: [], skills: null, skillSel: 0,
    expanded: new Set(), gen: 0, firstList: true, noticeTimer: 0 };

  // ---- connection -------------------------------------------------------------------
  async function connect() {
    setLink();
    let ticket = '';
    try {
      const r = await fetch('/api/code/ticket', { method: 'POST', credentials: 'same-origin' });
      if (r.status === 401) { location.href = '/login'; return; }
      ticket = (await r.json()).ticket || '';
    } catch (e) { /* retried below */ }
    if (!ticket) { retry(); return; }
    const ws = new WebSocket(wsBase() + '/code/ws?ticket=' + encodeURIComponent(ticket));
    st.ws = ws;
    ws.onopen = async () => {
      st.connected = true; st.backoff = 600; setLink();
      await loadSessions();
      if (st.sid && st.isTerminal) await attach(st.gen);
    };
    ws.onmessage = (e) => { let m; try { m = JSON.parse(e.data); } catch (_) { return; } onMessage(m); };
    ws.onclose = () => {
      if (st.ws !== ws) return;
      st.connected = false; st.ws = null;
      for (const [, r] of st.reqs) { clearTimeout(r.timer); r.reject({ code: 'offline', message: 'Lost the connection to ARES' }); }
      st.reqs.clear();
      setLink(); retry();
    };
  }
  // Caddy (and tailscale serve) route /code/* to ares-code. The plain :8080 address is DNAT'd
  // straight to the dashboard container and has no /code route, so it uses the phone's host.
  function wsBase() {
    const viaProxy = location.protocol === 'https:' || location.port === '8443';
    return viaProxy ? (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host : 'wss://' + PHONE_HOST;
  }
  function retry() { setTimeout(connect, st.backoff); st.backoff = Math.min(st.backoff * 2, 8000); }

  function request(op, params, timeoutMs) {
    return new Promise((resolve, reject) => {
      if (!st.ws || !st.connected) { reject({ code: 'offline', message: 'Not connected to ARES' }); return; }
      const id = 'w' + (st.nextId++);
      const timer = setTimeout(() => { st.reqs.delete(id); reject({ code: 'timeout', message: 'ARES did not answer in time' }); }, timeoutMs || 20000);
      st.reqs.set(id, { resolve, reject, timer });
      st.ws.send(JSON.stringify(Object.assign({ id, op }, params || {})));
    });
  }

  function onMessage(m) {
    if ('ok' in m && m.id && st.reqs.has(m.id)) {
      const r = st.reqs.get(m.id); st.reqs.delete(m.id); clearTimeout(r.timer);
      if (m.ok) r.resolve(m.result || {}); else r.reject(m.error || { code: 'error', message: 'Request failed' });
      return;
    }
    if (!m.type) return;
    if (m.session && m.session !== st.sid) return;
    if (st.opening) { st.buffered.push(m); return; }
    store.apply(m);
    if (m.type === 'status' && m.state === 'stopped') { st.isTerminal = false; loadSessions(); }
    schedule();
  }

  // ---- chats ------------------------------------------------------------------------
  async function loadSessions() {
    try { st.sessions = (await request('sessions', { all: true })).sessions || []; } catch (e) { return; }
    if (st.sid) st.row = st.sessions.find((s) => s.id === st.sid) || st.row;
    renderChats();
    if (st.firstList) {
      st.firstList = false;
      const want = decodeURIComponent(location.hash.slice(1));
      const row = want && st.sessions.find((s) => s.id === want);
      if (row) select(row);
    }
    schedule();
  }

  function renderChats() {
    const secs = S.sections(st.sessions, el.search.value);
    const live = st.sessions.filter((s) => s.live).length;
    el.meta.textContent = live + ' live';
    if (!secs.length) { el.list.innerHTML = '<div class="chats-empty">' + (st.sessions.length ? 'No chats match.' : 'No chats yet.') + '</div>'; return; }
    el.list.innerHTML = secs.map((sec) => '<div class="sec">' + esc(sec.name) + (sec.name === 'Live' ? '<b>' + sec.rows.length + '</b>' : '') + '</div>' +
      sec.rows.map((r) => '<button type="button" class="row' + (r.id === st.sid ? ' is-on' : '') + '" data-sid="' + esc(r.id) + '">' +
        '<div class="row-t">' + (r.live ? '<i class="live"></i>' : r.shelved ? '<i class="shelf"></i>' : '') + '<span>' + esc(r.title || 'Untitled chat') + '</span></div>' +
        '<div class="row-s">' + esc([r.project, S.ago(r.updated)].filter(Boolean).join(' · ')) + '</div></button>').join('')).join('');
  }

  function closeCurrent() {
    if (st.sid && st.isTerminal && st.connected) request('close', { session: st.sid }).catch(() => {});
  }

  function newChat() {
    closeCurrent(); st.gen++;
    store.reset(); st.sid = null; st.row = null; st.cwd = ROOT; st.isTerminal = false; st.expanded.clear();
    history.replaceState(null, '', location.pathname);
    hideNotice(); drawer(false); renderChats(); schedule(true); el.input.focus();
  }

  async function select(row) {
    closeCurrent(); const gen = ++st.gen;
    store.reset(); st.expanded.clear();
    st.sid = row.id; st.row = row; st.cwd = row.cwd || ROOT; st.isTerminal = false;
    history.replaceState(null, '', '#' + row.id);
    hideNotice(); drawer(false); renderChats(); schedule(true);
    if (row.live) { await openTab(gen); return; }
    // A saved chat shows its history; a Claude tab opens only when you send to it.
    try {
      const h = await request('history', { session: row.id, path: st.cwd }, 30000);
      if (gen !== st.gen) return;
      store.applyAll(h.events || []);
    } catch (e) { if (gen === st.gen) notice(e.message, true); }
    schedule(true);
  }

  // `open` for the current chat (none = a new chat in the pool root): the server opens or
  // attaches the real Claude tab and returns the transcript in the reply (batch).
  async function openTab(gen) {
    st.opening = true; st.buffered = [];
    try {
      const params = { path: st.cwd, batch: true, after_seq: 0 };
      if (st.sid) params.session = st.sid;
      const res = await request('open', params, 120000);
      if (gen !== st.gen) return false;
      st.sid = res.session; st.isTerminal = !!res.terminal;
      applyFresh((res.events || []).concat(st.buffered.filter((e) => e.session === st.sid)));
      history.replaceState(null, '', '#' + st.sid);
      return true;
    } catch (e) {
      if (gen === st.gen) notice(e.message, true);
      return false;
    } finally { st.opening = false; st.buffered = []; schedule(true); }
  }

  async function attach(gen) {
    st.opening = true; st.buffered = [];
    try {
      const res = await request('attach', { session: st.sid, batch: true }, 60000);
      if (gen !== st.gen) return;
      applyFresh((res.events || []).concat(st.buffered.filter((e) => e.session === st.sid)));
    } catch (e) {
      if (gen === st.gen && e.code === 'not_live') { st.isTerminal = false; notice('This tab closed on ARES. Send a message to pick it up again.'); }
    } finally { st.opening = false; st.buffered = []; schedule(true); }
  }

  function applyFresh(events) {
    const pending = store.pendingUser, imgs = store.pendingImages;
    store.reset();
    if (pending !== null) store.beginSend(pending, imgs);
    store.applyAll(events);
  }

  // ---- sending ----------------------------------------------------------------------
  async function send() {
    const text = el.input.value.trim();
    const imgs = st.images;
    if ((!text && !imgs.length) || st.sending) return;
    el.input.value = ''; st.images = []; renderAttach(); autosize(); closeSkills(); hideNotice();
    st.sending = true;
    const isNew = !st.sid;
    store.beginSend(text, imgs.map((i) => i.url));
    schedule(true);
    let ok = false;
    try {
      if (!st.isTerminal && !(await openTab(st.gen))) throw null;
      const p = { session: st.sid, text };
      if (imgs.length) p.images = imgs.map((i) => ({ media_type: 'image/jpeg', data: i.data, thumb: i.thumb }));
      await request('tsend', p, 30000);
      ok = true;
    } catch (e) { if (e) notice(e.message, true); }
    st.sending = false;
    if (!ok) {
      store.cancelSend();
      if (!el.input.value) el.input.value = text;
      if (!st.images.length) st.images = imgs;
      renderAttach(); autosize();
    }
    if (ok && isNew) setTimeout(loadSessions, 2500);
    schedule(true);
  }

  function stop() { if (st.sid && st.isTerminal) key('Escape'); }
  function key(k) { if (st.sid) request('tkey', { session: st.sid, key: k }).catch((e) => notice(e.message, true)); }

  // ---- transcript -------------------------------------------------------------------
  const GLYPH = { read: '≡', edit: '✎', create: '+', run: '>_', search: '⌕', web: '◍', graph: '∴', agent: '◇', plan: '☰' };
  const glyph = (name) => GLYPH[S.kind(name)] || '•';

  function rowView(r) {
    if (r.kind === 'user') {
      const a = store.attachments.get(r.id);
      return { cls: 'm-user', html: imgsHtml(a) + esc(r.text) };
    }
    if (r.kind === 'assistant') return { cls: 'm-asst', html: MD.render(r.text) };
    if (r.kind === 'steps') {
      const open = st.expanded.has(r.id);
      const run = store.running && r.cards.some((c) => c.status === 'running');
      const first = r.cards.find((c) => c.name !== 'ToolSearch') || r.cards[0];
      const cards = r.cards.map((c) => '<div class="card ' + esc(c.status) + '"><span class="st">' + (c.status === 'running' ? '●' : c.status === 'error' ? '×' : '✓') + '</span>' +
        '<span class="nm">' + esc(S.displayName(c.name)) + '</span><span class="sm" title="' + esc(c.summary) + '">' + esc(S.shortPath(c.summary)) + '</span>' +
        (c.added || c.removed ? '<span class="df"><span class="a">+' + (c.added || 0) + '</span> <span class="r">−' + (c.removed || 0) + '</span></span>' : '') + '</div>').join('');
      const notes = r.notes.map((n) => '<div class="steps-note">' + MD.render(n.text) + '</div>').join('');
      return { cls: 'steps' + (open ? ' open' : '') + (run ? ' run' : ''),
        html: '<button type="button" class="steps-h" data-steps="' + esc(r.id) + '"><span class="ic">' + esc(glyph(first.name)) + '</span><span>' +
          esc(S.stepSummary(r.cards)) + '</span><span class="chev">›</span></button><div class="steps-b">' + notes + cards + '</div>' };
    }
    if (r.kind === 'permission') {
      const res = r.resolved ? (r.byTimeout ? 'Timed out' : r.allowed ? 'Allowed' : 'Denied') : 'Waiting for approval on the phone';
      return { cls: 'm-perm', html: '<div class="t">' + esc(r.tool) + '</div><pre>' + esc(r.detail) + '</pre><div class="res">' + res + '</div>' };
    }
    return { cls: 'm-err', html: esc(r.text) };
  }

  function imgsHtml(a) {
    if (!a || !a.count) return '';
    const shown = a.thumbs.slice(0, 4).map((u) => '<img alt="" src="' + esc(u) + '">').join('');
    const more = a.count > a.thumbs.length ? '<span class="more">' + a.count + ' img</span>' : '';
    return '<div class="m-imgs">' + shown + more + '</div>';
  }

  const nodes = new Map();
  function renderTranscript() {
    const want = S.rows(store.items).map((r) => Object.assign({ id: r.id }, rowView(r)));
    if (store.pendingUser !== null) {
      want.push({ id: '__pending', cls: 'm-user pending', html: imgsHtml({ thumbs: store.pendingImages, count: store.pendingImages.length }) + esc(store.pendingUser) });
    }
    if (!want.length) {
      want.push({ id: '__empty', cls: 'empty', html: st.sid ? '<div class="sub">Loading this chat…</div>'
        : '<img class="logo" src="/static/ares-mark.svg" alt=""><div class="big">What should ARES work on?</div><div class="sub">ARES finds the right project in the knowledge graph,<br>opens a Claude tab there and works like it does on your phone.</div>' });
    }
    const stick = nearBottom();
    const keep = new Set(want.map((w) => w.id));
    for (const [id, n] of nodes) if (!keep.has(id)) { n.el.remove(); nodes.delete(id); }
    let prev = null;
    for (const w of want) {
      let n = nodes.get(w.id);
      if (!n) { n = { el: document.createElement('div'), key: '' }; nodes.set(w.id, n); }
      const k = w.cls + '\u0001' + w.html;
      if (n.key !== k) { n.el.className = w.cls; n.el.innerHTML = w.html; n.key = k; }
      if (n.el.parentNode !== el.col || n.el.previousSibling !== prev) { if (prev) prev.after(n.el); else el.col.prepend(n.el); }
      prev = n.el;
    }
    if (stick || st.forceBottom) el.tx.scrollTop = el.tx.scrollHeight;
    st.forceBottom = false;
    updateJump();
  }

  function nearBottom() { return el.tx.scrollHeight - el.tx.scrollTop - el.tx.clientHeight < 80; }
  function updateJump() { el.jump.hidden = nearBottom(); }

  function renderChrome() {
    const r = st.row;
    el.title.textContent = (r && r.title) || S.title(store.items) || (st.sid ? 'Chat' : 'New chat');
    const proj = (r && r.project) || S.project(store.items);
    el.chip.hidden = !proj; el.chip.textContent = proj || '';
    const step = S.currentStep(store.items, store.running) || (store.pendingUser !== null ? 'Thinking' : null);
    el.working.hidden = !step; el.workingText.textContent = step || '';
    el.tprompt.hidden = !store.terminalPrompt; el.tpromptText.textContent = store.terminalPrompt || '';
    const empty = !el.input.value.trim() && !st.images.length;
    const stopMode = store.running && st.isTerminal && empty;
    el.send.classList.toggle('stop', stopMode);
    el.send.setAttribute('aria-label', stopMode ? 'Stop' : 'Send');
    el.send.disabled = !stopMode && (empty || st.sending || !st.connected);
    setLink();
  }

  function setLink() {
    let text = 'Offline', cls = 'bad';
    if (st.connected) {
      if (st.isTerminal) { text = 'Live tab'; cls = 'live'; }
      else if (st.sid) { text = 'Saved chat'; cls = 'ok'; }
      else { text = 'Ready'; cls = 'ok'; }
    } else if (!st.ws) text = 'Connecting';
    el.state.className = 'convo-state ' + cls; el.linkText.textContent = text;
  }

  let raf = 0;
  function schedule(bottom) {
    if (bottom) st.forceBottom = true;
    if (raf) return;
    raf = requestAnimationFrame(() => { raf = 0; renderTranscript(); renderChrome(); });
  }

  function notice(text, isErr) {
    el.notice.textContent = text || 'Something went wrong'; el.notice.className = 'notice' + (isErr ? ' err' : ''); el.notice.hidden = false;
    clearTimeout(st.noticeTimer); st.noticeTimer = setTimeout(hideNotice, 9000);
  }
  function hideNotice() { el.notice.hidden = true; }

  // ---- composer: images, skills, keys -----------------------------------------------
  function autosize() { el.input.style.height = 'auto'; el.input.style.height = Math.min(el.input.scrollHeight, window.innerHeight * 0.38) + 'px'; }

  async function encode(file, max, q) {
    const bmp = await createImageBitmap(file);
    const s = Math.min(1, max / Math.max(bmp.width, bmp.height));
    const c = document.createElement('canvas');
    c.width = Math.max(1, Math.round(bmp.width * s)); c.height = Math.max(1, Math.round(bmp.height * s));
    c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
    return c.toDataURL('image/jpeg', q);
  }

  async function addFiles(files) {
    for (const f of Array.from(files || []).slice(0, 8)) {
      if (!f.type.startsWith('image/')) continue;
      if (st.images.length >= 4) { notice('Up to 4 images per message.'); break; }
      try {
        const url = await encode(f, 2048, 0.86);
        const thumb = await encode(f, 240, 0.7);
        const data = url.split(',')[1];
        if (data.length * 0.75 > 5 * 1024 * 1024) { notice('That image is larger than 5 MB.', true); continue; }
        st.images.push({ url, data, thumb: thumb.split(',')[1] });
      } catch (e) { notice('Could not read that image.', true); }
    }
    renderAttach(); schedule();
  }

  function renderAttach() {
    el.attach.hidden = !st.images.length;
    el.attach.innerHTML = st.images.map((im, i) => '<div class="att"><img alt="" src="' + im.url + '"><button type="button" data-rm="' + i + '" aria-label="Remove">×</button></div>').join('');
  }

  async function openSkills() {
    if (!st.skills) { try { st.skills = (await request('skills')).skills || []; } catch (e) { notice(e.message, true); return; } }
    st.skillSel = 0; el.skills.hidden = false; $('show-skills').classList.add('on'); renderSkills();
  }
  function closeSkills() { el.skills.hidden = true; $('show-skills').classList.remove('on'); }
  function skillMatches() {
    const v = el.input.value;
    const q = v.startsWith('/') && !/\s/.test(v) ? v.slice(1).toLowerCase() : '';
    return (st.skills || []).filter((s) => !q || s.name.toLowerCase().includes(q)).slice(0, 60);
  }
  function renderSkills() {
    const list = skillMatches();
    st.skillSel = Math.min(st.skillSel, Math.max(0, list.length - 1));
    el.skillsList.innerHTML = list.length ? list.map((s, i) => '<button type="button" class="skill' + (i === st.skillSel ? ' sel' : '') + '" data-skill="' + esc(s.name) + '"><b>' +
      esc(s.name) + '</b><span>' + esc(s.description || '') + '</span></button>').join('') : '<div class="chats-empty">No skill matches.</div>';
  }
  function pickSkill(name) { el.input.value = name + ' '; closeSkills(); autosize(); el.input.focus(); schedule(); }

  el.form.addEventListener('submit', (e) => { e.preventDefault(); if (el.send.classList.contains('stop')) stop(); else send(); });
  el.input.addEventListener('input', () => {
    autosize();
    const v = el.input.value;
    if (v.startsWith('/') && !/\s/.test(v)) openSkills(); else if (!el.skills.hidden && !v.startsWith('/')) closeSkills();
    if (!el.skills.hidden) renderSkills();
    schedule();
  });
  el.input.addEventListener('keydown', (e) => {
    if (!el.skills.hidden) {
      const list = skillMatches();
      if (e.key === 'ArrowDown') { st.skillSel = Math.min(list.length - 1, st.skillSel + 1); renderSkills(); e.preventDefault(); return; }
      if (e.key === 'ArrowUp') { st.skillSel = Math.max(0, st.skillSel - 1); renderSkills(); e.preventDefault(); return; }
      if ((e.key === 'Enter' || e.key === 'Tab') && list[st.skillSel] && /^\/\S*$/.test(el.input.value)) { pickSkill(list[st.skillSel].name); e.preventDefault(); return; }
      if (e.key === 'Escape') { closeSkills(); e.preventDefault(); return; }
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); return; }
    if (e.key === 'Escape' && store.running) { stop(); e.preventDefault(); }
  });
  el.input.addEventListener('paste', (e) => {
    const files = Array.from(e.clipboardData ? e.clipboardData.files : []);
    if (files.length) { e.preventDefault(); addFiles(files); }
  });
  $('add-image').addEventListener('click', () => el.file.click());
  el.file.addEventListener('change', () => { addFiles(el.file.files); el.file.value = ''; });
  $('show-skills').addEventListener('click', () => { if (el.skills.hidden) openSkills(); else closeSkills(); el.input.focus(); });
  el.skillsList.addEventListener('click', (e) => { const b = e.target.closest('[data-skill]'); if (b) pickSkill(b.dataset.skill); });
  el.attach.addEventListener('click', (e) => { const b = e.target.closest('[data-rm]'); if (b) { st.images.splice(+b.dataset.rm, 1); renderAttach(); schedule(); } });
  el.tprompt.addEventListener('click', (e) => { const b = e.target.closest('[data-key]'); if (b) key(b.dataset.key); });
  el.col.addEventListener('click', (e) => {
    const b = e.target.closest('[data-steps]');
    if (!b) return;
    const id = b.dataset.steps;
    if (st.expanded.has(id)) st.expanded.delete(id); else st.expanded.add(id);
    schedule();
  });
  el.tx.addEventListener('scroll', updateJump, { passive: true });
  el.jump.addEventListener('click', () => { el.tx.scrollTo({ top: el.tx.scrollHeight, behavior: 'smooth' }); });

  let drags = 0;
  el.convo.addEventListener('dragenter', (e) => { if (e.dataTransfer && Array.from(e.dataTransfer.types).includes('Files')) { drags++; el.drop.hidden = false; e.preventDefault(); } });
  el.convo.addEventListener('dragover', (e) => { if (!el.drop.hidden) e.preventDefault(); });
  el.convo.addEventListener('dragleave', () => { drags = Math.max(0, drags - 1); if (!drags) el.drop.hidden = true; });
  el.convo.addEventListener('drop', (e) => { drags = 0; el.drop.hidden = true; if (e.dataTransfer && e.dataTransfer.files.length) { e.preventDefault(); addFiles(e.dataTransfer.files); } });

  // ---- chat list: open, shelve, drawer ----------------------------------------------
  el.list.addEventListener('click', (e) => {
    const b = e.target.closest('[data-sid]');
    if (!b) return;
    const row = st.sessions.find((s) => s.id === b.dataset.sid);
    if (row && row.id !== st.sid) select(row); else drawer(false);
  });
  el.list.addEventListener('contextmenu', (e) => {
    const b = e.target.closest('[data-sid]');
    const row = b && st.sessions.find((s) => s.id === b.dataset.sid);
    if (!row) return;
    e.preventDefault();
    const canShelve = row.live || row.shelved;
    el.ctx.innerHTML = '<button type="button" data-a="open">Continue</button>' +
      '<button type="button" data-a="shelve"' + (canShelve ? '' : ' disabled') + '>' + (row.shelved ? 'Remove from shelf' : 'Shelve') + '</button>' +
      '<button type="button" data-a="cancel">Cancel</button>';
    el.ctx.style.left = Math.min(e.clientX, innerWidth - 210) + 'px';
    el.ctx.style.top = Math.min(e.clientY, innerHeight - 140) + 'px';
    el.ctx.hidden = false; el.ctx.dataset.sid = row.id;
  });
  el.ctx.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-a]');
    if (!b || b.disabled) return;
    const row = st.sessions.find((s) => s.id === el.ctx.dataset.sid);
    el.ctx.hidden = true;
    if (!row) return;
    if (b.dataset.a === 'open') select(row);
    if (b.dataset.a === 'shelve') {
      try { await request('shelve', { session: row.id, shelved: !row.shelved }); } catch (err) { notice(err.message, true); }
      loadSessions();
    }
  });
  document.addEventListener('click', (e) => { if (!el.ctx.hidden && !el.ctx.contains(e.target)) el.ctx.hidden = true; });
  el.search.addEventListener('input', renderChats);
  $('new-chat').addEventListener('click', newChat);
  function drawer(open) { el.chats.classList.toggle('open', open); el.scrim.hidden = !open; }
  $('open-chats').addEventListener('click', () => drawer(true));
  el.scrim.addEventListener('click', () => drawer(false));
  document.addEventListener('visibilitychange', () => { if (!document.hidden && st.connected) loadSessions(); });
  setInterval(() => { if (st.connected && !document.hidden) loadSessions(); }, 15000);

  connect();
  schedule(true);
  el.input.focus();
})();
