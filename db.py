"""SQLite database for StockLens — a single file, stocklens.db, next to this script.

Tables
  stocks          one row per company: name, sector, industry
  memberships     which lists each stock belongs to (popular, nasdaq100)
  screen_runs     one row per screener refresh (timestamp, I/B/E/S data date)
  screen_results  every stock's results for every run, so history builds up over time
  ibes_estimates  monthly I/B/E/S consensus + price per stock (from ibes.py)
  earnings        quarterly reported EPS per stock (from Yahoo), re-downloaded only when a week old
  earnings_status when each stock's earnings were last downloaded
  daily_attention the most-watched stocks for each market session (Lookup page "In focus")
  ai_usage        one row per AI request: agent, tokens, time, success (spending guard + tokenomics)
  company_profiles full Yahoo company details per stock, saved from a home connection (snapshot_profiles.py)
                  so the hosted site can show them even though Yahoo blocks that service for cloud servers

View
  latest_results  the newest snapshot joined with company info: the easy table to query

Inspect it with any SQLite tool, e.g.
    sqlite3 stocklens.db "select * from screen_runs"
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_FILE = Path(__file__).parent / "stocklens.db"

SCHEMA = """
create table if not exists stocks (
    symbol      text primary key,
    name        text not null,
    sector      text,
    industry    text,
    updated_at  text default (datetime('now', 'localtime'))
);
create table if not exists memberships (
    symbol      text not null references stocks(symbol),
    list_name   text not null,
    primary key (symbol, list_name)
);
create table if not exists screen_runs (
    run_id      integer primary key autoincrement,
    run_at      text not null default (datetime('now', 'localtime')),
    ibes_as_of  text,
    n_stocks    integer
);
create table if not exists screen_results (
    run_id      integer not null references screen_runs(run_id),
    symbol      text not null references stocks(symbol),
    price real, ttm_eps real, last_report text,
    pe real, percentile real, pe_min real, pe_median real, pe_max real, years real,
    fwd_pe real, fwd_pctile real, fwd_min real, fwd_median real, fwd_max real,
    peg real, peg_pctile real, peg_min real, peg_median real, peg_max real,
    growth real, ltg real, ltg_analysts real, fy1_analysts real, ibes_as_of text, fwd_months integer, fwd_note text,
    unreliable integer default 0, note text, error text,
    spark text, fwd_spark text, peg_spark text,          -- JSON arrays for the mini charts
    primary key (run_id, symbol)
);
create index if not exists idx_results_symbol on screen_results(symbol, run_id);
create table if not exists earnings (
    symbol      text not null,
    report_date text not null,        -- announcement time, New York local
    eps         real,
    primary key (symbol, report_date)
);
create table if not exists earnings_status (
    symbol      text primary key,
    fetched_at  text not null,
    n_quarters  integer
);
create view if not exists latest_results as
select r.symbol, s.name, s.sector, s.industry,
       (select group_concat(m.list_name) from memberships m where m.symbol = r.symbol) as lists,
       r.price, r.ttm_eps, r.last_report,
       r.pe, r.percentile as pe_pctile, r.pe_min, r.pe_median, r.pe_max,
       r.fwd_pe, r.fwd_pctile, r.fwd_min, r.fwd_median, r.fwd_max,
       r.peg, r.peg_pctile, r.peg_median,
       r.growth, r.ltg, r.ltg_analysts, r.fwd_months, r.unreliable, r.note, r.error, run.run_at
from screen_results r
join stocks s using (symbol)
join screen_runs run using (run_id)
where r.run_id = (select max(run_id) from screen_runs);
create table if not exists daily_attention (
    session     text not null,        -- trading day, YYYY-MM-DD
    rank        integer not null,
    symbol      text not null,
    name        text,
    score real, rel_volume real, change_pct real, dollar_volume real, mentions integer,
    computed_at text default (datetime('now', 'localtime')),
    primary key (session, rank)
);
create table if not exists ai_usage (
    id               integer primary key autoincrement,
    started_at       text not null default (datetime('now')),   -- UTC
    visitor          text not null,                              -- salted hash of the IP, never the IP itself
    agent            text not null,                              -- explain | brief | ask
    ok               integer,                                    -- null while running, then 1/0
    model            text,
    llm_calls        integer default 0,
    input_tokens     integer default 0,
    cached_tokens    integer default 0,
    output_tokens    integer default 0,
    reasoning_tokens integer default 0,
    ms               integer,
    detail           text
);
create index if not exists idx_ai_usage_time on ai_usage(started_at);
create table if not exists company_profiles (
    symbol      text primary key,
    fetched_at  text not null default (datetime('now', 'localtime')),
    info        text not null          -- JSON: the fields the Lookup page uses
);
create table if not exists ibes_estimates (
    symbol      text not null,
    ibes_ticker text not null,
    statpers    text not null,        -- I/B/E/S consensus date (monthly)
    fy1 real, fy2 real, fy1_end text, fy1_n real, ltg real, ltg_n real, price real,
    primary key (symbol, statpers)
);
"""

# result-dict key (used by the web page) -> screen_results column
RESULT_COLUMNS = {
    "price": "price", "ttmEps": "ttm_eps", "lastReport": "last_report",
    "pe": "pe", "percentile": "percentile", "peMin": "pe_min", "peMedian": "pe_median", "peMax": "pe_max", "years": "years",
    "fwdPE": "fwd_pe", "fwdPctile": "fwd_pctile", "fwdMin": "fwd_min", "fwdMedian": "fwd_median", "fwdMax": "fwd_max",
    "peg": "peg", "pegPctile": "peg_pctile", "pegMin": "peg_min", "pegMedian": "peg_median", "pegMax": "peg_max",
    "growth": "growth", "ltg": "ltg", "ltgAnalysts": "ltg_analysts", "fy1Analysts": "fy1_analysts", "ibesAsOf": "ibes_as_of",
    "fwdMonths": "fwd_months", "fwdNote": "fwd_note",
    "unreliable": "unreliable", "note": "note", "error": "error",
}
SPARK_COLUMNS = {"spark": "spark", "fwdSpark": "fwd_spark", "pegSpark": "peg_spark", "pctHistory": "pct_history"}


@contextmanager
def connect():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with connect() as c:
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("pragma table_info(screen_results)")}
        if "pct_history" not in cols:   # added later: weekly percentile over the past year (JSON)
            c.execute("alter table screen_results add column pct_history text")


# ---------- stock list ----------

def sync_universe(lists):
    """Make the stocks + memberships tables match {list_name: {symbol: name}}."""
    init()
    with connect() as c:
        for list_name, members in lists.items():
            for sym, name in members.items():
                c.execute("insert into stocks(symbol, name) values (?, ?) on conflict(symbol) do nothing", (sym, name))
            c.execute("delete from memberships where list_name = ?", (list_name,))
            c.executemany("insert into memberships(symbol, list_name) values (?, ?)",
                          [(s, list_name) for s in members])


def stocks_missing_profile():
    with connect() as c:
        return [r["symbol"] for r in c.execute("select symbol from stocks where sector is null")]


def set_profile(symbol, name, sector, industry):
    with connect() as c:
        c.execute("""update stocks set name = coalesce(?, name), sector = ?, industry = ?,
                     updated_at = datetime('now', 'localtime') where symbol = ?""", (name, sector, industry, symbol))


def memberships():
    with connect() as c:
        out = {}
        for r in c.execute("select symbol, list_name from memberships"):
            out.setdefault(r["symbol"], []).append(r["list_name"])
        return out


# ---------- screener runs ----------

def save_run(results, ibes_as_of):
    """Store one screener refresh. Returns the new run_id."""
    with connect() as c:
        run_id = c.execute("insert into screen_runs(ibes_as_of, n_stocks) values (?, ?)",
                           (ibes_as_of, len(results))).lastrowid
        cols = ["run_id", "symbol"] + list(RESULT_COLUMNS.values()) + list(SPARK_COLUMNS.values())
        rows = []
        for r in results:
            vals = [run_id, r["symbol"]]
            vals += [int(bool(r.get(k))) if k == "unreliable" else r.get(k) for k in RESULT_COLUMNS]
            vals += [json.dumps(r[k]) if r.get(k) is not None else None for k in SPARK_COLUMNS]
            rows.append(vals)
        c.executemany(f"insert into screen_results({', '.join(cols)}) values ({', '.join('?' * len(cols))})", rows)
        return run_id


def latest_run():
    """Most recent run in the same shape the web page expects (or None)."""
    init()
    with connect() as c:
        run = c.execute("select * from screen_runs order by run_id desc limit 1").fetchone()
        if not run:
            return None
        members = memberships()
        results = []
        for row in c.execute("""select r.*, s.name, s.sector, s.industry from screen_results r
                                join stocks s using (symbol) where run_id = ?""", (run["run_id"],)):
            r = {"symbol": row["symbol"], "name": row["name"], "sector": row["sector"], "industry": row["industry"],
                 "lists": members.get(row["symbol"], [])}
            for key, col in RESULT_COLUMNS.items():
                if row[col] is not None:
                    r[key] = bool(row[col]) if key == "unreliable" else row[col]
            for key, col in SPARK_COLUMNS.items():
                r[key] = json.loads(row[col]) if row[col] else None
            results.append(r)
        n_runs = c.execute("select count(*) from screen_runs").fetchone()[0]
    return {"runId": run["run_id"], "updated": run["run_at"][:16], "ibesAsOf": run["ibes_as_of"],
            "runs": n_runs, "results": results}


def latest_result(symbol):
    """One stock's row from the newest snapshot (or None)."""
    init()
    with connect() as c:
        row = c.execute("select * from latest_results where symbol = ?", (symbol,)).fetchone()
        return dict(row) if row else None


def valuation(symbol):
    """One stock's full row from the newest snapshot, including its chart series (or None)."""
    init()
    with connect() as c:
        run = c.execute("select run_id, run_at, ibes_as_of from screen_runs order by run_id desc limit 1").fetchone()
        if not run:
            return None
        row = c.execute("""select r.*, s.name, s.sector from screen_results r join stocks s using (symbol)
                           where r.run_id = ? and r.symbol = ?""", (run["run_id"], symbol)).fetchone()
    if not row:
        return None
    out = {"symbol": symbol, "name": row["name"], "sector": row["sector"], "snapshot": run["run_at"][:16],
           "ibesAsOf": run["ibes_as_of"]}
    for key, col in RESULT_COLUMNS.items():
        if row[col] is not None:
            out[key] = bool(row[col]) if key == "unreliable" else row[col]
    for key, col in SPARK_COLUMNS.items():
        out[key] = json.loads(row[col]) if row[col] else None
    return out


def percentile_history(symbol):
    """Every saved snapshot for one stock — how its percentiles moved over time."""
    with connect() as c:
        return [dict(r) for r in c.execute("""
            select s.run_at, r.pe, r.percentile, r.fwd_pe, r.fwd_pctile, r.peg, r.peg_pctile, r.price
            from screen_results r join screen_runs s using (run_id)
            where r.symbol = ? order by r.run_id""", (symbol,))]


# ---------- quarterly earnings (Yahoo) ----------

def earnings_stale(symbols, max_age_days):
    """Symbols whose saved earnings are missing or older than max_age_days."""
    init()
    with connect() as c:
        # Stocks with earnings: refetch weekly. Stocks Yahoo had nothing for (new listings): retry daily.
        fresh = {r[0] for r in c.execute(
            """select symbol from earnings_status
               where (n_quarters > 0 and fetched_at >= datetime('now', 'localtime', ?))
                  or (n_quarters = 0 and fetched_at >= datetime('now', 'localtime', '-1 day'))""",
            (f"-{max_age_days} days",))}
    return [s for s in symbols if s not in fresh]


def save_earnings(symbol, rows):
    """rows: [(report_date 'YYYY-MM-DD HH:MM:SS', eps), ...] — replaces that stock's saved earnings."""
    with connect() as c:
        c.execute("delete from earnings where symbol = ?", (symbol,))
        c.executemany("insert or replace into earnings(symbol, report_date, eps) values (?, ?, ?)",
                      [(symbol, d, e) for d, e in rows])
        c.execute("""insert into earnings_status(symbol, fetched_at, n_quarters)
                     values (?, datetime('now', 'localtime'), ?)
                     on conflict(symbol) do update set fetched_at = excluded.fetched_at, n_quarters = excluded.n_quarters""",
                  (symbol, len(rows)))


def load_earnings(symbols):
    """{symbol: [(report_date, eps), ...]} oldest first."""
    init()
    out = {s: [] for s in symbols}
    with connect() as c:
        for r in c.execute("select symbol, report_date, eps from earnings order by symbol, report_date"):
            if r["symbol"] in out:
                out[r["symbol"]].append((r["report_date"], r["eps"]))
    return out


# ---------- daily attention ----------

def save_attention(session, ranked):
    """Store (replace) the attention ranking for one trading session."""
    if not session or not ranked:
        return
    init()
    with connect() as c:
        c.execute("delete from daily_attention where session = ?", (session,))
        c.executemany("""insert into daily_attention(session, rank, symbol, name, score, rel_volume, change_pct,
                         dollar_volume, mentions) values (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                      [(session, i + 1, r["symbol"], r["name"], r["score"], r["relVolume"], r["changePct"],
                        r["dollarVolume"], r["mentions"]) for i, r in enumerate(ranked)])


# ---------- AI usage (spending guard) ----------

def ai_usage_start(visitor, agent):
    init()
    with connect() as c:
        return c.execute("insert into ai_usage(visitor, agent) values (?, ?)", (visitor, agent)).lastrowid


def ai_usage_finish(row_id, ok, model, llm_calls, input_tokens, cached_tokens, output_tokens, reasoning_tokens, ms, detail):
    with connect() as c:
        c.execute("""update ai_usage set ok = ?, model = ?, llm_calls = ?, input_tokens = ?, cached_tokens = ?,
                     output_tokens = ?, reasoning_tokens = ?, ms = ?, detail = ? where id = ?""",
                  (int(ok), model, llm_calls, input_tokens, cached_tokens, output_tokens, reasoning_tokens, ms, detail, row_id))


def ai_usage_summary(visitor):
    """Requests by this visitor in the last hour, and site-wide requests/tokens today (UTC)."""
    init()
    with connect() as c:
        mine = c.execute("select count(*) from ai_usage where visitor = ? and started_at >= datetime('now', '-1 hour')",
                         (visitor,)).fetchone()[0]
        calls, tokens = c.execute("""select count(*), coalesce(sum(input_tokens + output_tokens), 0) from ai_usage
                                     where started_at >= date('now')""").fetchone()
    return {"visitor_last_hour": mine, "calls_today": calls, "tokens_today": tokens}


# ---------- stock search (our own 516 stocks) ----------

_SUFFIXES = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "the",
             "holdings", "holding", "group", "sa", "nv", "ag", "class", "a", "b", "c"}


def _norm(text):
    """'Apple Inc.' -> 'apple' ; 'Johnson & Johnson' -> 'johnson johnson'."""
    words = "".join(ch.lower() if ch.isalnum() else " " for ch in (text or "")).split()
    kept = [w for w in words if w not in _SUFFIXES]
    return " ".join(kept or words)


def search_stocks(query, limit=6):
    """Match by ticker or company name (our short name or Yahoo's full name from the snapshot)."""
    init()
    q = _norm(query)
    if not q:
        return []
    up = query.strip().upper()
    with connect() as c:
        rows = c.execute("""select s.symbol, s.name, s.sector, p.info from stocks s
                            left join company_profiles p using (symbol)""").fetchall()
    scored = []
    for r in rows:
        full = (json.loads(r["info"]).get("longName") if r["info"] else None) or ""
        names = [_norm(r["name"]), _norm(full)]
        sym = r["symbol"]
        if sym == up:
            rank = 0                                   # exact ticker
        elif any(n == q for n in names):
            rank = 1                                   # exact company name
        elif any(n.startswith(q) for n in names) or sym.startswith(up):
            rank = 2                                   # starts with what was typed
        elif any(f" {q}" in f" {n}" for n in names):
            rank = 3                                   # a later word starts with it
        else:
            continue
        scored.append((rank, len(r["name"] or ""), {"symbol": sym, "name": full or r["name"], "type": "EQUITY",
                                                     "exchange": r["sector"] or ""}))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [x[2] for x in scored[:limit]]


# ---------- company profiles (snapshot of Yahoo company details) ----------

def save_profile(symbol, info):
    init()
    with connect() as c:
        c.execute("""insert into company_profiles(symbol, info) values (?, ?)
                     on conflict(symbol) do update set info = excluded.info, fetched_at = datetime('now', 'localtime')""",
                  (symbol, json.dumps(info, default=str)))


def load_profile(symbol):
    """(info_dict, fetched_at) or (None, None)."""
    init()
    with connect() as c:
        row = c.execute("select info, fetched_at from company_profiles where symbol = ?", (symbol,)).fetchone()
    return (json.loads(row["info"]), row["fetched_at"]) if row else (None, None)


def profiles_fresh(max_age_days):
    init()
    with connect() as c:
        return {r[0] for r in c.execute("select symbol from company_profiles where fetched_at >= datetime('now', 'localtime', ?)",
                                        (f"-{max_age_days} days",))}


# ---------- I/B/E/S ----------

def save_ibes(symbol_rows):
    """Replace the stored I/B/E/S history. symbol_rows: {symbol: {"ibesTicker": ..., "rows": [...]}}"""
    init()
    with connect() as c:
        c.execute("delete from ibes_estimates")
        c.executemany(
            """insert into ibes_estimates(symbol, ibes_ticker, statpers, fy1, fy2, fy1_end, fy1_n, ltg, ltg_n, price)
               values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(sym, d["ibesTicker"], r["date"], r["fy1"], r["fy2"], r["fy1End"], r["fy1N"], r["ltg"], r["ltgN"], r["price"])
             for sym, d in symbol_rows.items() for r in d["rows"]])


def load_ibes():
    """I/B/E/S history in the shape ibes.py uses: {"asOf", "symbols": {sym: {"ibesTicker", "rows"}}}."""
    init()
    with connect() as c:
        rows = c.execute("select * from ibes_estimates order by symbol, statpers").fetchall()
    if not rows:
        return None
    symbols = {}
    for r in rows:
        s = symbols.setdefault(r["symbol"], {"ibesTicker": r["ibes_ticker"], "rows": []})
        s["rows"].append({"date": r["statpers"], "fy1": r["fy1"], "fy2": r["fy2"], "fy1End": r["fy1_end"],
                          "fy1N": r["fy1_n"], "ltg": r["ltg"], "ltgN": r["ltg_n"], "price": r["price"]})
    return {"asOf": max(r["statpers"] for r in rows), "symbols": symbols}
