"""Historical forward P/E and PEG from I/B/E/S (via WRDS).

For every month-end consensus date ("statpers") over the past 5 years:

    NTM EPS     = blend of FY1 and FY2 consensus mean EPS, weighted by time left in FY1
                  (w = days until FY1 ends ÷ 365; NTM = w·FY1 + (1−w)·FY2) — the standard
                  "next twelve months" forward EPS
    Forward P/E = I/B/E/S price ÷ NTM EPS
    Growth      = FY2 consensus ÷ FY1 consensus − 1   (expected next-year EPS growth)
    PEG         = forward P/E ÷ (growth × 100)        (only when growth > 0)

Prices and estimates both come from I/B/E/S's split-adjusted files, so they're on the same basis.
The analysts' long-term growth estimate (LTG) is also saved, but only 1–4 analysts provide it
per stock, so PEG uses the FY1→FY2 growth instead.

Connects straight to the WRDS PostgreSQL server with psycopg2 (the `wrds` package pins an old
pandas that crashes on Python 3.14). The password comes from ~/.pgpass, saved by wrds_login.py.

Run (WRDS may send a Duo push):
    .venv/bin/python ibes.py
I/B/E/S updates monthly, so refreshing once a month is enough.
"""

import math
import time

import pandas as pd

import db

YEARS = 5
WRDS_USERNAME = "lc2000111"
WRDS_HOST, WRDS_PORT, WRDS_DB = "wrds-pgdata.wharton.upenn.edu", 9737, "wrds"
MAX_PE = 500        # P/E above this means EPS is ~zero: not meaningful
MIN_GROWTH = 0.02   # PEG with growth under 2% explodes toward infinity: not meaningful
MIN_MONTHS = 24     # need at least 2 years of monthly history before ranking against it


def connect():
    """Open a WRDS connection; libpq reads the password from ~/.pgpass."""
    import psycopg2
    return psycopg2.connect(host=WRDS_HOST, port=WRDS_PORT, dbname=WRDS_DB, user=WRDS_USERNAME,
                            sslmode="require", connect_timeout=60)


def query(conn, sql, date_cols=()):
    with conn.cursor() as cur:
        cur.execute(sql)
        df = pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])
    for c in date_cols:
        df[c] = pd.to_datetime(df[c])
    for c in df.columns:
        if c not in date_cols and df[c].dtype == object and c not in ("ticker", "oftic", "fpi"):
            df[c] = pd.to_numeric(df[c], errors="coerce")  # Decimal -> float
    return df


def num(v, digits=4):
    return None if v is None or pd.isna(v) or (isinstance(v, float) and math.isinf(v)) else round(float(v), digits)


def fetch(symbols):
    """Pull consensus estimates + prices for all symbols in one WRDS session and save the monthly series."""
    start = (pd.Timestamp.today() - pd.DateOffset(years=YEARS, months=1)).strftime("%Y-%m-%d")
    conn = connect()
    try:
        # I/B/E/S writes share classes with a dot (BRK.B); Yahoo uses a dash (BRK-B)
        sym_list = "','".join(sym.replace("-", ".") for sym in symbols)
        # Map exchange tickers (oftic) to permanent I/B/E/S tickers, using the latest record
        mapping = query(conn, f"""
            select distinct on (oftic) oftic, ticker from ibes.statsum_epsus
            where oftic in ('{sym_list}') and statpers >= current_date - interval '2 years'
            order by oftic, statpers desc""")
        mapping["oftic"] = mapping["oftic"].str.replace(".", "-", regex=False)
        tickers = "','".join(mapping["ticker"])
        # Dollar estimates only: some foreign listings (e.g. Nebius, formerly Yandex) carry estimates
        # in other currencies, which would make price ÷ EPS meaningless. If a month has duplicate
        # records (e.g. ASML), keep the one with the most analysts.
        est = query(conn, f"""
            select distinct on (ticker, fpi, statpers) ticker, statpers, fpi, fpedats, meanest, numest
            from ibes.statsum_epsus
            where ticker in ('{tickers}') and fpi in ('0', '1', '2') and statpers >= '{start}'
              and (curcode = 'USD' or (fpi = '0' and curcode is null))
            order by ticker, fpi, statpers, numest desc nulls last""",
            date_cols=["statpers", "fpedats"])
        px = query(conn, f"""
            select distinct on (ticker, statpers) ticker, statpers, price from ibes.actpsum_epsus
            where ticker in ('{tickers}') and statpers >= '{start}' and curr_price = 'USD'
            order by ticker, statpers""", date_cols=["statpers"])
    finally:
        conn.close()

    out = {}
    for _, row in mapping.iterrows():
        sym, tk = row["oftic"], row["ticker"]
        e = est[est["ticker"] == tk]
        if e.empty:
            continue
        fy1 = e[e.fpi == "1"].set_index("statpers")
        fy2 = e[e.fpi == "2"].set_index("statpers")
        ltg = e[e.fpi == "0"].set_index("statpers")
        df = pd.DataFrame({
            "fy1": fy1["meanest"], "fy1End": fy1["fpedats"], "fy1N": fy1["numest"],
            "fy2": fy2["meanest"], "ltg": ltg["meanest"], "ltgN": ltg["numest"],
        })
        df["price"] = px[px["ticker"] == tk].set_index("statpers")["price"]
        df = df.sort_index().dropna(subset=["fy1"])
        out[sym] = {
            "ibesTicker": tk,
            "rows": [
                {"date": d.strftime("%Y-%m-%d"), "fy1": num(r.fy1), "fy2": num(r.fy2),
                 "fy1End": r.fy1End.strftime("%Y-%m-%d") if pd.notna(r.fy1End) else None,
                 "fy1N": num(r.fy1N, 0), "ltg": num(r.ltg), "ltgN": num(r.ltgN, 0), "price": num(r.price)}
                for d, r in df.iterrows()
            ],
        }

    db.save_ibes(out)
    missing = sorted(set(symbols) - set(out))
    return {"asOf": max((s["rows"][-1]["date"] for s in out.values() if s["rows"]), default=None),
            "symbols": out, "missing": missing}


def load_cache():
    """I/B/E/S history from the database (stocklens.db)."""
    return db.load_ibes()


def ntm_eps(fy1, fy2, fy1_end, on_date):
    """Next-twelve-months EPS: time-weighted blend of FY1 and FY2."""
    if fy1 is None:
        return None
    if fy2 is None or fy1_end is None:
        return fy1
    w = min(1.0, max(0.0, (pd.Timestamp(fy1_end) - pd.Timestamp(on_date)).days / 365))
    return w * fy1 + (1 - w) * fy2


def series(sym_data):
    """Monthly forward P/E, growth and PEG series from the cached I/B/E/S rows."""
    rows = []
    for r in sym_data["rows"]:
        ntm = ntm_eps(r["fy1"], r["fy2"], r["fy1End"], r["date"])
        pe = r["price"] / ntm if r["price"] and ntm and ntm > 0 else None
        pe = pe if pe is not None and pe <= MAX_PE else None
        g = r["fy2"] / r["fy1"] - 1 if r["fy1"] and r["fy2"] is not None and r["fy1"] > 0 else None
        peg = pe / (g * 100) if pe and g is not None and g >= MIN_GROWTH else None
        rows.append({"date": r["date"], "fwdPE": pe, "growth": g, "peg": peg})
    return pd.DataFrame(rows).set_index(pd.to_datetime([r["date"] for r in rows]))


def pct_rank(hist, now):
    hist = hist.dropna()
    if now is None or len(hist) < MIN_MONTHS:
        return None
    return round(float((hist <= now).mean() * 100), 1)


def current_metrics(symbol, price_today, splits_since=1.0, cache=None):
    """Today's forward P/E and PEG (today's price ÷ latest I/B/E/S consensus), ranked against the
    monthly 5-year history. `splits_since` = combined split ratio since the last I/B/E/S date."""
    cache = cache or load_cache()
    if not cache or symbol not in cache["symbols"] or not cache["symbols"][symbol]["rows"]:
        return {}
    data = cache["symbols"][symbol]
    s = series(data)
    s = s[s.index >= pd.Timestamp.today() - pd.DateOffset(years=YEARS)]
    last = data["rows"][-1]

    ntm = ntm_eps(last["fy1"], last["fy2"], last["fy1End"], pd.Timestamp.today())
    ntm = ntm / splits_since if ntm else ntm
    fwd_pe = price_today / ntm if ntm and ntm > 0 else None
    fwd_pe = fwd_pe if fwd_pe is not None and fwd_pe <= MAX_PE else None
    g = s["growth"].iloc[-1] if not s.empty else None
    peg = fwd_pe / (g * 100) if fwd_pe and g is not None and not pd.isna(g) and g >= MIN_GROWTH else None

    fwd_hist, peg_hist = s["fwdPE"].dropna(), s["peg"].dropna()
    short = len(fwd_hist) < MIN_MONTHS
    return {
        "fwdMonths": len(fwd_hist),
        "fwdNote": f"Only {len(fwd_hist)} months of I/B/E/S history (need {MIN_MONTHS}) — not ranked" if short else None,
        "ibesAsOf": last["date"],
        "fwdPE": num(fwd_pe, 2),
        "fwdPctile": pct_rank(fwd_hist, fwd_pe),
        "fwdMin": num(fwd_hist.min(), 2) if not fwd_hist.empty else None,
        "fwdMedian": num(fwd_hist.median(), 2) if not fwd_hist.empty else None,
        "fwdMax": num(fwd_hist.max(), 2) if not fwd_hist.empty else None,
        "fwdSpark": [num(v, 2) for v in s["fwdPE"]],
        "growth": num(g),
        "peg": num(peg, 2),
        "pegPctile": pct_rank(peg_hist, peg),
        "pegMin": num(peg_hist.min(), 2) if not peg_hist.empty else None,
        "pegMedian": num(peg_hist.median(), 2) if not peg_hist.empty else None,
        "pegMax": num(peg_hist.max(), 2) if not peg_hist.empty else None,
        "pegSpark": [num(v, 2) for v in s["peg"]],
        "ltg": last["ltg"], "ltgAnalysts": last["ltgN"], "fy1Analysts": last["fy1N"],
    }


if __name__ == "__main__":
    print("Connecting to WRDS (approve the Duo push if asked)…")
    import universe
    t = time.time()
    p = fetch(list(universe.all_symbols()))
    print(f"Saved {len(p['symbols'])} stocks to stocklens.db · I/B/E/S data as of {p['asOf']} · {time.time() - t:.0f}s")
    if p["missing"]:
        print(f"Not in the I/B/E/S US file ({len(p['missing'])}): {', '.join(p['missing'])}")
