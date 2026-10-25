"""Stock metrics dashboard — Flask backend that serves yfinance data as JSON.

Run:
    .venv/bin/python app.py
Then open http://127.0.0.1:5050
"""

import os
import threading
import time

import yfinance as yf
from flask import Flask, jsonify, request, send_from_directory

import agents
import db
import guard
import market
import screener
import statements

app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 60 * 1024 * 1024   # whole upload; each file is also capped at 5 MB

# Background state for the P/E screener (it takes ~15 seconds to scan every stock)
screen_job = {"running": False, "done": 0, "total": 0, "step": "", "error": None, "stop": False}


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/stock")
def stock_page():
    return send_from_directory("static", "stock.html")


@app.route("/screener")
def screener_page():
    return send_from_directory("static", "screener.html")


@app.route("/ask")
def ask_page():
    return send_from_directory("static", "ask.html")


@app.route("/statement")
def statement_page():
    return send_from_directory("static", "statement.html")


# ---------- Personal income statement (statements.py). Files are processed in memory and never saved. ----------

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "sample_statements")


@app.route("/api/statement/parse", methods=["POST"])
def statement_parse():
    files = [(f.filename or "statement", f.read()) for f in request.files.getlist("files") if f and f.filename]
    if not files:
        return jsonify({"error": "Choose at least one CSV or PDF statement."}), 400
    return _agent("statement", statements.process, files)


@app.route("/api/statement/sample", methods=["POST"])
def statement_sample():
    names = sorted(n for n in os.listdir(SAMPLE_DIR) if n.lower().endswith((".csv", ".pdf")))
    files = [(n, open(os.path.join(SAMPLE_DIR, n), "rb").read()) for n in names]
    return _agent("statement", statements.process, files)


@app.route("/api/statement/insights", methods=["POST"])
def statement_insights():
    summary = request.get_json(silent=True) or {}
    if not summary.get("totals"):
        return jsonify({"error": "Build an income statement first."}), 400
    return _agent("insights", statements.insights, summary)


# ---------- AI agents (gpt-6-luna via Portkey; see agents.py) ----------

def _agent(name, fn, *args):
    """Run an AI agent behind the spending guard (rate limit + daily caps + usage log)."""
    visitor = guard.visitor_id(request)
    try:
        row = guard.reserve(visitor, name)
    except guard.LimitReached as e:
        return jsonify({"error": str(e), "limited": True}), 429
    started = time.time()
    try:
        result = fn(*args)
    except Exception as e:
        guard.finish(row, error=e, started=started)
        return jsonify({"error": str(e)[:300]}), 500
    guard.finish(row, result=result, started=started)
    return jsonify(result)


@app.route("/api/ai/usage")
def ai_usage():
    return jsonify(guard.status(guard.visitor_id(request)))


@app.route("/api/agent/explain/<symbol>", methods=["POST"])
def agent_explain(symbol):
    compact = bool((request.get_json(silent=True) or {}).get("compact"))
    return _agent("brief" if compact else "explain", agents.explain_stock, symbol, compact)


@app.route("/api/agent/ask", methods=["POST"])
def agent_ask():
    question = ((request.get_json(silent=True) or {}).get("question") or "").strip()
    if not question:
        return jsonify({"error": "Type a question first."}), 400
    return _agent("ask", agents.ask, question[:1000])


@app.route("/api/screener")
def screener_results():
    return jsonify({"job": screen_job, "data": screener.load_cache()})


REFRESH_COOLDOWN_MIN = int(os.environ.get("REFRESH_COOLDOWN_MIN", 10))  # protects Yahoo (and the site) from repeated scans


@app.route("/api/screener/refresh", methods=["POST"])
def screener_refresh():
    last = screener.load_cache()
    if last and not screen_job["running"]:
        age_min = (time.time() - time.mktime(time.strptime(last["updated"], "%Y-%m-%d %H:%M"))) / 60
        if age_min < REFRESH_COOLDOWN_MIN:
            wait = max(1, round(REFRESH_COOLDOWN_MIN - age_min))
            return jsonify({"job": screen_job, "error": f"Prices were refreshed {max(0, round(age_min))} min ago. "
                                                         f"Try again in about {wait} min."}), 429
    if not screen_job["running"]:
        screen_job.update(running=True, done=0, total=len(screener.UNIVERSE), step="Starting…", error=None, stop=False)

        def work():
            try:
                screener.run_screen(progress=lambda d, n, step: screen_job.update(done=d, total=n, step=step),
                                    should_stop=lambda: screen_job["stop"])
            except Exception as e:
                screen_job["error"] = str(e)
            finally:
                screen_job.update(running=False, stop=False)

        threading.Thread(target=work, daemon=True).start()
    return jsonify({"job": screen_job})


@app.route("/api/screener/stop", methods=["POST"])
def screener_stop():
    """Ask a running scan to stop at its next checkpoint (within a few seconds). Nothing partial is saved."""
    if screen_job["running"]:
        screen_job["stop"] = True
        screen_job["step"] = "Stopping…"
    return jsonify({"job": screen_job})


@app.route("/api/search")
def search():
    query = request.args.get("q", "").strip()[:80]
    if not query:
        return jsonify([])
    # 1) our own 516 stocks: instant and works even when Yahoo search is slow or blocked
    try:
        results = db.search_stocks(query)
    except Exception:
        results = []
    # 2) Yahoo search, for everything else (other tickers, ETFs, foreign listings)
    try:
        quotes = yf.Search(query, max_results=8).quotes
    except Exception:
        quotes = []
    seen = {r["symbol"] for r in results}
    for q in quotes:
        if q.get("quoteType") in ("EQUITY", "ETF") and q.get("symbol") not in seen:
            seen.add(q.get("symbol"))
            results.append({"symbol": q.get("symbol"), "name": q.get("shortname") or q.get("longname"),
                            "type": q.get("quoteType"), "exchange": q.get("exchDisp")})
    return jsonify(results[:8])


@app.route("/api/market/today")
def market_today():
    try:
        return jsonify(market.market_today(force=request.args.get("refresh") == "1"))
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 502


@app.route("/api/stock/<symbol>")
def stock(symbol):
    data, status = market.stock_data(symbol)
    return jsonify(data), status


@app.route("/api/history/<symbol>")
def history(symbol):
    data, status = market.history_data(symbol, request.args.get("period", "1y"))
    return jsonify(data), status


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))   # e.g. PORT=5051 python app.py if 5050 is busy
    print(f"\n  StockLens is running → open http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=True)
