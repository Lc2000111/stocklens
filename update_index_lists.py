"""Refresh the S&P 500 member list from Wikipedia's "List of S&P 500 companies" table.

Run (a few times a year — S&P changes membership as companies grow, shrink or merge):
    .venv/bin/python update_index_lists.py

Writes index_lists/sp500.csv (ticker, company, sector, sub-industry, date added). The Nasdaq-100
and Dow 30 lists are short and change rarely, so they live directly in universe.py.
"""

import io
from pathlib import Path

import pandas as pd
import requests

URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
OUT = Path(__file__).parent / "index_lists" / "sp500.csv"

html = requests.get(URL, headers={"User-Agent": "StockLens class project (MGT 409)"}, timeout=30).text
table = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})[0]
df = pd.DataFrame({
    # Yahoo writes share classes with a dash (BRK-B); Wikipedia uses a dot (BRK.B)
    "symbol": table["Symbol"].str.replace(".", "-", regex=False),
    "company": table["Security"],
    "sector": table["GICS Sector"],
    "sub_industry": table["GICS Sub-Industry"],
    "date_added": table["Date added"],
    "cik": table["CIK"],
})
OUT.parent.mkdir(exist_ok=True)
df.to_csv(OUT, index=False)
print(f"Saved {len(df)} securities ({df['cik'].nunique()} companies) to {OUT.relative_to(Path(__file__).parent)}")
