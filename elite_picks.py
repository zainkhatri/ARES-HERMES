"""Elite Picks engine — the stocks superinvestors & institutions are buying.

Standalone (no Flask / CLIP deps) so the weekly refresh cron can import it
cheaply. app.py imports compute_elite_picks(); the cron calls it with force=True.

Signals (data-only ranking, no LLM):
  • Superinvestor / hedge-fund 13F (Dataroma)  — always on, quarter-lagged.
  • 3-month price momentum (Yahoo v8 chart)    — best-effort; reported ON only
    if values actually resolve (this host's IP is sometimes 429'd by Yahoo).
  • Congress buys (QuiverQuant)                 — needs QUIVER_TOKEN.
  • Analyst price-target upside (Finnhub)        — needs FINNHUB_KEY.
Result is cached weekly in elite_picks.json; force=True recomputes.
"""
import os
import re
import json
import time
import collections
from concurrent.futures import ThreadPoolExecutor

import requests

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(APP_DIR, "elite_picks.json")
TTL = 7 * 24 * 3600            # one week
MAX_CANDIDATES = 25            # bounded fan-out
WORKERS = 4                    # bounded concurrency (gentle on Yahoo)
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# The marquee names — a Buy from one of these carries more weight than a random
# fund. Matched as case-insensitive substrings against Dataroma's firm names
# ("Bill Ackman - Pershing Square Capital Management", etc.). Heuristic, edit freely.
HEAVY_HITTERS = (
    "buffett", "munger", "ackman", "klarman", "tepper", "burry", "icahn",
    "loeb", "einhorn", "hohn", "li lu", "pabrai", "guy spier", "terry smith",
    "nygren", "howard marks", "oaktree", "berkowitz", "watsa", "gates",
    "druckenmiller", "valueact", "viking global", "lone pine", "tiger global",
    "baupost", "dodge & cox", "ruane cunniff", "greenberg", "cooperman",
    "peltz", "duan yongping", "greenblatt", "yacktman", "gayner", "russo",
)
HEAVY_WEIGHT = 3          # a heavy hitter counts as this many regular buyers


def _is_heavy(firm):
    f = firm.lower()
    return any(h in f for h in HEAVY_HITTERS)


def _dataroma_buys():
    """({ticker: {'name','buyers','firms'}}, quarter) — the funds that Bought/Added.

    Each Dataroma row is one superinvestor: cell[0] is the fund/manager name,
    cell[1] the quarter, the rest are per-ticker activity. We attribute every
    Buy/Add to that fund so the UI can show *who* is accumulating each name.
    """
    r = requests.get("https://www.dataroma.com/m/allact.php",
                     headers={"User-Agent": _UA, "Accept": "text/html"}, timeout=12)
    assert r.status_code == 200, f"dataroma http {r.status_code}"
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S)
    assert rows, "dataroma: no rows parsed"
    out, quarter = {}, ""
    for row in rows[:200]:                                   # bounded
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        if len(cells) < 3:
            continue
        firm = cells[0]
        if not firm or firm == "Portfolio Manager - Firm":  # header / blank
            continue
        for c in cells[1:]:
            if re.fullmatch(r"Q[1-4]\s+\d{4}", c):
                quarter = c
                continue
            if "\n" not in c:
                continue
            sym, tail = c.split("\n", 1)
            sym = sym.strip()
            if not re.fullmatch(r"[A-Z][A-Z.]{0,5}", sym):
                continue
            act = re.search(r"(Buy|Add [\d.]+%|Reduce [-\d.]+%|Sell [-\d.]+%|Sold Out)"
                            r"Change to portfolio", tail)
            if not act:
                continue
            name = tail[:act.start()].strip()
            e = out.setdefault(sym, {"name": name or sym, "buyers": 0, "firms": []})
            if name and e["name"] == sym:
                e["name"] = name
            if act.group(1) == "Buy" or act.group(1).startswith("Add"):
                e["buyers"] += 1
                if firm not in e["firms"]:
                    e["firms"].append(firm)
    return out, quarter


def _momentum_3mo(sym):
    """3-month price change %% from Yahoo, or None on failure."""
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            r = requests.get(f"https://{host}/v8/finance/chart/{sym}",
                             params={"interval": "1d", "range": "3mo"},
                             headers={"User-Agent": _UA}, timeout=6)
            if r.status_code != 200:
                continue
            cl = [x for x in r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if x]
            if len(cl) >= 2:
                return round((cl[-1] / cl[0] - 1) * 100, 1)
        except Exception:
            continue
    return None


def _congress_buys():
    """{ticker: purchase_count} from QuiverQuant, empty without QUIVER_TOKEN."""
    token = os.getenv("QUIVER_TOKEN", "")
    if not token:
        return {}
    try:
        r = requests.get("https://api.quiverquant.com/beta/live/congresstrading",
                         headers={"Authorization": f"Token {token}", "User-Agent": _UA},
                         timeout=12)
        if r.status_code != 200:
            return {}
        c = collections.Counter()
        for t in r.json()[:1000]:                            # bounded
            if str(t.get("Transaction", "")).lower().startswith("purchase"):
                sym = str(t.get("Ticker", "")).upper().strip()
                if sym:
                    c[sym] += 1
        return dict(c)
    except Exception:
        return {}


def _analyst_upside(sym):
    """Upside %% to mean analyst target (Finnhub), or None."""
    key = os.getenv("FINNHUB_KEY", "")
    if not key:
        return None
    try:
        r = requests.get("https://finnhub.io/api/v1/stock/price-target",
                         params={"symbol": sym, "token": key}, timeout=6)
        if r.status_code != 200:
            return None
        d = r.json()
        tgt, cur = d.get("targetMean"), d.get("lastPrice")
        if tgt and cur:
            return round((tgt / cur - 1) * 100, 1)
    except Exception:
        pass
    return None


def compute_elite_picks(force=False):
    """Ranked elite picks, cached weekly. force=True bypasses the cache."""
    if not force and os.path.exists(CACHE):
        try:
            cached = json.load(open(CACHE))
            if time.time() - cached.get("generated_ts", 0) < TTL:
                return cached
        except Exception:
            pass
    try:
        buys, quarter = _dataroma_buys()
    except Exception as e:
        if os.path.exists(CACHE):                            # serve stale over nothing
            return json.load(open(CACHE))
        return {"picks": [], "quarter": "", "sources": {}, "error": str(e)}

    congress = _congress_buys()
    ranked = sorted(buys.items(), key=lambda kv: kv[1]["buyers"], reverse=True)
    cands = [s for s, v in ranked if v["buyers"] >= 1][:MAX_CANDIDATES]

    # Circuit breaker: Yahoo 429s this host's IP intermittently. Probe once —
    # if it's down, skip momentum for every pick instead of eating 25 timeouts.
    mom_alive = bool(cands) and _momentum_3mo(cands[0]) is not None

    def enrich(sym):
        info = buys[sym]
        mom = _momentum_3mo(sym) if mom_alive else None
        up, cong = _analyst_upside(sym), congress.get(sym, 0)
        # tag + sort firms so the marquee names lead the line; weight the score
        firms = [{"name": f, "heavy": _is_heavy(f)} for f in info.get("firms", [])]
        firms.sort(key=lambda x: (not x["heavy"], x["name"]))
        heavies = sum(1 for f in firms if f["heavy"])
        weight = sum(HEAVY_WEIGHT if f["heavy"] else 1 for f in firms)  # conviction, weighted
        signals = ["superinvestor"]
        if heavies:
            signals.append("heavyweight")
        if cong:
            signals.append("congress")
        if up is not None and up > 0:
            signals.append("analyst")
        if mom is not None and mom > 0:
            signals.append("momentum")
        score = (weight + cong * 2
                 + (max(up, 0) * 0.2 if up is not None else 0)
                 + (max(mom, 0) * 0.1 if mom is not None else 0))
        return {"ticker": sym, "name": info["name"], "buyers": info["buyers"],
                "heavies": heavies, "firms": firms, "congress": cong, "momentum": mom,
                "upside": up, "signals": signals, "score": round(score, 1)}

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:     # bounded workers
        picks = list(ex.map(enrich, cands))
    picks.sort(key=lambda p: p["score"], reverse=True)
    for i, p in enumerate(picks):
        p["rank"] = i + 1

    # report a signal as ON only if it actually contributed data
    result = {"picks": picks[:20], "quarter": quarter, "generated_ts": time.time(),
              "sources": {"superinvestor": True,
                          "momentum": any(p["momentum"] is not None for p in picks),
                          "congress": bool(congress),
                          "analyst": any(p["upside"] is not None for p in picks)}}
    try:
        json.dump(result, open(CACHE, "w"))
    except Exception:
        pass
    return result


if __name__ == "__main__":
    r = compute_elite_picks(force=True)
    print(f"[elite-picks] {len(r.get('picks', []))} picks | {r.get('quarter')} "
          f"| sources={r.get('sources')} | err={r.get('error')}")
