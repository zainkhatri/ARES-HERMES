// code-md.js — small, safe Markdown → HTML for the Code chat (static/code-chat.js).
// Every piece of text is HTML-escaped first; only the tags built here are emitted.
// Covers what Claude writes: headings, paragraphs, lists, quotes, fenced code, tables,
// rules, bold, italic, strike, inline code and http(s) links.
(function () {
  'use strict';
  const MAX_LINES = 20000;

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // Inline: code spans are cut out first so their contents are never formatted.
  function inline(src) {
    const codes = [];
    let s = String(src).replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return '\u0000' + (codes.length - 1) + '\u0000'; });
    s = esc(s);
    s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, (_, t, u) => '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + t + '</a>');
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, (_, pre, u) => pre + '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + u + '</a>');
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>').replace(/__([^_\n]+)__/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, '$1<em>$2</em>').replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, '$1<em>$2</em>');
    s = s.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => '<code>' + esc(codes[+i]) + '</code>');
  }

  function isTableSep(line) { return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line); }
  function cells(line) {
    let t = line.trim();
    if (t.startsWith('|')) t = t.slice(1);
    if (t.endsWith('|')) t = t.slice(0, -1);
    return t.split('|').map((c) => c.trim());
  }

  function table(lines, i) {
    const head = cells(lines[i]);
    let j = i + 2;
    const rows = [];
    while (j < lines.length && lines[j].includes('|') && lines[j].trim()) { rows.push(cells(lines[j])); j++; }
    const th = head.map((c) => '<th>' + inline(c) + '</th>').join('');
    const tb = rows.map((r) => '<tr>' + head.map((_, k) => '<td>' + inline(r[k] || '') + '</td>').join('') + '</tr>').join('');
    return { html: '<div class="md-table"><table><thead><tr>' + th + '</tr></thead><tbody>' + tb + '</tbody></table></div>', next: j };
  }

  function list(lines, i) {
    const ordered = /^\s*\d+[.)]\s/.test(lines[i]);
    const re = ordered ? /^(\s*)\d+[.)]\s+(.*)$/ : /^(\s*)[-*+]\s+(.*)$/;
    const items = [];
    let j = i;
    while (j < lines.length) {
      const m = lines[j].match(re);
      if (m) { items.push(m[2]); j++; continue; }
      if (lines[j].trim() && /^\s{2,}\S/.test(lines[j]) && items.length) { items[items.length - 1] += '\n' + lines[j].trim(); j++; continue; }
      break;
    }
    const tag = ordered ? 'ol' : 'ul';
    const lis = items.map((t) => {
      const task = t.match(/^\[( |x|X)\]\s+(.*)$/s);
      if (task) return '<li class="task' + (task[1] === ' ' ? '' : ' done') + '">' + inline(task[2]).replace(/\n/g, '<br>') + '</li>';
      return '<li>' + inline(t).replace(/\n/g, '<br>') + '</li>';
    }).join('');
    return { html: '<' + tag + '>' + lis + '</' + tag + '>', next: j };
  }

  function render(src) {
    const lines = String(src || '').replace(/\r\n?/g, '\n').split('\n').slice(0, MAX_LINES);
    const out = [];
    let para = [];
    const flush = () => { if (para.length) { out.push('<p>' + para.map(inline).join('<br>') + '</p>'); para = []; } };
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      const fence = line.match(/^\s*(```+|~~~+)\s*([\w+#.-]*)/);
      if (fence) {
        flush();
        const body = [];
        let j = i + 1;
        while (j < lines.length && !lines[j].trim().startsWith(fence[1])) { body.push(lines[j]); j++; }
        const lang = fence[2] ? '<span class="md-lang">' + esc(fence[2]) + '</span>' : '';
        out.push('<div class="md-code">' + lang + '<pre><code>' + esc(body.join('\n')) + '</code></pre></div>');
        i = j + 1;
        continue;
      }
      if (!line.trim()) { flush(); i++; continue; }
      const h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) { flush(); const n = Math.min(4, h[1].length + 1); out.push('<h' + n + '>' + inline(h[2]) + '</h' + n + '>'); i++; continue; }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { flush(); out.push('<hr>'); i++; continue; }
      if (/^\s*>/.test(line)) {
        flush();
        const q = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) { q.push(lines[i].replace(/^\s*>\s?/, '')); i++; }
        out.push('<blockquote>' + render(q.join('\n')) + '</blockquote>');
        continue;
      }
      if (line.includes('|') && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        flush(); const t = table(lines, i); out.push(t.html); i = t.next; continue;
      }
      if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) { flush(); const l = list(lines, i); out.push(l.html); i = l.next; continue; }
      para.push(line);
      i++;
    }
    flush();
    return out.join('');
  }

  window.CodeMD = { render, esc, inline };
})();
