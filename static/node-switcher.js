// Twin-brand node switcher: clones the existing brand block for the OTHER node,
// tucks it in right next to it in the top-bar. Click the inactive twin → SSO
// handoff via /api/sso/issue → you land on the other site already logged in.
// Set window.__NODE = 'ARES' or 'HERMES' in the page before loading.
(function () {
  const onAresLan = location.hostname === '192.168.20.213' || location.hostname === 'ares.local';
  const URLS = {
    ARES:  onAresLan ? 'http://192.168.20.213:8080/' : 'https://ares.tail3045df.ts.net/',
    HERMES: 'https://hermes.tail3045df.ts.net/',
  };
  const NODES = {
    HERMES: { letter: 'H', name: 'HERMES', tag: 'MID-NAS',  color: '#6495ed' },
    ARES:  { letter: 'A', name: 'ARES',  tag: 'SUPER-NAS', color: '#ef4444' },
  };

  const current = (window.__NODE ||
    (location.hostname === 'hermes.tail3045df.ts.net' || location.href.indexOf('100.100.29.36') !== -1 ? 'HERMES' : 'ARES')).toUpperCase();
  const other = current === 'HERMES' ? 'ARES' : 'HERMES';

  // Purge any prior incarnations of this switcher.
  const stale = document.getElementById('node-switcher');
  if (stale) stale.remove();
  document.querySelectorAll('.np-tabs, .nb-twin').forEach(el => el.remove());

  const style = document.createElement('style');
  style.textContent = `
    .nb-twin {
      position:relative;
      display:inline-flex; align-items:center; gap:14px;
      text-decoration:none; color:inherit;
      margin-right:32px; padding-right:28px;
      opacity:0.40;
      transition:opacity 0.3s cubic-bezier(0.16,1,0.3,1),
                 transform 0.3s cubic-bezier(0.16,1,0.3,1),
                 filter 0.3s ease;
      cursor:pointer;
      -webkit-tap-highlight-color:transparent;
    }
    .nb-twin::after {
      content:''; position:absolute;
      right:0; top:50%;
      width:1px; height:30px;
      transform:translateY(-50%);
      background:rgba(255,255,255,0.08);
      transition:background 0.3s ease, box-shadow 0.3s ease;
    }
    .nb-twin:hover {
      opacity:1;
      transform:translateX(-2px);
    }
    .nb-twin:hover::after {
      background:var(--nb-color);
      box-shadow:0 0 8px -1px var(--nb-color);
    }
    .nb-twin:focus-visible {
      opacity:1;
      outline:none;
    }
    .nb-twin.loading {
      pointer-events:none;
      animation:nb-flicker 0.7s steps(2) infinite;
    }
    @keyframes nb-flicker {
      0%, 100% { opacity:1; }
      50% { opacity:0.55; }
    }

    .nb-twin .nb-mark {
      position:relative;
      width:26px; height:26px;
      display:flex; align-items:center; justify-content:center;
      border:1px solid var(--nb-color);
      color:var(--nb-color);
      font-family:'Bricolage Grotesque', 'JetBrains Mono', sans-serif;
      font-weight:800; font-size:14px; line-height:1;
      transition:box-shadow 0.3s ease;
      background:rgba(4,7,13,0.4);
    }
    .nb-twin:hover .nb-mark {
      box-shadow:
        inset 0 0 12px -4px var(--nb-color),
        0 0 14px -3px var(--nb-color);
    }

    .nb-twin .nb-line { display:flex; align-items:baseline; gap:8px; min-width:0; }
    .nb-twin .nb-text {
      font-family:'Bricolage Grotesque', 'JetBrains Mono', sans-serif;
      font-weight:700; font-size:14px; letter-spacing:0.02em;
      color:var(--nb-color);
    }
    .nb-twin .nb-sep { color:rgba(255,255,255,0.22); font-size:11px; }
    .nb-twin .nb-tag {
      font-family:'JetBrains Mono', monospace; font-size:10px;
      color:rgba(255,255,255,0.45);
      letter-spacing:0.1em; text-transform:uppercase;
      transition:color 0.3s ease;
    }
    .nb-twin:hover .nb-tag { color:rgba(255,255,255,0.85); }

    @media (max-width:768px) {
      .nb-twin { margin-right:20px; padding-right:18px; gap:10px; }
      .nb-twin .nb-tag, .nb-twin .nb-sep { display:none; }
    }
    @media (max-width:430px) {
      .nb-twin { margin-right:14px; padding-right:14px; }
      .nb-twin .nb-line { display:none; }
    }
  `;
  document.head.appendChild(style);

  const info = NODES[other];
  const link = document.createElement('a');
  link.className = 'nb-twin';
  link.href = URLS[other];
  link.dataset.node = other;
  link.setAttribute('aria-label', 'Switch to ' + info.name);
  link.title = 'Open ' + info.name;
  link.style.setProperty('--nb-color', info.color);
  link.innerHTML =
    '<div class="nb-mark">' + info.letter + '</div>' +
    '<div class="nb-line">' +
      '<span class="nb-text">' + info.name + '</span>' +
      '<span class="nb-sep">/</span>' +
      '<span class="nb-tag">' + info.tag + '</span>' +
    '</div>';

  link.addEventListener('click', async function (e) {
    e.preventDefault();
    if (link.classList.contains('loading')) return;
    link.classList.add('loading');
    try {
      const r = await fetch('/api/sso/issue?for=' + encodeURIComponent(URLS[other]), { credentials: 'include' });
      if (r.ok) {
        const d = await r.json();
        if (d && d.redirect) { window.location.href = d.redirect; return; }
      }
    } catch (_) { /* fall through to plain redirect */ }
    window.location.href = URLS[other];
  });

  function mount() {
    const brand = document.querySelector('.brand');
    if (!brand || !brand.parentElement) return;
    brand.insertAdjacentElement('beforebegin', link);
  }

  if (document.body) mount();
  else document.addEventListener('DOMContentLoaded', mount);
})();
