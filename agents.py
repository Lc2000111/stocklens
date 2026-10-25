"""AI agents for StockLens — gpt-6-luna via Portkey (PORTKEY_API_KEY from .env; never printed).

1. explain_stock(symbol, compact)   Stock explainer: plain-English summary of valuation, growth, trend and
                                    risks. Full version on the Stock page; compact (~120 words) version in
                                    the screener's expandable rows.
2. ask(question)                    Ask the database: a tool-calling agent that writes read-only SQL
                                    against stocklens.db, runs it, and answers with the results.

Both only use data passed to them or read from the database, and are told not to give buy/sell advice.

Try from the command line:
    .venv/bin/python agents.py explain AAPL
    .venv/bin/python agents.py brief AAPL
    .venv/bin/python agents.py ask "Which tech stocks are high on all 3 measures?"
"""

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd

import db
import market

MODEL = "gpt-6-luna"
HERE = Path(__file__).parent
_client = None

GUARDRAILS = (
    "This is an educational tool for an MBA class. Use only the data you are given or retrieve; "
    "never invent numbers. Cite the specific figures behind each point. Do not tell the user to buy, "
    "sell or hold anything, and do not give personalized investment advice. Note data limitations when "
    "they matter (e.g. estimates are analyst forecasts; 'expensive vs. its own history' is not the same "
    "as overvalued). Write in clear, plain English for a business audience, formatted in Markdown."
)


def client():
    """OpenAI client routed through Portkey. Reads the key from this folder's .env, else the class folder's."""
    global _client
    if _client is None:
        from dotenv import load_dotenv
        from openai import OpenAI
        for env in (HERE / ".env", HERE.parent / ".env"):
            if env.exists():
                load_dotenv(env)
                break
        key = os.environ.get("PORTKEY_API_KEY")
        if not key:
            raise RuntimeError("PORTKEY_API_KEY not found — add it to a .env file in this folder or the class folder.")
        _client = OpenAI(api_key=key, base_url="https://api.portkey.ai/v1",
                         default_headers={"x-portkey-api-key": key})
    return _client


def _add_usage(total, r):
    """Accumulate token usage from one Responses API call (input, cached input, output, reasoning)."""
    u = getattr(r, "usage", None)
    total["llm_calls"] = total.get("llm_calls", 0) + 1
    if u:
        total["input_tokens"] = total.get("input_tokens", 0) + (u.input_tokens or 0)
        total["output_tokens"] = total.get("output_tokens", 0) + (u.output_tokens or 0)
        det_in, det_out = getattr(u, "input_tokens_details", None), getattr(u, "output_tokens_details", None)
        total["cached_tokens"] = total.get("cached_tokens", 0) + ((det_in.cached_tokens or 0) if det_in else 0)
        total["reasoning_tokens"] = total.get("reasoning_tokens", 0) + ((det_out.reasoning_tokens or 0) if det_out else 0)
    return total


def _respond(instructions, payload):
    r = client().responses.create(model=MODEL, instructions=instructions, input=payload)
    return r.output_text, r.model, _add_usage({}, r)


def _round(v, d=2):
    return round(v, d) if isinstance(v, (int, float)) and v is not None else v


# ---------------------------------------------------------------------------------------------
# 1. Stock explainer
# ---------------------------------------------------------------------------------------------

EXPLAIN_INSTRUCTIONS = GUARDRAILS + """

You are explaining one stock to a manager who is not a finance specialist. Structure:
## Snapshot: one or two sentences on what the company does and how big it is.
## Valuation: P/E (trailing and forward) and other multiples. If 5-year percentile data is provided,
   say where today sits versus the stock's own history and whether trailing, forward and PEG agree.
## Growth & profitability: revenue/earnings growth, margins, returns.
## Trend & momentum: price vs the 50- and 200-day moving averages, RSI, 52-week range.
## Things to watch: 2–4 risks or open questions the numbers raise.
## Bottom line: a neutral, balanced 2–3 sentence summary (no recommendation).
Keep it under 350 words."""

BRIEF_INSTRUCTIONS = GUARDRAILS + """

Write a SHORT summary of one stock for a pop-out panel in a stock screener. Exactly four bullets, no headings,
each starting with a bold label, about 120 words total:
- **Valuation:** where trailing P/E, forward P/E and PEG sit versus the stock's own 5-year history (percentiles)
  and whether they agree.
- **Why:** the main driver: compare the 1-year price change with the 1-year trailing-EPS change, and mention
  expected growth (e.g. "price +36% vs EPS +11%: the multiple expanded").
- **Fundamentals:** growth and margins in one line.
- **Watch:** the single most important risk or open question."""


def _one_year_changes(symbol, pts):
    """1-year price change (from the chart data) and trailing-EPS change (from saved earnings)."""
    import screener
    import universe
    price_chg = eps_chg = None
    if len(pts) > 1 and pts[0]["close"]:
        price_chg = pts[-1]["close"] / pts[0]["close"] - 1
    try:
        src = universe.EARNINGS_FROM.get(symbol, symbol)
        ttm = screener.ttm_eps_series(db.load_earnings([src])[src])
        before = ttm[ttm.index <= pd.Timestamp.today().normalize() - pd.DateOffset(years=1)]
        if len(before) and before.iloc[-1] > 0:
            eps_chg = ttm.iloc[-1] / before.iloc[-1] - 1
    except Exception:
        pass
    return _round(price_chg, 4), _round(eps_chg, 4)


def explain_stock(symbol, compact=False):
    symbol = symbol.upper().strip()
    data, status = market.stock_data(symbol)
    if status != 200:
        raise ValueError(data.get("error", f"No data for {symbol}"))
    hist, _ = market.history_data(symbol, "1y")
    pts = hist.get("points") or []
    latest = hist.get("latest") or {}
    price_chg, eps_chg = _one_year_changes(symbol, pts)
    trend = {
        "price": latest.get("close"), "sma50": latest.get("sma50"), "sma200": latest.get("sma200"),
        "rsi14": latest.get("rsi"), "priceChange1y": price_chg, "trailingEpsChange1y": eps_chg,
    }
    summary = (data.get("longBusinessSummary") or "")[:700]
    facts = {k: v for k, v in data.items() if k not in ("longBusinessSummary", "website") and v is not None}
    facts["businessSummary"] = summary
    facts["annual"] = facts.get("annual", [])[-4:]
    # 5-year percentile context from the screener. Its P/Es use ADJUSTED EPS, so label them explicitly —
    # otherwise the model may pair Yahoo's GAAP P/E (companyMetrics) with the screener's percentile.
    row = db.latest_result(symbol)
    screen = None
    if row:
        names = {"pe": "trailingPE_adjusted", "pe_pctile": "trailingPE_percentile", "pe_median": "trailingPE_5yMedian",
                 "fwd_pe": "forwardPE_nextTwelveMonths", "fwd_pctile": "forwardPE_percentile", "fwd_median": "forwardPE_5yMedian",
                 "peg": "PEG", "peg_pctile": "PEG_percentile", "growth": "expectedNextYearEpsGrowth", "note": "dataNote"}
        screen = {new: row[old] for old, new in names.items() if row.get(old) is not None}
        if compact:  # the brief only uses the screener's numbers, so there's no second P/E to confuse
            for k in ("trailingPE", "forwardPE", "trailingPegRatio", "trailingEps", "forwardEps", "epsCurrentYear"):
                facts.pop(k, None)
    payload = json.dumps({
        "symbol": symbol,
        "notes": "dividendYield is already in percent (0.4 = 0.4%); other ratios like margins/growth are fractions "
                 "(0.25 = 25%); debtToEquity is in percent (78 = 0.78x). Percentiles are 0–100 vs the stock's own "
                 "past 5 years. IMPORTANT: whenever you cite a percentile, cite the P/E from fiveYearValuationContext "
                 "next to it (same adjusted-EPS basis). companyMetrics.trailingPE/forwardPE are Yahoo's GAAP-based "
                 "figures and can differ a lot; if you mention them, label them as GAAP.",
        "companyMetrics": facts, "priceTrend": trend, "fiveYearValuationContext": screen,
    }, default=str)
    text, model, usage = _respond(BRIEF_INSTRUCTIONS if compact else EXPLAIN_INSTRUCTIONS, payload)
    return {"markdown": text, "model": model, "symbol": symbol, "usage": usage}


# ---------------------------------------------------------------------------------------------
# 2. Ask the database (tool-calling agent)
# ---------------------------------------------------------------------------------------------

SCHEMA_GUIDE = """StockLens SQLite database. Prefer the view `latest_results` for current values.

latest_results (one row per stock, newest snapshot)
  symbol, name, sector, industry
  lists        comma-separated: sp500, nasdaq100, dow30, popular (use: lists LIKE '%sp500%')
  price        latest close (USD)
  ttm_eps      trailing-12-month adjusted EPS;  last_report = date of latest earnings report
  pe           trailing P/E;  pe_pctile = percentile 0–100 vs own 5-year daily history
  pe_min, pe_median, pe_max   5-year trailing P/E range
  fwd_pe       forward P/E (price ÷ next-12-month consensus EPS, I/B/E/S); fwd_pctile vs own 5-year monthly history
  fwd_min, fwd_median, fwd_max   range of the PAST 5 years of monthly values. Today's fwd_pe is compared
               against that history, so it can sit below fwd_min (a new low → fwd_pctile 0) or above
               fwd_max (a new high → 100). Same for pe vs pe_min/pe_max and peg. That is expected, not an error.
  peg          forward P/E ÷ expected next-year EPS growth (%); peg_pctile vs own history; peg_median
  growth       expected next-year EPS growth as a fraction (0.12 = 12%)
  ltg          analysts' long-term growth estimate in percent (thin coverage: ltg_analysts)
  fwd_months   months of I/B/E/S history available (percentiles need >= 24)
  unreliable   1 = spin-off in window, P/E history may be distorted (usually exclude)
  note, error  data caveats; error not null = stock couldn't be analyzed
  run_at       when the snapshot was taken

Expensive vs. cheap: high percentiles = expensive vs. the stock's own history, low = cheap. With "most extreme N%":
"expensive on K measures" = count of pe_pctile >= 100-N, fwd_pctile >= 100-N, peg_pctile >= 100-N;
"cheap on K measures" = count of those <= N (NULLs don't count). Default N = 20.

Other tables
  screen_runs(run_id, run_at, ibes_as_of, n_stocks)            one row per refresh
  screen_results(run_id, symbol, pe, percentile, fwd_pe, fwd_pctile, peg, peg_pctile, growth, price, ...)
                                                                every snapshot (for changes over time)
  stocks(symbol, name, sector, industry)
  memberships(symbol, list_name)
  earnings(symbol, report_date, eps)                            quarterly reported EPS (Yahoo)
  ibes_estimates(symbol, statpers, fy1, fy2, fy1_end, ltg, price)  monthly analyst consensus (I/B/E/S)
"""

ASK_INSTRUCTIONS = GUARDRAILS + """

You answer questions about the StockLens database by writing SQLite queries with the run_sql tool.
Workflow: (1) if unsure of columns, call get_schema; (2) write one or more SELECT queries; (3) answer.
Rules: only SELECT/WITH queries; use LIMIT (≤ 50 rows) unless counting; exclude rows where error IS NOT NULL
and usually where unreliable = 1, and say so; format growth as a percent; when listing stocks, show a compact
Markdown table. If a query errors, fix it and retry. If the question can't be answered from this data, say what's
missing. End with one line noting the snapshot date (run_at) you used."""

TOOLS = [
    {"type": "function", "name": "get_schema",
     "description": "Describe the StockLens database tables, the latest_results view, and what each column means.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "run_sql",
     "description": "Run ONE read-only SQLite SELECT query against stocklens.db and return up to 200 rows as JSON.",
     "parameters": {"type": "object", "properties": {"sql": {"type": "string", "description": "A single SELECT or WITH query"}},
                    "required": ["sql"], "additionalProperties": False}},
]


def run_sql(sql, max_rows=200):
    """Execute a single read-only query. The connection is opened read-only, so writes are impossible."""
    q = sql.strip().rstrip(";").strip()
    if not re.match(r"(?is)^(select|with)\b", q) or ";" in q:
        return json.dumps({"error": "Only a single SELECT/WITH query is allowed."})
    try:
        conn = sqlite3.connect(f"file:{db.DB_FILE}?mode=ro", uri=True, timeout=10)
        try:
            cur = conn.execute(q)
            cols = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(max_rows + 1)
        finally:
            conn.close()
    except Exception as e:
        return json.dumps({"error": str(e)})
    truncated = len(rows) > max_rows
    return json.dumps({"columns": cols, "rows": [list(r) for r in rows[:max_rows]], "truncated": truncated}, default=str)


def ask(question, max_steps=8):
    db.init()
    convo = [{"role": "user", "content": question}]
    queries, usage = [], {}
    for _ in range(max_steps):   # hard cap on agent-loop steps: a confused agent can't run up the bill
        r = client().responses.create(model=MODEL, instructions=ASK_INSTRUCTIONS, input=convo, tools=TOOLS)
        _add_usage(usage, r)
        calls = [o for o in r.output if o.type == "function_call"]
        if not calls:
            return {"markdown": r.output_text, "model": r.model, "queries": queries, "usage": usage}
        convo += [o.model_dump(exclude_none=True) for o in r.output]
        for c in calls:
            if c.name == "get_schema":
                out = SCHEMA_GUIDE
            elif c.name == "run_sql":
                sql = json.loads(c.arguments or "{}").get("sql", "")
                out = run_sql(sql)
                queries.append({"sql": sql, "ok": '"error"' not in out[:20]})
            else:
                out = json.dumps({"error": f"unknown tool {c.name}"})
            convo.append({"type": "function_call_output", "call_id": c.call_id, "output": out})
    return {"markdown": "I couldn't finish within the step limit — try a more specific question.",
            "model": MODEL, "queries": queries, "usage": usage}


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "explain":
        print(explain_stock(sys.argv[2] if len(sys.argv) > 2 else "AAPL")["markdown"])
    elif cmd == "brief":
        print(explain_stock(sys.argv[2] if len(sys.argv) > 2 else "AAPL", compact=True)["markdown"])
    elif cmd == "ask":
        res = ask(" ".join(sys.argv[2:]) or "Which stocks are high on all 3 measures at the 80th percentile?")
        for q in res["queries"]:
            print("SQL:", q["sql"])
        print(res["markdown"])
    else:
        print(__doc__)
