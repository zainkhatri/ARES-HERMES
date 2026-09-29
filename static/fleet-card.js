// fleet-card.js — the home page's Fleet card (where Thermals was): EROS live vitals and ZEUS
// state. ZEUS sleeps between its 04:00 backup runs, so it shows storage, backup and SSD
// health instead of live load. Reads window.__FLEET, which pollFleet() refreshes every 15 s
// from /api/fleet (written by the host's ares-fleet.timer collector).
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  if (!$('fl-eros')) return;
  const SEGS = 10;
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function meter(label, pct, value, hot) {
    const p = Math.max(0, Math.min(100, +pct || 0));
    const on = Math.round(p / 100 * SEGS);
    let segs = '';
    for (let i = 0; i < SEGS; i++) segs += '<i' + (i < on ? ' class="on"' : '') + '></i>';
    const warn = hot != null && p >= hot ? ' hot' : '';
    return '<div class="fl-m' + warn + '"><div class="fl-line"><span class="fl-k">' + esc(label) + '</span><span class="fl-v">' + esc(value) + '</span></div><span class="fl-seg">' + segs + '</span></div>';
  }
  // A text stat in the meter's shape (empty track) so all three columns line up.
  const stat = (label, value, cls) => '<div class="fl-m fl-txt' + (cls ? ' ' + cls : '') + '"><div class="fl-line"><span class="fl-k">' + esc(label) + '</span><span class="fl-v">' + esc(value) + '</span></div><span class="fl-seg"><i></i></span></div>';

  function ago(epoch) {
    if (!epoch) return '—';
    const s = Math.max(0, Date.now() / 1000 - epoch);
    if (s < 3600) return Math.round(s / 60) + 'm';
    if (s < 86400 * 2) return Math.round(s / 3600) + 'h';
    return Math.round(s / 86400) + 'd';
  }
  function shortUp(u) {
    const m = String(u || '').match(/(\d+)\s*week.*?(\d+)\s*day|(\d+)\s*day|(\d+)\s*hour/);
    if (!m) return '';
    if (m[1]) return 'up ' + m[1] + 'w ' + m[2] + 'd';
    if (m[3]) return 'up ' + m[3] + 'd';
    return 'up ' + m[4] + 'h';
  }
  function state(id, cls, text) {
    const el = $(id);
    el.className = 'fl-st ' + cls;
    el.lastElementChild.textContent = text;
  }

  function render() {
    const d = window.__FLEET;
    if (!d || !d.ok) return;
    const e = d.eros || {}, g = e.gpu || {}, b = d.backups || {}, z = d.zeus || {}, st = z.store || {};
    $('fl-meta').textContent = d.stale ? 'stale · ' + Math.round((d.age_sec || 0) / 60) + 'm' : 'live';

    if (e.up) {
      const cs = e.containers || [];
      const run = cs.filter((c) => c.state === 'running').length;
      state('fl-eros-st', 'on', 'Online');
      $('fl-eros').title = ['EROS', shortUp(e.uptime), cs.length ? run + '/' + cs.length + ' services up' : ''].filter(Boolean).join(' · ');
      const mem = e.mem_total_gb ? e.mem_used_gb / e.mem_total_gb * 100 : 0;
      $('fl-eros-mets').innerHTML =
        meter('CPU', e.cpu_pct, (e.cpu_pct != null ? e.cpu_pct : '—') + '%', 90) +
        meter('MEM', mem, Math.round(mem) + '%', 92) +
        meter('GPU', g.temp != null ? (g.temp - 20) / 70 * 100 : 0, g.temp != null ? g.temp + '°' : '—', 80);
    } else {
      state('fl-eros-st', 'off', 'Offline');
      $('fl-eros').title = 'EROS is not answering';
      $('fl-eros-mets').innerHTML = stat('CPU', '—') + stat('MEM', '—') + stat('GPU', '—');
    }

    const awake = !!b.zeus_reachable;
    state('fl-zeus-st', awake ? 'on' : 'sleep', awake ? 'Awake' : 'Asleep');
    const disks = b.disks || [];
    const bad = disks.filter((x) => x.health && x.health !== 'PASSED').length;
    const backupOk = (b.ares_to_zeus || {}).state === 'OK';
    $('fl-zeus').title = awake ? 'ZEUS is awake (backup window)' : 'ZEUS sleeps between backups; it wakes at 04:00';
    $('fl-zeus-mets').innerHTML =
      meter('STORE', st.pct, st.pct != null ? st.pct + '%' : '—', 90) +
      stat('BACKUP', ago((b.ares_to_zeus || {}).epoch), backupOk ? '' : 'bad') +
      stat('SSD', disks.length ? (bad ? bad + ' failing' : disks.length + ' ok') : '—', bad ? 'bad' : '');
  }

  render();
  setInterval(render, 5000);
})();
