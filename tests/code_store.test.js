// node tests/code_store.test.js — the web Code chat must group and label a conversation the
// same way the ARES iPhone app does (ChatLayout.swift / ChatStore.swift).
const assert = require('assert');
const S = require('../static/code-store.js');
let n = 0;
function test(name, fn) { fn(); n++; console.log('ok', name); }

const tool = (id, name, summary, status = 'done') => ({ type: 'tool', tool_id: id, name, summary, status });
const msg = (id, role, text, seq) => ({ type: 'message', msg_id: id, role, text, seq });

test('tool steps and in-between notes fold into one row; the last reply stays full', () => {
  const st = new S.Store();
  st.applyAll([msg('u1', 'user', 'fix it'), msg('a1', 'assistant', 'Looking'), tool('t1', 'Bash', 'ls'),
    tool('t2', 'Bash', 'pwd'), tool('t3', 'Read', '/mnt/nvme/PROMETHEUS/PROJECTS/X/app.py'), msg('a2', 'assistant', 'Done')]);
  const rows = S.rows(st.items);
  assert.deepStrictEqual(rows.map((r) => r.kind), ['user', 'steps', 'assistant']);
  assert.strictEqual(rows[1].notes.length, 1);
  assert.strictEqual(S.stepSummary(rows[1].cards), 'Ran 2 commands · Read app.py');
  assert.strictEqual(S.project(st.items), 'X');
});

test('text deltas append; replayed seqs are ignored', () => {
  const st = new S.Store();
  st.apply({ type: 'text_delta', msg_id: 'm', text: 'Hel', seq: 1 });
  st.apply({ type: 'text_delta', msg_id: 'm', text: 'lo', seq: 2 });
  st.apply({ type: 'text_delta', msg_id: 'm', text: 'lo', seq: 2 });
  assert.strictEqual(st.items[0].text, 'Hello');
});

test('a tool update replaces its card in place', () => {
  const st = new S.Store();
  st.apply(tool('t', 'Edit', 'a.py', 'running'));
  assert.strictEqual(S.currentStep(st.items, true), 'Editing a.py');
  st.apply(tool('t', 'Edit', 'a.py', 'done'));
  assert.strictEqual(st.items.length, 1);
  assert.strictEqual(st.items[0].status, 'done');
});

test('the optimistic echo keeps the working line until the server echoes it', () => {
  const st = new S.Store();
  st.beginSend('hi', []);
  st.apply({ type: 'status', state: 'idle' });
  assert.strictEqual(st.running, true);
  st.apply(msg('u', 'user', 'hi'));
  assert.strictEqual(st.pendingUser, null);
});

test('terminal prompt shows and clears; a stopped tab clears it', () => {
  const st = new S.Store();
  st.apply({ type: 'terminal_prompt', text: ' Allow?\n1. Yes ' });
  assert.strictEqual(st.terminalPrompt, 'Allow?\n1. Yes');
  st.apply({ type: 'status', state: 'stopped' });
  assert.strictEqual(st.terminalPrompt, null);
  assert.strictEqual(st.stopped, true);
});

test('ATLAS searches are named by their query', () => {
  assert.strictEqual(S.stepSummary([{ name: 'mcp__homelab-kg__kg_search', summary: 'dashboard terminal' }]),
    'Searched ATLAS for “dashboard terminal”');
  assert.strictEqual(S.stepSummary([{ name: 'ToolSearch', summary: 'x' }]), 'Loaded tools');
});

test('chat list sections: live, shelf, then by day', () => {
  const now = new Date(2026, 8, 28, 18, 0).getTime() / 1000;
  const secs = S.sections([
    { id: 'a', title: 'A', updated: now - 10, live: true },
    { id: 'b', title: 'B', updated: now - 20, shelved: true },
    { id: 'c', title: 'C', updated: now - 3600 },
    { id: 'd', title: 'D', updated: now - 30 * 3600 },
    { id: 'e', title: 'E', updated: now - 40 * 86400 },
  ], '', now);
  assert.deepStrictEqual(secs.map((s) => s.name), ['Live', 'Shelf', 'Today', 'Yesterday', 'Earlier']);
  assert.deepStrictEqual(S.sections([{ id: 'a', title: 'Alpha' }, { id: 'b', title: 'Beta', project: 'ARES' }], 'ares', now)
    .flatMap((s) => s.rows.map((r) => r.id)), ['b']);
});

test('ago', () => {
  assert.strictEqual(S.ago(1000 - 30, 1000), 'now');
  assert.strictEqual(S.ago(1000 - 7200, 1000), '2h');
});

const md = (() => { global.window = {}; require('../static/code-md.js'); return global.window.CodeMD; })();
test('markdown escapes html and renders code, lists and links', () => {
  const h = md.render('**hi** <script>x</script>\n\n- a `<b>`\n- b\n\n```js\nif (a<b) {}\n```\n[x](https://e.com) [bad](javascript:alert(1))');
  assert(!h.includes('<script>'));
  assert(h.includes('<strong>hi</strong>'));
  assert(h.includes('<ul><li>a <code>&lt;b&gt;</code></li><li>b</li></ul>'));
  assert(h.includes('if (a&lt;b) {}'));
  assert(h.includes('<a href="https://e.com"'));
  assert(!h.includes('href="javascript'));
});

test('markdown tables', () => {
  const h = md.render('| a | b |\n|---|---|\n| 1 | 2 |');
  assert(h.includes('<th>a</th>') && h.includes('<td>2</td>'));
});
console.log(n + ' passed');
