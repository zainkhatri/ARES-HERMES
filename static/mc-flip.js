// mc-flip.js — double-tap (or double-click) the home Thermals card: it turns around and shows
// the FOMER Minecraft server on EROS with a Start button. Double-tap again to turn it back.
// Talks to /api/mc/fomer (GET status, POST {action} with the X-ARES-Write CSRF header).
(function () {
  'use strict';
  const card = document.getElementById('panel-thermals');
  const back = document.getElementById('mc-back');
  if (!card || !back) return;
  const $ = (id) => document.getElementById(id);
  const go = $('mc-go'), stateEl = $('mc-state'), stateT = $('mc-state-t'), players = $('mc-players'), addr = $('mc-addr');
  let startedAt = 0, flipped = false, busy = false, info = null, poll = 0, confirmTimer = 0, lastTap = 0, turning = false;

  function flip() {
    if (turning) return;
    turning = true;
    card.classList.remove('mc-in');
    card.classList.add('mc-out');
    card.addEventListener('animationend', function half() {
      card.removeEventListener('animationend', half);
      flipped = !flipped;
      card.classList.toggle('mc-flipped', flipped);
      back.setAttribute('aria-hidden', String(!flipped));
      card.classList.remove('mc-out');
      card.classList.add('mc-in');
      card.addEventListener('animationend', () => { card.classList.remove('mc-in'); turning = false; }, { once: true });
      if (flipped) { refresh(); schedule(10000); } else { clearTimeout(poll); disarm(); }
    });
  }

  // Double-click on a desktop; two taps within 320 ms on a phone. Taps on the back's own
  // buttons never count toward a flip.
  card.addEventListener('dblclick', (e) => { if (!e.target.closest('button')) { e.preventDefault(); flip(); } });
  card.addEventListener('touchend', (e) => {
    if (e.target.closest('button')) return;
    const now = Date.now();
    if (now - lastTap < 320) { e.preventDefault(); lastTap = 0; flip(); } else lastTap = now;
  }, { passive: false });

  function schedule(ms) { clearTimeout(poll); if (flipped) poll = setTimeout(() => { refresh(); }, ms); }

  async function refresh() {
    try {
      const r = await fetch('/api/mc/fomer', { credentials: 'same-origin', cache: 'no-store' });
      info = r.ok ? await r.json() : { running: null };
    } catch (e) { info = { running: null }; }
    render();
    schedule(info && info.running && info.players && info.players.online < 0 ? 4000 : 10000);
  }

  function render() {
    const run = info && info.running;
    const p = (info && info.players) || { online: -1, max: 0, names: [] };
    // Forge takes ~1 min to load the world after Start; after that an unanswered ping only
    // means the server is busy, not that it is still starting.
    const booting = run && p.online < 0 && Date.now() - startedAt < 150000;
    if (info && info.address) addr.textContent = info.address;
    let label = 'Checking', cls = '';
    if (busy) { label = busy === 'start' ? 'Starting' : 'Stopping'; cls = 'busy'; }
    else if (run === null || !info) { label = 'Unreachable'; }
    else if (booting) { label = 'Starting'; cls = 'busy'; }
    else if (run) { label = 'Online'; cls = 'on'; }
    else label = 'Offline';
    stateEl.className = 'mc-state ' + cls; stateT.textContent = label;
    players.textContent = run && p.online >= 0
      ? p.online + ' / ' + p.max + ' players' + (p.names && p.names.length ? ' · ' + p.names.join(', ') : '')
      : booting ? 'Loading the world…' : run ? 'Players: busy, checking again' : run === false ? 'Server is off' : 'Could not reach EROS';
    if (!go.classList.contains('confirm')) {
      go.textContent = run ? 'Stop' : 'Start';
      go.classList.toggle('stop', !!run);
    }
    go.disabled = !!busy || !info || run === null;
  }

  function disarm() { clearTimeout(confirmTimer); go.classList.remove('confirm'); render(); }

  async function act(action) {
    busy = action; if (action === 'start') startedAt = Date.now(); disarm(); render();
    try {
      const r = await fetch('/api/mc/fomer', { method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-ARES-Write': '1' }, body: JSON.stringify({ action }) });
      info = r.ok ? await r.json() : info;
    } catch (e) { /* the next poll shows the real state */ }
    busy = false; render(); schedule(3000);
  }

  go.addEventListener('click', () => {
    if (busy || !info) return;
    if (!info.running) { act('start'); return; }
    // Stopping kicks whoever is playing: ask for a second tap within 3 s.
    if (!go.classList.contains('confirm')) {
      go.classList.add('confirm'); go.classList.remove('stop'); go.textContent = 'Confirm';
      confirmTimer = setTimeout(disarm, 3000);
      return;
    }
    act('stop');
  });

  addr.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(addr.textContent); addr.classList.add('copied'); setTimeout(() => addr.classList.remove('copied'), 1200); } catch (e) { /* no clipboard */ }
  });
})();
