// code-store.js — the Code chat's event reducer and row layout. A port of the ARES iPhone app's
// ChatStore.swift and ChatLayout.swift, so the web chat shows a conversation exactly the way the
// phone does. Pure (no DOM); tests/code_store.test.js runs it under node.
(function (root) {
  'use strict';
  const MAX_ITEMS = 20000;
  const POOL = '/mnt/nvme/PROMETHEUS/';

  function Store() { this.reset(); }

  Store.prototype.reset = function () {
    this.items = [];
    this.index = new Map();
    this.lastSeq = 0;
    this.running = false;
    this.pendingUser = null;
    this.pendingImages = [];
    this.terminalPrompt = null;
    this.stopped = false;
    this.attachments = new Map();
  };

  Store.prototype.upsert = function (item) {
    const at = this.index.get(item.id);
    if (at !== undefined && at < this.items.length) { this.items[at] = item; return; }
    if (this.items.length >= MAX_ITEMS) return;
    this.index.set(item.id, this.items.length);
    this.items.push(item);
  };

  Store.prototype.beginSend = function (text, thumbs) {
    this.pendingUser = text;
    this.pendingImages = thumbs || [];
    this.running = true;
  };

  Store.prototype.cancelSend = function () {
    this.pendingUser = null;
    this.pendingImages = [];
    this.running = false;
  };

  Store.prototype.applyAll = function (events) {
    for (const ev of (events || []).slice(0, MAX_ITEMS)) this.apply(ev);
  };

  Store.prototype.apply = function (ev) {
    if (!ev || typeof ev !== 'object') return;
    if (typeof ev.seq === 'number') {
      if (ev.seq <= this.lastSeq) return;
      this.lastSeq = ev.seq;
    }
    this.reduce(ev);
  };

  Store.prototype.reduce = function (ev) {
    switch (ev.type) {
      case 'text_delta': {
        if (!ev.msg_id) return;
        const at = this.index.get(ev.msg_id);
        const old = at !== undefined && this.items[at].kind === 'assistant' ? this.items[at].text : '';
        this.upsert({ kind: 'assistant', id: ev.msg_id, text: old + (ev.text || '') });
        return;
      }
      case 'message': {
        if (!ev.msg_id) return;
        if (ev.role === 'user') {
          let thumbs = (ev.images || []).slice(0, 8).map((b) => 'data:image/jpeg;base64,' + b);
          if (this.pendingUser !== null && this.pendingImages.length && this.pendingImages.length === Math.max(thumbs.length, ev.images_count || 0)) thumbs = this.pendingImages;
          const count = Math.max(thumbs.length, ev.images_count || 0);
          if (count) this.attachments.set(ev.msg_id, { thumbs, count });
          this.pendingUser = null;
          this.pendingImages = [];
          this.upsert({ kind: 'user', id: ev.msg_id, text: ev.text || '' });
        } else {
          this.upsert({ kind: 'assistant', id: ev.msg_id, text: ev.text || '' });
        }
        return;
      }
      case 'tool':
        if (!ev.tool_id) return;
        this.upsert({ kind: 'tool', id: 'tool-' + ev.tool_id, name: ev.name || 'Tool', summary: ev.summary || '',
          status: ev.status || 'running', added: ev.diff_stats ? ev.diff_stats.added : null,
          removed: ev.diff_stats ? ev.diff_stats.removed : null });
        return;
      case 'permission':
        if (!ev.request_id) return;
        this.upsert({ kind: 'permission', id: 'perm-' + ev.request_id, rid: ev.request_id, tool: ev.tool || 'Tool',
          detail: ev.detail || '', resolved: false, allowed: null, byTimeout: false });
        return;
      case 'permission_resolved': {
        const at = this.index.get('perm-' + ev.request_id);
        if (at === undefined) return;
        this.items[at] = Object.assign({}, this.items[at], { resolved: true, allowed: !!ev.allow, byTimeout: ev.by === 'timeout' });
        return;
      }
      case 'error':
        this.pendingUser = null;
        this.upsert({ kind: 'error', id: 'err-' + (ev.seq || this.items.length), text: ev.message || 'Something went wrong' });
        return;
      case 'status':
        // A new tab's backlog starts "idle": keep the working line while the sent message waits.
        if (ev.state === 'idle' && this.pendingUser !== null) return;
        this.running = ev.state === 'running';
        if (ev.state === 'stopped') { this.terminalPrompt = null; this.stopped = true; this.running = false; }
        return;
      case 'turn_done':
        this.running = false;
        return;
      case 'terminal_prompt': {
        const t = String(ev.text || '').trim();
        this.terminalPrompt = t || null;
        return;
      }
      default:
    }
  };

  // ---- layout (ChatLayout.swift) -------------------------------------------------------

  function finalReplies(items) {
    const out = new Set();
    let last = null;
    items.forEach((it, i) => {
      if (it.kind === 'user') { if (last !== null) out.add(last); last = null; }
      else if (it.kind === 'assistant') last = i;
    });
    if (last !== null) out.add(last);
    return out;
  }

  // Tool steps and the notes Claude wrote between them fold into one "steps" row; only a
  // turn's last assistant message shows as a full reply.
  function rows(items) {
    const finals = finalReplies(items);
    const out = [];
    let cards = [], notes = [], gid = null;
    const flush = () => {
      if (gid === null) return;
      if (!cards.length) notes.forEach((n) => out.push({ kind: 'assistant', id: n.id, text: n.text }));
      else out.push({ kind: 'steps', id: gid, cards, notes });
      cards = []; notes = []; gid = null;
    };
    items.forEach((it, i) => {
      if (it.kind === 'tool') { if (gid === null) gid = 'steps-' + it.id; cards.push(it); }
      else if (it.kind === 'assistant') {
        if (finals.has(i)) { flush(); out.push(it); }
        else { if (gid === null) gid = 'steps-' + it.id; notes.push({ id: it.id, text: it.text }); }
      } else { flush(); out.push(it); }
    });
    flush();
    return out;
  }

  function kind(name) {
    if (name.startsWith('mcp__homelab-kg__')) return 'graph';
    switch (name) {
      case 'Read': return 'read';
      case 'Edit': case 'MultiEdit': case 'NotebookEdit': return 'edit';
      case 'Write': return 'create';
      case 'Bash': case 'BashOutput': case 'KillShell': return 'run';
      case 'Grep': case 'Glob': case 'LS': return 'search';
      case 'WebFetch': case 'WebSearch': return 'web';
      case 'Task': case 'Agent': return 'agent';
      case 'TodoWrite': return 'plan';
      default: return 'other:' + displayName(name);
    }
  }

  function displayName(name) {
    if (!name.startsWith('mcp__')) return name;
    const last = name.split('__').pop();
    return last || name;
  }

  const fileName = (p) => String(p).split('/').filter(Boolean).pop() || String(p);
  const shortPath = (s) => (String(s).startsWith(POOL) ? String(s).slice(POOL.length) : String(s));

  function phrase(k, cards) {
    const n = cards.length;
    if (k === 'read' || k === 'edit' || k === 'create') {
      const verb = k === 'read' ? 'Read' : (k === 'edit' ? 'Edited' : 'Created');
      const files = [...new Set(cards.map((c) => c.summary))];
      return files.length === 1 ? verb + ' ' + fileName(files[0]) : verb + ' ' + files.length + ' files';
    }
    if (k === 'run') return n === 1 ? 'Ran a command' : 'Ran ' + n + ' commands';
    if (k === 'search') return n === 1 ? 'Searched code' : 'Ran ' + n + ' searches';
    if (k === 'web') return n === 1 ? 'Browsed the web' : 'Browsed ' + n + ' pages';
    if (k === 'graph') {
      const q = cards.find((c) => c.name.endsWith('kg_search') && !c.summary.startsWith('mcp__'));
      return q ? 'Searched ATLAS for “' + q.summary.slice(0, 40) + '”' : 'Searched ATLAS';
    }
    if (k === 'agent') return n === 1 ? 'Ran an agent' : 'Ran ' + n + ' agents';
    if (k === 'plan') return 'Updated the plan';
    const name = k.slice(6);
    return n === 1 ? 'Used ' + name : 'Used ' + name + ' ' + n + ' times';
  }

  function stepSummary(cards) {
    const visible = cards.filter((c) => c.name !== 'ToolSearch');
    if (!visible.length) return cards.length ? 'Loaded tools' : '';
    const order = [], groups = [];
    for (const c of visible.slice(0, 500)) {
      const k = kind(c.name);
      const at = order.indexOf(k);
      if (at >= 0) groups[at].push(c); else { order.push(k); groups.push([c]); }
    }
    return order.map((k, i) => phrase(k, groups[i])).join(' · ');
  }

  function runningPhrase(c) {
    const f = fileName(c.summary);
    const k = kind(c.name);
    const map = { read: 'Reading ' + f, edit: 'Editing ' + f, create: 'Creating ' + f, run: 'Running a command',
      search: 'Searching code', web: 'Browsing the web', graph: 'Searching ATLAS', agent: 'Running an agent',
      plan: 'Updating the plan' };
    return map[k] || 'Using ' + k.slice(6);
  }

  function currentStep(items, running) {
    if (!running) return null;
    const tail = items.slice(-200).reverse();
    if (tail.some((it) => it.kind === 'permission' && !it.resolved)) return 'Waiting for your approval';
    const t = tail.find((it) => it.kind === 'tool' && it.status === 'running');
    if (t) return runningPhrase(t);
    const last = items[items.length - 1];
    return last && last.kind === 'assistant' ? 'Writing' : 'Thinking';
  }

  function projectDir(path) {
    if (!path.startsWith(POOL)) return null;
    const c = path.slice(POOL.length).split('/').filter(Boolean);
    if (!c.length || c[0].startsWith('.')) return null;
    if (c[0] === 'PROJECTS') return c.length >= 2 && !c[1].startsWith('.') ? c[1] : null;
    if (c.length === 1 && c[0].includes('.')) return null;
    return c[0];
  }

  function project(items) {
    const counts = new Map(), last = new Map();
    let i = 0;
    for (const it of items) {
      if (it.kind !== 'tool') continue;
      const k = kind(it.name);
      const paths = (k === 'read' || k === 'edit' || k === 'create') ? [it.summary]
        : it.summary.split(/[\s"'`=;|&()<>,]+/).slice(0, 100);
      for (const p of paths) {
        const d = projectDir(p);
        if (!d) continue;
        counts.set(d, (counts.get(d) || 0) + 1); last.set(d, i++);
      }
    }
    let best = null;
    for (const [d, n] of counts) {
      if (!best || n > counts.get(best) || (n === counts.get(best) && last.get(d) > last.get(best))) best = d;
    }
    return best;
  }

  function title(items) {
    const u = items.find((it) => it.kind === 'user');
    if (!u) return null;
    const line = u.text.trim().split('\n')[0] || '';
    if (!line) return null;
    return line.length > 40 ? line.slice(0, 39) + '…' : line;
  }

  function ago(epoch, now) {
    if (!epoch) return '';
    const s = Math.max(0, Math.floor((now || Date.now() / 1000) - epoch));
    if (s < 60) return 'now';
    if (s < 3600) return Math.floor(s / 60) + 'm';
    if (s < 86400) return Math.floor(s / 3600) + 'h';
    if (s < 7 * 86400) return Math.floor(s / 86400) + 'd';
    return Math.floor(s / (7 * 86400)) + 'w';
  }

  // Live / Shelf / Today / Yesterday / This week / Earlier — HistoryDrawer.swift's sections.
  function sections(sessions, query, now) {
    const q = String(query || '').trim().toLowerCase();
    const list = (sessions || []).slice(0, 500).filter((s) => s.id)
      .filter((s) => !q || (s.title || '').toLowerCase().includes(q) || (s.project || '').toLowerCase().includes(q))
      .sort((a, b) => (b.updated || 0) - (a.updated || 0));
    const d = new Date((now || Date.now() / 1000) * 1000);
    const midnight = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 1000;
    const buckets = [['Live', []], ['Shelf', []], ['Today', []], ['Yesterday', []], ['This week', []], ['Earlier', []]];
    for (const s of list) {
      const t = s.updated || 0;
      const b = s.live ? 0 : s.shelved ? 1 : t >= midnight ? 2 : t >= midnight - 86400 ? 3 : t >= midnight - 6 * 86400 ? 4 : 5;
      buckets[b][1].push(s);
    }
    return buckets.filter((b) => b[1].length).map((b) => ({ name: b[0], rows: b[1] }));
  }

  const api = { Store, rows, stepSummary, runningPhrase, currentStep, kind, displayName, fileName,
    shortPath, project, projectDir, title, ago, sections, MAX_ITEMS };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.CodeStore = api;
})(typeof window !== 'undefined' ? window : globalThis);
