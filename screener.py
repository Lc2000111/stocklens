"""P/E percentile screener.

For each stock, rebuild a daily P/E history over the past 5 years:

    P/E on day d = closing price on d ÷ trailing-12-month EPS known on d

where trailing EPS = sum of the 4 most recently *reported* quarterly EPS figures
(from Yahoo's earnings history). Then rank today's P/E against that history:

    percentile = % of days in the past 5 years with a P/E at or below today's

90th percentile = the stock is more expensive (on earnings) than on 90% of days in 5 years.

Results are saved to the SQLite database (db.py), one snapshot per refresh.

Run standalone to print the table:
    .venv/bin/python screener.py
"""

import math
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import yfinance as yf

import db
import ibes
import universe

# yfinance still uses a pandas option that pandas 3.1 deprecates — harmless, so keep the console quiet
warnings.filterwarnings("ignore", message="The inplace keyword")

# Stocks to scan come from universe.py (Popular 50 + Nasdaq-100); results are saved in stocklens.db
UNIVERSE = universe.all_symbols()

YEARS = 5


def clean(v, digits=2):
    return None if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) else round(float(v), digits)


EARNINGS_MAX_AGE_DAYS = 7   # quarterly EPS changes at most once a quarter; refetch weekly
PRICE_BATCH = 100           # tickers per Yahoo price download
SCAN_TIME_LIMIT = 10 * 60   # seconds: a scan that runs longer than this stops itself


class Stopped(Exception):
    """Raised when a scan is stopped (Stop button or time limit). The previous snapshot is kept."""
NY = "America/New_York"


def fetch_earnings(symbol, tries=4):
    """Quarterly reported EPS from Yahoo → saved in the database.
    When Yahoo throttles it returns *empty* data rather than an error, so back off and retry."""
    for attempt in range(tries):
        try:
            ed = yf.Ticker(symbol).get_earnings_dates(limit=40)
        except Exception:
            ed = None
        if ed is not None and not ed.empty and "Reported EPS" in ed:
            eps = ed["Reported EPS"].dropna().sort_index()
            idx = eps.index.tz_convert(NY).tz_localize(None) if eps.index.tz is not None else eps.index
            db.save_earnings(symbol, [(d.strftime("%Y-%m-%d %H:%M:%S"), float(v)) for d, v in zip(idx, eps.values)])
            return True
        if attempt < tries - 1:
            time.sleep(5 * (attempt + 1))
    db.save_earnings(symbol, [])  # remember the miss so it's retried tomorrow, not on every refresh
    return False


def ttm_eps_series(rows):
    """Trailing-12-month EPS, effective from each earnings announcement onward. rows: [(date, eps)]."""
    if not rows:
        raise ValueError("No earnings history on Yahoo yet (new listing or spin-off; retried daily)")
    eps = pd.Series([e for _, e in rows], index=pd.to_datetime([d for d, _ in rows])).sort_index()
    eps = eps[~eps.index.normalize().duplicated(keep="last")]
    if len(eps) < 4:
        raise ValueError("Fewer than 4 quarters of earnings history on Yahoo (new listing or foreign filer), so no trailing P/E history")
    ttm = eps.rolling(4).sum()
    # Only trust a 4-quarter sum if those 4 reports span roughly 9 months (no missing quarters)
    span_days = pd.Series(eps.index, index=eps.index).diff(3).dt.days
    ttm[(span_days < 240) | (span_days > 310)] = float("nan")
    return ttm.dropna()


def download_prices(symbols, start, check=lambda step: None):
    """Daily closes + split records for many tickers in a few batched requests (instead of one per stock).
    `check(step)` is called before each batch so the scan can report progress or be stopped."""
    closes, splits = {}, {}
    n_batches = (len(symbols) + PRICE_BATCH - 1) // PRICE_BATCH
    for bi, i in enumerate(range(0, len(symbols), PRICE_BATCH), 1):
        batch = symbols[i:i + PRICE_BATCH]
        df = None
        for attempt in range(3):
            check(f"Downloading prices (batch {bi} of {n_batches})…")
            try:
                # auto_adjust=False: actual (split-adjusted) closes, not dividend-adjusted, to match reported EPS
                df = yf.download(batch, start=start, auto_adjust=False, actions=True, group_by="ticker",
                                 threads=True, progress=False, timeout=30)
            except Exception:
                df = None
            if df is not None and not df.empty:
                break
            time.sleep(5 * (attempt + 1))
        if df is None or df.empty:
            continue
        if df.index.tz is not None:
            df.index = df.index.tz_convert(NY).tz_localize(None)
        have = set(df.columns.get_level_values(0))
        for sym in batch:
            if sym not in have:
                continue
            sub = df[sym]
            c = sub["Close"].dropna()
            if not c.empty:
                closes[sym] = c
            if "Stock Splits" in sub:
                sp = sub["Stock Splits"].fillna(0)
                splits[sym] = sp[sp > 0]
    return closes, splits


def analyze(symbol, ibes_cache, close, splits, earnings_rows):
    ttm = ttm_eps_series(earnings_rows)
    start = pd.Timestamp.today().normalize() - pd.DateOffset(years=YEARS)
    if close is None or close.empty:
        raise ValueError("no price history")
    prices = close[close.index >= start]

    # Attach the latest TTM EPS known as of each trading day
    ttm.index = ttm.index.as_unit(prices.index.unit)
    df = pd.merge_asof(prices.rename("close").to_frame(), ttm.rename("ttm_eps").to_frame(),
                       left_index=True, right_index=True, direction="backward")
    df["pe"] = df["close"] / df["ttm_eps"]
    valid = df[df["ttm_eps"] > 0].dropna(subset=["pe"])

    latest = df.iloc[-1]
    result = {
        "symbol": symbol,
        "name": UNIVERSE.get(symbol, symbol),
        "price": clean(latest["close"]),
        "ttmEps": clean(latest["ttm_eps"]),
        "lastReport": ttm.index[-1].strftime("%Y-%m-%d"),
    }
    if not (latest["ttm_eps"] > 0) or valid.empty:
        result["note"] = "Negative or missing trailing EPS — P/E not meaningful"
        result.update(ibes_metrics(splits, symbol, latest["close"], ibes_cache))
        return result

    pe_now = latest["close"] / latest["ttm_eps"]
    hist = valid["pe"]
    weekly = hist.resample("W").last().dropna()
    result.update({
        "pe": clean(pe_now),
        "percentile": clean((hist <= pe_now).mean() * 100, 1),
        "peMin": clean(hist.min()),
        "peMedian": clean(hist.median()),
        "peMax": clean(hist.max()),
        "vsMedian": clean(pe_now / hist.median() - 1, 4),
        "years": clean((hist.index[-1] - hist.index[0]).days / 365.25, 1),
        "spark": [clean(v) for v in weekly.values],
    })
    result.update(ibes_metrics(splits, symbol, latest["close"], ibes_cache))
    notes = []
    if result["years"] < YEARS - 0.5:
        notes.append(f"Only {result['years']} years of positive-earnings history")
    if had_spinoff(splits, start - pd.DateOffset(years=1)):
        notes.append("Spin-off/restructuring in window — earnings before and after aren't comparable, so this P/E history may be distorted")
        result["unreliable"] = True
    if notes:
        result["note"] = " · ".join(notes)
    return result


def ibes_metrics(splits, symbol, price, cache):
    """Forward P/E and PEG today vs. their own 5-year monthly history (from I/B/E/S, see ibes.py)."""
    if not cache or symbol not in cache["symbols"]:
        return {}
    # If the stock split after the last I/B/E/S date, put the estimates on today's share basis
    as_of = pd.Timestamp(cache["symbols"][symbol]["rows"][-1]["date"])
    ratio = 1.0
    if splits is not None and not splits.empty:
        for r in splits[splits.index > as_of]:
            ratio *= r
    return ibes.current_metrics(symbol, float(price), splits_since=ratio, cache=cache)


def had_spinoff(splits, since):
    """Yahoo records spin-offs as fractional 'splits' (e.g. 1.061). Normal splits are whole ratios like 4:1 or 1:10."""
    if splits is None or splits.empty:
        return False
    for r in splits[splits.index >= since]:
        whole = abs(r - round(r)) < 0.01 if r >= 1 else abs(1 / r - round(1 / r)) < 0.01
        if not whole:
            return True
    return False


def fill_profiles(workers=4):
    """Look up sector/industry once for stocks the database doesn't have yet."""
    def one(sym):
        try:
            info = yf.Ticker(sym).info
            if info.get("sector"):
                db.set_profile(sym, None, info["sector"], info.get("industry"))  # keep our short display name
        except Exception:
            pass
    missing = db.stocks_missing_profile()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, missing))
    return len(missing)


def run_screen(symbols=None, progress=None, workers=3, should_stop=lambda: False):
    """Refresh data and save a snapshot to the database.
    `progress(done, total, step)` reports progress; `should_stop()` returning True stops the scan
    (raises Stopped) and keeps the previous snapshot. The scan also stops itself after SCAN_TIME_LIMIT.

    Steps: (1) sync stock lists, (2) download quarterly earnings only for stocks whose saved copy is
    over a week old, (3) download prices in batches of 100, (4) compute everything locally."""
    started = time.time()
    state = {"done": 0, "total": 0, "step": "Starting…"}

    def check(step=None):
        if step:
            state["step"] = step
        if should_stop():
            raise Stopped("Scan stopped. The previous snapshot is still shown.")
        if time.time() - started > SCAN_TIME_LIMIT:
            raise Stopped(f"Scan took longer than {SCAN_TIME_LIMIT // 60} minutes (Yahoo may be slow or throttling), "
                          "so it was stopped. The previous snapshot is still shown.")
        if progress:
            progress(state["done"], state["total"], state["step"])

    check("Updating stock lists…")
    db.sync_universe(universe.LISTS)
    fill_profiles()
    symbols = symbols or list(UNIVERSE)
    ibes_cache = ibes.load_cache()

    # Share classes that borrow another class's earnings don't need their own download
    earnings_syms = sorted({universe.EARNINGS_FROM.get(s, s) for s in symbols})
    stale = db.earnings_stale(earnings_syms, EARNINGS_MAX_AGE_DAYS)
    state["total"] = len(stale) + len(symbols)
    if stale:
        check(f"Downloading earnings for {len(stale)} stocks…")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for _ in as_completed([pool.submit(fetch_earnings, s) for s in stale]):
                state["done"] += 1
                check()

    start = (pd.Timestamp.today().normalize() - pd.DateOffset(years=YEARS + 1)).strftime("%Y-%m-%d")
    closes, splits = download_prices(symbols, start, check)
    if len(closes) < len(symbols) / 2:
        raise Stopped(f"Yahoo returned prices for only {len(closes)} of {len(symbols)} stocks (it may be throttling "
                      "this server), so nothing was saved. The previous snapshot is still shown.")
    earnings = db.load_earnings(earnings_syms)

    results = []
    check("Calculating percentiles…")
    for sym in symbols:
        try:
            src = universe.EARNINGS_FROM.get(sym, sym)
            r = analyze(sym, ibes_cache, closes.get(sym), splits.get(sym), earnings[src])
            if src != sym:
                r["note"] = " · ".join(filter(None, [r.get("note"), f"EPS taken from {src} (same company; Yahoo's {sym} EPS includes one-time items)"]))
            results.append(r)
        except Exception as e:
            results.append({"symbol": sym, "name": UNIVERSE.get(sym, sym), "error": str(e)[:200]})
        state["done"] += 1
        if state["done"] % 25 == 0:
            check()
    results.sort(key=lambda r: r.get("percentile") if r.get("percentile") is not None else -1, reverse=True)
    db.save_run(results, ibes_cache["asOf"] if ibes_cache else None)
    return db.latest_run()


def load_cache():
    """Latest saved snapshot from the database."""
    return db.latest_run()


if __name__ == "__main__":
    data = run_screen(progress=lambda d, n, step="": print(f"\r{d}/{n} {step:<45}", end="", flush=True))
    print(f"\n\n{'Ticker':<7}{'P/E':>8}{'Pctile':>8}{'5y min':>8}{'median':>8}{'5y max':>8}  note")
    for r in data["results"]:
        if "error" in r:
            print(f"{r['symbol']:<7}  error: {r['error']}")
            continue
        f = lambda k: f"{r[k]:>8.1f}" if r.get(k) is not None else f"{'—':>8}"
        print(f"{r['symbol']:<7}{f('pe')}{f('percentile')}{f('peMin')}{f('peMedian')}{f('peMax')}  {r.get('note', '')}")
