import { allocateBudget } from '../static/elite_alloc.js';
function eq(a, b, m) { if (a !== b) { console.error(`FAIL ${m}: ${a} !== ${b}`); process.exit(1); } }

// sums to budget exactly
const picks = [{llm_score:92},{llm_score:88},{llm_score:79},{llm_score:70},{llm_score:61}];
const a = allocateBudget(picks, 1000, {});
eq(a.reduce((s,x)=>s+x.amount,0), 1000, 'sum==budget');
// below-bar (61) unfunded, others funded
eq(a[4].funded, false, '61 below bar');
eq(a[0].funded, true, '92 funded');
// monotonic: higher score -> >= amount among funded
if (!(a[0].amount >= a[1].amount && a[1].amount >= a[2].amount)) { console.error('FAIL monotonic'); process.exit(1); }
// barPct is the absolute score
eq(a[0].barPct, 92, 'barPct==score');
// empty-funded edge: nothing clears BAR -> fund single top
const b = allocateBudget([{llm_score:10},{llm_score:5}], 500, {});
eq(b[0].funded, true, 'top funded when none clear');
eq(b[1].funded, false, 'second not funded');
eq(b[0].amount, 500, 'all budget to top');
// MAX_FUNDED cap
const many = Array.from({length:15}, () => ({llm_score:90}));
const c = allocateBudget(many, 1000, {});
eq(c.filter(x=>x.funded).length, 10, 'max 10 funded');
console.log('OK');
