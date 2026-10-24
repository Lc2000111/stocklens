"""Quick yfinance demo: pull prices and basic info for a ticker.

Usage:
    .venv/bin/python fetch_stock.py          # defaults to AAPL
    .venv/bin/python fetch_stock.py MSFT
"""

import sys

import yfinance as yf

symbol = sys.argv[1].upper() if len(sys.argv) > 1 else "AAPL"
ticker = yf.Ticker(symbol)

# Company snapshot
info = ticker.info
print(f"\n=== {info.get('longName', symbol)} ({symbol}) ===")
print(f"Sector:         {info.get('sector')}")
print(f"Price:          {info.get('currentPrice')}")
print(f"Market cap:     {info.get('marketCap'):,}" if info.get("marketCap") else "Market cap:     n/a")
print(f"Trailing P/E:   {info.get('trailingPE')}")

# Last 5 trading days of prices
print("\n--- Last 5 days ---")
history = ticker.history(period="5d")
print(history[["Open", "High", "Low", "Close", "Volume"]].round(2))

# Annual income statement (a few key rows)
print("\n--- Annual income statement (key rows) ---")
income = ticker.income_stmt
rows = [r for r in ["Total Revenue", "Gross Profit", "Operating Income", "Net Income"] if r in income.index]
print(income.loc[rows])
