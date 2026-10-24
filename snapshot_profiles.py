"""Save every screener stock's full Yahoo company details into stocklens.db (table company_profiles).

Why: Yahoo's company-metrics service refuses cloud servers like Render, so the hosted site can't fetch
the company description, beta, analyst price targets, margins, debt and so on. Running this on a home
connection (where Yahoo still answers) and pushing the updated database lets the hosted site show them.
Prices and price-based ratios stay live; see market.LIVE_FIELDS.

Run (about 5–10 minutes for ~517 stocks; requests are paced so Yahoo doesn't throttle them):
    .venv/bin/python snapshot_profiles.py            # skips stocks saved in the last 7 days
    .venv/bin/python snapshot_profiles.py --force    # re-download everything
Then commit stocklens.db and push. Render redeploys automatically.
"""

import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

import yfinance as yf

import db
import market
import universe

warnings.filterwarnings("ignore")
WORKERS = 2          # gentle: two requests at a time
PAUSE = 0.4          # seconds between requests per worker
MAX_AGE_DAYS = 7


def fetch(symbol, tries=4):
    for attempt in range(tries):
        try:
            info = yf.Ticker(symbol).info
        except Exception:
            info = {}
        if info and (info.get("regularMarketPrice") is not None or info.get("currentPrice") is not None):
            db.save_profile(symbol, {k: market.clean(info.get(k)) for k in market.INFO_KEYS})
            time.sleep(PAUSE)
            return True
        time.sleep(10 * (attempt + 1))   # empty answer usually means Yahoo is throttling: back off
    return False


if __name__ == "__main__":
    symbols = sorted(set(universe.all_symbols()) | {"GOOG"})
    if "--force" not in sys.argv:
        fresh = db.profiles_fresh(MAX_AGE_DAYS)
        symbols = [s for s in symbols if s not in fresh]
    print(f"Saving company details for {len(symbols)} stocks…", flush=True)
    ok, failed, start = 0, [], time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fetch, s): s for s in symbols}
        for i, fut in enumerate(as_completed(futures), 1):
            if fut.result():
                ok += 1
            else:
                failed.append(futures[fut])
            if i % 25 == 0 or i == len(symbols):
                print(f"  {i}/{len(symbols)} done · {ok} saved · {len(failed)} failed · {time.time() - start:.0f}s", flush=True)
    print(f"Finished: {ok} saved, {len(failed)} failed{': ' + ', '.join(failed) if failed else ''}")
    print("Next: commit stocklens.db and push so the hosted site gets them.")
