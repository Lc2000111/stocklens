"""Market data for one stock (Yahoo via yfinance): company metrics, price history, moving averages, RSI.

Shared by the web routes (app.py) and the AI agents (agents.py), so both use the same numbers.
Each function returns (data_dict, http_status).
"""

import math

import pandas as pd
import yfinance as yf


def clean(value):
    """Turn NaN/inf into None so the JSON stays valid."""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def stock_data(symbol):
    ticker = yf.Ticker(symbol.upper())
    try:
        info = ticker.info
    except Exception as e:
        return {"error": f"Could not fetch {symbol}: {e}"}, 502
    if not info or (info.get("regularMarketPrice") is None and info.get("currentPrice") is None):
        return {"error": f"No data found for '{symbol.upper()}'."}, 404

    keys = [
        # Profile
        "symbol", "longName", "shortName", "sector", "industry", "exchange", "currency", "website", "longBusinessSummary", "quoteType",
        # Price
        "currentPrice", "regularMarketPrice", "previousClose", "regularMarketChange", "regularMarketChangePercent",
        "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "fiftyDayAverage", "twoHundredDayAverage", "beta",
        # Valuation
        "marketCap", "enterpriseValue", "trailingPE", "forwardPE", "trailingPegRatio", "priceToBook",
        "priceToSalesTrailing12Months", "enterpriseToEbitda", "enterpriseToRevenue",
        # Earnings
        "trailingEps", "forwardEps", "epsCurrentYear",
        # Growth
        "revenueGrowth", "earningsGrowth", "earningsQuarterlyGrowth",
        # Profitability
        "grossMargins", "operatingMargins", "profitMargins", "returnOnEquity", "returnOnAssets",
        # Financial health
        "totalRevenue", "ebitda", "freeCashflow", "totalCash", "totalDebt", "debtToEquity", "currentRatio",
        # Dividends
        "dividendYield", "dividendRate", "payoutRatio",
        # Analysts
        "targetMeanPrice", "targetHighPrice", "targetLowPrice", "recommendationKey", "numberOfAnalystOpinions",
    ]
    data = {k: clean(info.get(k)) for k in keys}

    # Annual revenue / net income with year-over-year growth
    annual = []
    try:
        income = ticker.income_stmt
        if income is not None and not income.empty:
            for col in sorted(income.columns):
                rev = clean(float(income.at["Total Revenue", col])) if "Total Revenue" in income.index else None
                ni = clean(float(income.at["Net Income", col])) if "Net Income" in income.index else None
                eps = clean(float(income.at["Diluted EPS", col])) if "Diluted EPS" in income.index else None
                if rev is None and ni is None:
                    continue
                annual.append({"year": str(col.year), "revenue": rev, "netIncome": ni, "eps": eps})
            for prev, cur in zip(annual, annual[1:]):
                if prev["revenue"] and cur["revenue"]:
                    cur["revenueGrowth"] = cur["revenue"] / prev["revenue"] - 1
    except Exception:
        pass
    data["annual"] = annual
    return data, 200


PERIOD_DAYS = {"1mo": 31, "6mo": 183, "1y": 365, "5y": 365 * 5}
MA_WINDOWS = [20, 50, 100, 200]  # simple moving averages, in trading days
MAX_POINTS = 1000  # downsample long ranges so the chart stays fast


def compute_rsi(close, n=14):
    """Relative Strength Index using Wilder's smoothing."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)


def num(value):
    if pd.isna(value):
        return None
    value = float(value)
    return round(value, 4 if abs(value) < 10 else 2)  # keep precision for low-priced history


def history_data(symbol, period="1y"):
    if period not in {"1mo", "6mo", "ytd", "1y", "5y", "max"}:
        period = "1y"

    # Always use daily bars, and fetch ~400 extra calendar days before the visible
    # window so the 200-day average and RSI are already "warmed up" on day one.
    today = pd.Timestamp.today().normalize()
    if period == "max":
        start = None
    elif period == "ytd":
        start = pd.Timestamp(today.year, 1, 1)
    else:
        start = today - pd.Timedelta(days=PERIOD_DAYS[period])
    try:
        ticker = yf.Ticker(symbol.upper())
        if start is None:
            hist = ticker.history(period="max", interval="1d")
        else:
            hist = ticker.history(start=(start - pd.Timedelta(days=400)).strftime("%Y-%m-%d"), interval="1d")
    except Exception as e:
        return {"error": str(e)}, 502

    close = hist["Close"].dropna()
    if close.empty:
        return {"points": [], "latest": None}, 200

    df = pd.DataFrame({"close": close})
    for w in MA_WINDOWS:
        df[f"sma{w}"] = close.rolling(w).mean()
    df["rsi"] = compute_rsi(close)

    latest = {col: num(df[col].iloc[-1]) for col in df.columns}

    if start is not None:
        df = df[df.index >= start.tz_localize(df.index.tz)]
    step = max(1, math.ceil(len(df) / MAX_POINTS))
    if step > 1:
        keep = list(range(0, len(df), step))
        if keep[-1] != len(df) - 1:
            keep.append(len(df) - 1)  # always include the latest day
        df = df.iloc[keep]

    points = [
        {"date": idx.strftime("%Y-%m-%d"), **{col: num(row[col]) for col in df.columns}}
        for idx, row in df.iterrows()
    ]
    return {"points": points, "latest": latest, "maWindows": MA_WINDOWS}, 200


# ---------------------------------------------------------------------------------------------
# Market today: index moves, top headlines, and the stock getting the most attention
# ---------------------------------------------------------------------------------------------

import re
import threading
import time as _time

INDICES = {"^GSPC": "S&P 500", "^IXIC": "Nasdaq", "^DJI": "Dow Jones"}
NEWS_QUERIES = ["stock market today", "Wall Street stocks", "stocks movers", "stocks to watch", "earnings stocks today"]
HEADLINE_QUERIES = NEWS_QUERIES[:3]   # shown on the page; the wider set is only used to count mentions
# "Top 3 ... stocks to watch" listicles crowd out real market news, so they're not shown as headlines
LISTICLE = re.compile(r"(?i)\bstocks? to (watch|buy)\b|penny stocks|^\s*(top\s+)?\d+\s+\w+(\s+\w+)?\s+stocks\b")
MIN_MARKET_CAP = 10e9      # ignore small caps so the pick is a company most people would recognize
CACHE_SECONDS = 15 * 60
_today_cache = {"at": 0, "data": None}
_today_lock = threading.Lock()


def _headlines(query, n=12):
    try:
        items = yf.Search(query, news_count=n).news or []
    except Exception:
        return []
    out = []
    for x in items:
        link = x.get("link") or ""
        if not link.startswith(("http://", "https://")):
            continue
        out.append({"id": x.get("uuid") or link, "title": x.get("title"), "publisher": x.get("publisher"),
                    "link": link, "time": x.get("providerPublishTime"), "tickers": x.get("relatedTickers") or [],
                    "image": _thumbnail(x)})
    return out


def _thumbnail(item):
    """The small (140×140) version of a story's picture. The 'original' can be 6000px wide, too heavy for a list."""
    sizes = [r for r in (item.get("thumbnail") or {}).get("resolutions", []) if (r.get("url") or "").startswith("https://")]
    small = [r for r in sizes if r.get("tag") == "140x140"] or sorted(sizes, key=lambda r: r.get("width") or 1e9)
    return small[0]["url"] if small else None


def _indices():
    try:
        close = yf.download(list(INDICES), period="5d", auto_adjust=False, progress=False)["Close"].dropna()
    except Exception:
        return [], None
    out = []
    for sym, name in INDICES.items():
        if sym in close and len(close[sym]) >= 2:
            last, prev = float(close[sym].iloc[-1]), float(close[sym].iloc[-2])
            out.append({"symbol": sym, "name": name, "level": round(last, 2), "changePct": round((last / prev - 1) * 100, 2)})
    session = close.index[-1].strftime("%Y-%m-%d") if len(close) else None
    return out, session


def _attention(news):
    """Score candidates on four signals of attention:
        money traded:     log10(dollar volume ÷ $100M)  — how much trading, without letting mega-caps always win
        unusual volume:   √(today's volume ÷ 3-month average)
        price move:       1 + |% change| ÷ 10
        news coverage:    1 + 0.5 × number of today's top market headlines that tag the stock"""
    quotes = {}
    for screen, count in (("most_actives", 50), ("day_gainers", 25), ("day_losers", 25)):
        try:
            for q in yf.screen(screen, count=count)["quotes"]:
                quotes.setdefault(q["symbol"], q)
        except Exception:
            continue
    mentions = {}
    for item in news:
        for t in set(item["tickers"]):
            mentions[t] = mentions.get(t, 0) + 1
    scored = []
    for sym, q in quotes.items():
        vol, avg = q.get("regularMarketVolume"), q.get("averageDailyVolume3Month")
        chg, cap, price = q.get("regularMarketChangePercent"), q.get("marketCap"), q.get("regularMarketPrice")
        if q.get("quoteType") != "EQUITY" or not vol or not avg or chg is None or not cap or cap < MIN_MARKET_CAP:
            continue
        rel = vol / avg
        m = mentions.get(sym, 0)
        dollars = vol * (price or 0)
        if dollars < 1e8:
            continue
        score = math.log10(dollars / 1e8) * math.sqrt(rel) * (1 + abs(chg) / 10) * (1 + 0.5 * m)
        scored.append({"symbol": sym, "name": q.get("shortName") or q.get("longName") or sym,
                       "price": price, "changePct": round(chg, 2), "relVolume": round(rel, 2),
                       "dollarVolume": round(vol * (price or 0)), "marketCap": cap, "mentions": m,
                       "score": round(score, 2)})
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:8]


def market_today(force=False):
    """Everything for the 'Market today' panel. Cached for 15 minutes."""
    with _today_lock:
        if not force and _today_cache["data"] and _time.time() - _today_cache["at"] < CACHE_SECONDS:
            return _today_cache["data"]
        seen, news, shown = set(), [], []
        for q in NEWS_QUERIES:
            for item in _headlines(q, 15):
                if item["id"] in seen or not item["title"]:
                    continue
                seen.add(item["id"])
                news.append(item)                       # every story counts toward mentions
                if q in HEADLINE_QUERIES and not LISTICLE.search(item["title"]):
                    shown.append(item)                  # only real market news is displayed
        shown.sort(key=lambda x: x["time"] or 0, reverse=True)
        indices, session = _indices()
        ranked = _attention(news)
        focus_news = []
        if ranked:
            top = ranked[0]["symbol"]
            focus_news = [n for n in _headlines(top, 10) if top in n["tickers"]][:3]
        data = {
            "session": session, "indices": indices, "news": shown[:8], "attention": ranked, "focusNews": focus_news,
            "method": "Attention score combines four signals: money traded (log of dollar volume), unusual volume "
                      "(√ of today's volume ÷ 3-month average), the size of the price move, and how many of today's top "
                      "market headlines mention the stock. Candidates are Yahoo's most-active stocks and biggest movers "
                      f"worth ${MIN_MARKET_CAP / 1e9:.0f}B+.",
            "generatedAt": _time.strftime("%Y-%m-%d %H:%M"),
        }
        _today_cache.update(at=_time.time(), data=data)
        try:
            import db
            db.save_attention(session, ranked)
        except Exception:
            pass  # history is a nice-to-have; never break the page over it
        return data
