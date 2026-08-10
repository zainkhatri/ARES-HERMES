// Pure budget-allocation math for Elite Picks. Split `budget` across only the
// picks whose llm_score clears BAR, weighted by score^GAMMA. Used by
// breakdown.html and node tests. No DOM, no globals beyond the export.
function allocateBudget(picks, budget, opts) {
  const BAR = (opts && opts.BAR != null) ? opts.BAR : 65;
  const GAMMA = (opts && opts.GAMMA != null) ? opts.GAMMA : 2;
  const MAX_FUNDED = (opts && opts.MAX_FUNDED != null) ? opts.MAX_FUNDED : 10;
  console.assert(Array.isArray(picks), 'picks must be an array');
  console.assert(budget >= 0, 'budget must be >= 0');

  const scored = picks.map((p, i) => ({ i, score: Number(p.llm_score) || 0 }));
  let funded = scored.filter(s => s.score >= BAR)
                     .sort((a, b) => b.score - a.score)
                     .slice(0, MAX_FUNDED);
  if (funded.length === 0 && scored.length) {
    funded = [scored.slice().sort((a, b) => b.score - a.score)[0]];
  }
  const fundedIdx = new Set(funded.map(s => s.i));
  const wsum = funded.reduce((s, f) => s + Math.pow(f.score, GAMMA), 0) || 1;

  const amounts = picks.map(() => 0);
  funded.forEach(f => { amounts[f.i] = Math.round(budget * Math.pow(f.score, GAMMA) / wsum); });
  if (funded.length) {                       // push rounding remainder onto the top pick
    const top = funded[0].i;
    amounts[top] += budget - amounts.reduce((s, a) => s + a, 0);
  }
  return picks.map((p, i) => ({
    funded: fundedIdx.has(i),
    amount: amounts[i],
    barPct: Math.max(0, Math.min(100, Number(p.llm_score) || 0)),
  }));
}
if (typeof module !== 'undefined' && module.exports) module.exports = { allocateBudget };
if (typeof window !== 'undefined') window.allocateBudget = allocateBudget;
export { allocateBudget };
