# StockLens: is this stock expensive vs. its own history?

**Live app: https://stocklens-9saq.onrender.com**. It's on free hosting, so the first visit after a quiet period
can take up to a minute while the server wakes up.

StockLens screens the **S&P 500, Nasdaq-100 and Dow 30** (516 stocks) and shows where each one's **trailing P/E,
forward P/E and PEG** sit against **its own 5-year history**. AI agents explain the numbers in plain English and
answer questions about the data. MGT 409 (AI Foundations for Managers) final project · Josh Liang.

## What it does

| Page | What you get |
|---|---|
| **Lookup** (`/`) | **Market today**: index moves, top headlines with pictures, and the stock getting the **most market attention** (unusual volume × money traded × price move × news mentions), which loads automatically. Then that stock's metrics, price chart with 20/50/100/200-day moving averages, RSI, annual financials, and an **"Explain with AI"** summary. |
| **P/E Screener** (`/screener`) | Every stock ranked by percentile vs. its own 5 years on trailing P/E, forward P/E or PEG. Badges show stocks expensive on **ALL 3** or **2 OF 3** measures. Filter by index, sector and cutoff. Click **✦** on any row for an inline AI summary. |
| **Ask AI** (`/ask`) | Ask questions in plain English ("Which tech stocks are high on all 3 measures but still expect 15%+ growth?"). An agent writes read-only SQL against the database, runs it, and answers. You can see the queries it ran. |

## Run it locally (about 5 minutes)

Requires Python 3.11+ (tested on 3.13 and 3.14).

```bash
git clone <this repo URL> stocklens
cd stocklens
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**API key.** The AI features need `PORTKEY_API_KEY` (gpt-6-luna via Portkey). The app looks for a `.env` file
**first in this folder, then in the folder above it**, so a shared `.env` one level up works as-is. Otherwise:

```bash
cp .env.example .env               # then put your key after PORTKEY_API_KEY=
```

**Start the app:**

```bash
python app.py
```

Open **http://127.0.0.1:5050**. Everything works without a key except the three AI features.

## Data

| Source | Used for | Refresh |
|---|---|---|
| Yahoo Finance (via `yfinance`) | Prices, company metrics, quarterly earnings, news, most-active stocks | Live; screener: **Refresh prices** button (about 20 s for 516 stocks) |
| LSEG I/B/E/S via WRDS (Yale license) | Monthly analyst consensus EPS → forward P/E and PEG history | Monthly, needs a WRDS login: `python wrds_login.py` once, then `python ibes.py` |
| Wikipedia | S&P 500 member list | `python update_index_lists.py` |

A ready-to-use snapshot is included in **`stocklens.db`** (SQLite, ~15 MB), so the app runs immediately with no
WRDS account. It holds the stock lists, cached quarterly earnings, I/B/E/S estimates, screener snapshots, daily
"most attention" rankings and the AI usage log. To test with a **smaller dataset**, edit the lists in `universe.py`
(e.g. keep only `DOW30`) and click **Refresh prices**; the screener rescans just those stocks.

## The AI agents (`agents.py`)

All use **gpt-6-luna** through Portkey with the course key only.

1. **Stock explainer** (Lookup page and the screener's ✦ rows). It gets the stock's metrics, price trend and
   5-year percentiles, then writes a full analysis (Lookup) or a 4-bullet brief (screener). 1 model call.
2. **Ask the database** (`/ask`). A tool-calling agent with two tools, `get_schema` and `run_sql`. It loops
   (up to 8 steps): it writes SQL, reads the results, fixes errors, and answers with a table. Usually 2–4 model calls.

## Safety

- **Usage log** (`guard.py`): every AI request is logged with its token counts in the `ai_usage` table. Visitors
  are identified by a salted hash of their IP; raw IPs are never stored. Optional limits (per-visitor hourly and
  site-wide daily caps) are built in but **off by default**; set `AI_CALLS_PER_HOUR`, `AI_DAILY_CALL_CAP` or
  `AI_DAILY_TOKEN_CAP` to turn them on (over-limit requests get HTTP 429).
- **Read-only SQL**: the Ask agent's database connection is opened read-only (`mode=ro`), and only a single
  `SELECT`/`WITH` statement is accepted, so prompt injection can't modify or delete data.
- **Bounded agent loop**: at most 8 steps per question.
- **Grounded answers**: agents may only use the data they're given and are told never to give buy/sell advice.
  Every answer is labeled "AI can make mistakes · not investment advice".
- **Refresh cooldown**: the screener can be rescanned at most every 10 minutes, so visitors can't hammer Yahoo.
- **Secrets**: keys come from `.env` or host environment variables and are never committed (see `.gitignore`).

**Measured token usage** (for tokenomics):

```bash
sqlite3 stocklens.db "select agent, count(*) runs, avg(llm_calls), avg(input_tokens), avg(cached_tokens),
                      avg(output_tokens), avg(ms) from ai_usage where ok = 1 group by agent"
```

## Project structure

```
app.py               Flask web server: pages + JSON API
market.py            Yahoo data: stock metrics, price history, MAs/RSI, market news and attention ranking
screener.py          5-year trailing P/E history and percentiles (batched price downloads, cached earnings)
ibes.py              I/B/E/S forward P/E and PEG from WRDS (direct PostgreSQL connection)
agents.py            The AI agents
guard.py             Spending guard (rate limits, daily caps, usage log)
db.py                SQLite database (stocklens.db)
universe.py          Stock lists: S&P 500, Nasdaq-100, Dow 30, Popular 50
static/              The three web pages + md.js (renders AI answers)
render.yaml          Render deployment blueprint
```

## Deploy on Render (free)

1. Push this repo to GitHub. In Render: **New → Blueprint**, then select the repo (it reads `render.yaml`).
2. When asked, paste your `PORTKEY_API_KEY`. Render stores it as a secret.
3. Deploy. The app comes up at `https://<name>.onrender.com`. Free instances sleep after 15 idle minutes and
   wake on the next visit. Data written on Render (refreshes, usage log) resets when the service restarts.

_Educational project, not investment advice. Data may be delayed or incomplete._
