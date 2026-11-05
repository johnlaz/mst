#!/usr/bin/env python3
"""
Builds data/market.json for the NDX / S&P 500 / Dow terminal.

Keyless: prices come from Yahoo Finance via the `yfinance` package (run from a
GitHub Action, so browser CORS rules do not apply). Index membership comes from
Wikipedia. If Yahoo fails for the three index ETFs, it tries Stooq (keyless) and
then FMP (only if FMP_API_KEY is set as a repo secret).

Run locally:  python scripts/update_data.py
"""
import io
import json
import os
import sys
import time
import datetime as dt

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("NDX_OUT", os.path.join(HERE, "..", "data", "market.json"))

# ticker -> index config.  `eq` is the equal-weight ETF used for the
# "cap-weight vs equal-weight" reading (None = not applicable).
INDICES = {
    "QQQM": dict(label="Nasdaq-100", eq="QQQE",
                 wiki="https://en.wikipedia.org/wiki/Nasdaq-100", min_rows=90, max_rows=110),
    "SPY":  dict(label="S&P 500", eq="RSP",
                 wiki="https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", min_rows=480, max_rows=520),
    "DIA":  dict(label="Dow 30", eq=None,
                 wiki="https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average", min_rows=28, max_rows=32),
}
HISTORY_DAYS = 260
UA = {"User-Agent": "Mozilla/5.0 (compatible; ndx-terminal-bot; +https://github.com)"}


# ---------------------------------------------------------------- indicators
def sma(c, n):
    out = np.full(len(c), np.nan)
    if len(c) >= n:
        cs = np.cumsum(np.insert(c, 0, 0.0))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def rsi(c, n=14):
    out = np.full(len(c), np.nan)
    if len(c) <= n:
        return out
    d = np.diff(c)
    g = np.where(d > 0, d, 0.0)
    l = np.where(d < 0, -d, 0.0)
    ag, al = g[:n].mean(), l[:n].mean()
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n, len(d)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
        out[i + 1] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def ultimate_oscillator(h, l, c):
    """Larry Williams UO, periods 7/14/28 with weights 4/2/1."""
    n = len(c)
    out = np.full(n, np.nan)
    if n < 29:
        return out
    pc = np.roll(c, 1)
    lo, hi = np.minimum(l, pc), np.maximum(h, pc)
    bp, tr = c - lo, hi - lo
    bp[0] = tr[0] = 0.0
    cb, ct = np.cumsum(bp), np.cumsum(tr)
    idx = np.arange(28, n)

    def avg(k):
        sb, st = cb[idx] - cb[idx - k], ct[idx] - ct[idx - k]
        return np.divide(sb, st, out=np.zeros(len(idx)), where=st != 0)

    out[idx] = 100 * (4 * avg(7) + 2 * avg(14) + avg(28)) / 7
    return out


def r2(x):
    return None if x is None or not np.isfinite(x) else round(float(x), 2)


# ---------------------------------------------------------------- data fetch
def fetch_prices(tickers, period="2y", chunk=80):
    """Return {ticker: DataFrame[High, Low, Close]} using yfinance (keyless)."""
    import yfinance as yf
    out = {}
    tickers = list(tickers)
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        for attempt in range(3):
            try:
                df = yf.download(part, period=period, interval="1d", group_by="ticker",
                                 auto_adjust=False, progress=False, threads=True)
                break
            except Exception as e:  # noqa
                print(f"  yfinance chunk {i} attempt {attempt + 1} failed: {e}", file=sys.stderr)
                time.sleep(5 * (attempt + 1))
                df = None
        if df is None or df.empty:
            continue
        for t in part:
            try:
                sub = df[t][["High", "Low", "Close"]].dropna()
                if len(sub) > 30:
                    out[t] = sub
            except Exception:
                pass
        time.sleep(1.5)
    return out


def fallback_etf(sym):
    """Keyless Stooq, then FMP if a key is configured. Returns DataFrame or None."""
    try:
        r = requests.get(f"https://stooq.com/q/d/l/?s={sym.lower()}.us&i=d", headers=UA, timeout=30)
        df = pd.read_csv(io.StringIO(r.text))
        df = df.rename(columns=str.title).set_index("Date")
        df.index = pd.to_datetime(df.index)
        df = df[["High", "Low", "Close"]].dropna().tail(600)
        if len(df) > 100:
            print(f"  {sym}: using Stooq fallback")
            return df
    except Exception as e:  # noqa
        print(f"  {sym}: Stooq fallback failed: {e}", file=sys.stderr)
    key = os.environ.get("FMP_API_KEY")
    if key:
        try:
            r = requests.get("https://financialmodelingprep.com/stable/historical-price-eod/full",
                             params={"symbol": sym, "apikey": key}, timeout=30)
            data = r.json()
            data = data if isinstance(data, list) else data.get("historical", [])
            df = pd.DataFrame(data).rename(columns=str.title)
            df["Date"] = pd.to_datetime(df["Date"])
            df = df.set_index("Date").sort_index()[["High", "Low", "Close"]].dropna().tail(600)
            if len(df) > 100:
                print(f"  {sym}: using FMP fallback")
                return df
        except Exception as e:  # noqa
            print(f"  {sym}: FMP fallback failed: {e}", file=sys.stderr)
    return None


def fetch_market_caps(tickers, workers=8):
    """Keyless market caps via yfinance. {ticker: cap}. Fail-soft: missing tickers are just absent."""
    import yfinance as yf
    from concurrent.futures import ThreadPoolExecutor

    def one(t):
        try:
            v = yf.Ticker(t).fast_info["market_cap"]
            return t, float(v) if v else None
        except Exception:
            return t, None

    with ThreadPoolExecutor(workers) as ex:
        return {t: v for t, v in ex.map(one, tickers) if v}


def concentration(sym, cons, prices):
    """Top-10 / top-3 weight. Nasdaq-100: approximate (market-cap share; the real index caps some weights).
    Dow: price-weighted, so weight = price share. S&P 500: skipped (500 lookups is too fragile)."""
    tickers = [t for t, _, _ in cons]
    if sym == "DIA":
        w = {t: float(prices[t]["Close"].iloc[-1]) for t in tickers if t in prices}
        approx = False
    elif sym == "QQQM":
        w = fetch_market_caps(tickers)
        approx = True
    else:
        return {}
    if len(w) < 0.8 * len(tickers):
        return {}
    tot = sum(w.values())
    top = sorted(w.items(), key=lambda kv: kv[1], reverse=True)
    return dict(top10=r2(100 * sum(v for _, v in top[:10]) / tot), top3=r2(100 * sum(v for _, v in top[:3]) / tot),
                topNames=[t for t, _ in top[:5]], topApprox=approx)


# ---- historical "what happened after past signals" (price-only) ----
# Keep these in step with PRESETS in index.html: (oversold trigger, overbought trigger) on the Ultimate Oscillator.
PRESET_RULES = {"mild": (30, 70), "mod": (25, 75), "deep": (20, 80)}
HORIZONS = (21, 63, 126)  # ~1, 3, 6 months of trading days
STATS_SRC = {"QQQM": "QQQ", "SPY": "SPY", "DIA": "DIA"}  # longer-history twins


def _fwd(c, idxs):
    out = {}
    for h in HORIZONS:
        r = np.array([c[i + h] / c[i] - 1 for i in idxs if i + h < len(c)])
        out[str(h)] = dict(n=int(len(r)), med=r2(100 * np.median(r)) if len(r) else None,
                           pos=r2(100 * np.mean(r > 0)) if len(r) else None,
                           worst=r2(100 * r.min()) if len(r) else None)
    return out


def _episodes(mask, gap=21):
    idxs, last = [], -10 ** 9
    for i in np.where(mask)[0]:
        if i - last > gap:
            idxs.append(int(i))
        last = i
    return idxs


def signal_stats(df):
    h, l, c = (df[k].to_numpy(float) for k in ("High", "Low", "Close"))
    uo = ultimate_oscillator(h, l, c)
    valid = np.isfinite(uo)
    base = _fwd(c, list(np.where(valid)[0]))
    rules = {}
    for k, (o, b) in PRESET_RULES.items():
        eo, eb = _episodes(valid & (uo <= o)), _episodes(valid & (uo >= b))
        rules[k] = dict(os=dict(trig=o, episodes=len(eo), fwd=_fwd(c, eo)),
                        ob=dict(trig=b, episodes=len(eb), fwd=_fwd(c, eb)))
    return dict(**{"from": df.index[0].strftime("%Y-%m-%d"), "to": df.index[-1].strftime("%Y-%m-%d")},
                years=round(len(c) / 252, 1), baseline=base, rules=rules)


def wiki_constituents(cfg):
    """[(ticker, name, sector)] from the index's Wikipedia page."""
    html = requests.get(cfg["wiki"], headers=UA, timeout=30).text
    for t in pd.read_html(io.StringIO(html)):
        t.columns = [" ".join(map(str, c)).strip() if isinstance(c, tuple) else str(c).strip() for c in t.columns]
        low = {c.lower(): c for c in t.columns}
        tcol = next((low[k] for k in ("ticker", "symbol") if k in low), None)
        ncol = next((low[k] for k in ("company", "security") if k in low), None)
        scol = next((c for c in t.columns if "sector" in c.lower() or "industry" in c.lower()), None)
        if not tcol or not (cfg["min_rows"] <= len(t) <= cfg["max_rows"]):
            continue
        rows = []
        for _, r in t.iterrows():
            tk = str(r[tcol]).strip().upper().replace(".", "-")
            if tk and tk != "NAN":
                rows.append((tk, str(r[ncol]).strip() if ncol else "", str(r[scol]).strip() if scol else "Other"))
        if cfg["min_rows"] <= len(rows) <= cfg["max_rows"]:
            return rows
    raise ValueError("no suitable constituents table found")


# ---------------------------------------------------------------- builders
def series_for(df):
    h, l, c = (df[k].to_numpy(float) for k in ("High", "Low", "Close"))
    return dict(c=c, uo=ultimate_oscillator(h, l, c), rsi=rsi(c), s50=sma(c, 50), s200=sma(c, 200))


def build_index(sym, df, cons, prices, eq_df):
    s = series_for(df)
    dates = [d.strftime("%Y-%m-%d") for d in df.index]
    n = len(df)
    start = max(0, n - HISTORY_DAYS)
    history = [dict(d=dates[i], c=r2(s["c"][i]), uo=r2(s["uo"][i]), rsi=r2(s["rsi"][i]),
                    s50=r2(s["s50"][i]), s200=r2(s["s200"][i])) for i in range(start, n)]
    last = n - 1
    peak = float(np.max(s["c"][-252:]))

    watch, a50, a200, n50, n200 = [], 0, 0, 0, 0
    for tk, name, sector in cons:
        p = prices.get(tk)
        if p is None:
            continue
        cs = series_for(p)
        c = cs["c"][-1]
        v200 = (c / cs["s200"][-1] - 1) * 100 if np.isfinite(cs["s200"][-1]) else None
        if np.isfinite(cs["s50"][-1]):
            n50 += 1
            a50 += c > cs["s50"][-1]
        if np.isfinite(cs["s200"][-1]):
            n200 += 1
            a200 += c > cs["s200"][-1]
        watch.append(dict(ticker=tk, name=name, sector=sector, uo=r2(cs["uo"][-1]),
                          rsi=r2(cs["rsi"][-1]), vs200=r2(v200)))
    need = 0.6 * len(cons)
    br50 = round(100 * a50 / n50, 1) if n50 >= need else None
    br200 = round(100 * a200 / n200, 1) if n200 >= need else None

    spread = None
    if eq_df is not None and len(eq_df) > 22 and n > 22:
        cap_ret = df["Close"].iloc[-1] / df["Close"].iloc[-22] - 1
        eq_ret = eq_df["Close"].iloc[-1] / eq_df["Close"].iloc[-22] - 1
        spread = r2((cap_ret - eq_ret) * 100)

    index = dict(date=dates[last], price=r2(s["c"][last]), peak=r2(peak), uo=r2(s["uo"][last]),
                 rsi=r2(s["rsi"][last]), sma50=r2(s["s50"][last]), sma200=r2(s["s200"][last]),
                 br50=br50, br200=br200, spread=spread)
    return dict(index=index, history=history, watch=watch)


def main():
    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT))
        except Exception:
            prev = {}

    cons = {}
    for sym, cfg in INDICES.items():
        try:
            cons[sym] = wiki_constituents(cfg)
            print(f"{sym}: {len(cons[sym])} constituents from Wikipedia")
        except Exception as e:  # noqa
            old = prev.get("indices", {}).get(sym, {}).get("watch", [])
            cons[sym] = [(w["ticker"], w.get("name", ""), w.get("sector", "Other")) for w in old]
            print(f"{sym}: Wikipedia failed ({e}); reusing {len(cons[sym])} previous tickers", file=sys.stderr)

    etfs = set(INDICES) | {c["eq"] for c in INDICES.values() if c["eq"]}
    everything = sorted(etfs | {t for lst in cons.values() for t, _, _ in lst})
    print(f"Downloading {len(everything)} tickers…")
    prices = fetch_prices(everything)

    for sym in etfs:
        if sym not in prices:
            fb = fallback_etf(sym)
            if fb is not None:
                prices[sym] = fb

    result = {}
    for sym, cfg in INDICES.items():
        if sym not in prices:
            print(f"{sym}: no price data, keeping previous", file=sys.stderr)
            if sym in prev.get("indices", {}):
                result[sym] = prev["indices"][sym]
            continue
        result[sym] = build_index(sym, prices[sym], cons[sym], prices, prices.get(cfg["eq"]) if cfg["eq"] else None)
        try:
            result[sym]["index"].update(concentration(sym, cons[sym], prices))
        except Exception as e:  # noqa
            print(f"{sym}: concentration skipped ({e})", file=sys.stderr)
        print(f"{sym}: as of {result[sym]['index']['date']}, {len(result[sym]['watch'])} stocks, "
              f"breadth50={result[sym]['index']['br50']}")

    fresh = [v for k, v in result.items() if k in prices]
    if not fresh:
        print("No fresh data for any index; leaving file untouched.", file=sys.stderr)
        sys.exit(1)

    newest = max(v["index"]["date"] for v in fresh)
    old_newest = max((v["index"]["date"] for v in prev.get("indices", {}).values()), default="")
    if newest < old_newest:
        print("New data is older than existing data; leaving file untouched.", file=sys.stderr)
        sys.exit(1)

    # Long-history signal stats (3 extra downloads). Reuse the previous stats if this fails.
    stats = prev.get("stats", {})
    try:
        long = fetch_prices(sorted(set(STATS_SRC.values())), period="max")
        for sym, src in STATS_SRC.items():
            if src in long and len(long[src]) > 1000:
                stats[sym] = dict(src=src, **signal_stats(long[src]))
                print(f"{sym}: signal stats from {src}, {stats[sym]['years']} years")
    except Exception as e:  # noqa
        print(f"signal stats skipped ({e})", file=sys.stderr)

    payload = dict(generated=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   source="yfinance (Yahoo Finance) + Wikipedia constituents", indices=result, stats=stats)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"Wrote {OUT} ({os.path.getsize(OUT) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
