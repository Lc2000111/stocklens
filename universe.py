"""Stock lists the screener scans. Edit these, then click "Refresh prices" — the database syncs to them.

S&P 500: 503 securities from 500 companies, loaded from index_lists/sp500.csv
(refresh it with `.venv/bin/python update_index_lists.py`).

Dow Jones Industrial Average: 30 companies, all also in the S&P 500. Source: Wikipedia, October 2026.

Nasdaq-100 (what QQQ tracks): 101 securities from 100 companies. Alphabet is listed twice (GOOGL
Class A and GOOG Class C); both are analyzed. Fox (FOX) and News Corp (NWS) second classes are skipped.
Source: Wikipedia's Nasdaq-100 component list, October 2026. Verify against Invesco's QQQ holdings for
anything official; the index is reconstituted every December.
"""

import csv
from pathlib import Path

POPULAR = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA", "GOOGL": "Alphabet", "AMZN": "Amazon",
    "META": "Meta Platforms", "AVGO": "Broadcom", "TSLA": "Tesla", "NFLX": "Netflix", "ORCL": "Oracle",
    "CRM": "Salesforce", "ADBE": "Adobe", "AMD": "AMD", "INTC": "Intel", "CSCO": "Cisco",
    "QCOM": "Qualcomm", "TXN": "Texas Instruments", "IBM": "IBM", "PLTR": "Palantir", "UBER": "Uber",
    "JPM": "JPMorgan Chase", "BAC": "Bank of America", "WFC": "Wells Fargo", "GS": "Goldman Sachs", "MS": "Morgan Stanley",
    "V": "Visa", "MA": "Mastercard", "LLY": "Eli Lilly", "UNH": "UnitedHealth", "JNJ": "Johnson & Johnson",
    "MRK": "Merck", "ABBV": "AbbVie", "PFE": "Pfizer", "AMGN": "Amgen", "WMT": "Walmart",
    "COST": "Costco", "HD": "Home Depot", "LOW": "Lowe's", "MCD": "McDonald's", "SBUX": "Starbucks",
    "NKE": "Nike", "KO": "Coca-Cola", "PEP": "PepsiCo", "PG": "Procter & Gamble", "DIS": "Disney",
    "XOM": "Exxon Mobil", "CVX": "Chevron", "CAT": "Caterpillar", "GE": "GE Aerospace", "HON": "Honeywell",
}

NASDAQ100 = {
    "ADBE": "Adobe", "ADP": "ADP", "AMD": "AMD", "ABNB": "Airbnb", "ALNY": "Alnylam Pharmaceuticals",
    "GOOGL": "Alphabet", "GOOG": "Alphabet (Class C)", "AMZN": "Amazon", "AEP": "American Electric Power",
    "AMGN": "Amgen", "ADI": "Analog Devices", "AAPL": "Apple", "AMAT": "Applied Materials", "APP": "AppLovin",
    "ARM": "Arm Holdings", "ASML": "ASML", "ALAB": "Astera Labs", "ADSK": "Autodesk", "AXON": "Axon",
    "BKR": "Baker Hughes", "BKNG": "Booking Holdings", "AVGO": "Broadcom", "CDNS": "Cadence Design",
    "CTAS": "Cintas", "CSCO": "Cisco", "CCEP": "Coca-Cola Europacific", "CMCSA": "Comcast",
    "CEG": "Constellation Energy", "CPRT": "Copart", "CRWV": "CoreWeave", "COST": "Costco",
    "CRWD": "CrowdStrike", "CSX": "CSX", "DDOG": "Datadog", "DXCM": "Dexcom", "FANG": "Diamondback Energy",
    "DASH": "DoorDash", "EXC": "Exelon", "FAST": "Fastenal", "FER": "Ferrovial", "FTNT": "Fortinet",
    "GEHC": "GE HealthCare", "GILD": "Gilead Sciences", "HONA": "Honeywell Aerospace", "HON": "Honeywell",
    "IDXX": "IDEXX Laboratories", "INTC": "Intel", "INTU": "Intuit", "ISRG": "Intuitive Surgical",
    "KDP": "Keurig Dr Pepper", "KLAC": "KLA", "LRCX": "Lam Research", "LIN": "Linde", "LITE": "Lumentum",
    "MAR": "Marriott", "MRVL": "Marvell Technology", "MELI": "MercadoLibre", "META": "Meta Platforms",
    "MCHP": "Microchip Technology", "MU": "Micron Technology", "MSFT": "Microsoft", "MSTR": "Strategy",
    "MRNA": "Moderna", "MDLZ": "Mondelez", "MPWR": "Monolithic Power", "MNST": "Monster Beverage",
    "NBIS": "Nebius Group", "NFLX": "Netflix", "NVDA": "NVIDIA", "NXPI": "NXP Semiconductors",
    "ORLY": "O'Reilly Automotive", "ODFL": "Old Dominion Freight", "PCAR": "PACCAR", "PLTR": "Palantir",
    "PANW": "Palo Alto Networks", "PAYX": "Paychex", "PYPL": "PayPal", "PDD": "PDD Holdings", "PEP": "PepsiCo",
    "QCOM": "Qualcomm", "REGN": "Regeneron", "RKLB": "Rocket Lab", "ROP": "Roper Technologies",
    "ROST": "Ross Stores", "SNDK": "Sandisk", "STX": "Seagate", "SHOP": "Shopify", "SPCX": "SpaceX",
    "SBUX": "Starbucks", "SNPS": "Synopsys", "TMUS": "T-Mobile US", "TTWO": "Take-Two Interactive",
    "TER": "Teradyne", "TSLA": "Tesla", "TXN": "Texas Instruments", "TRI": "Thomson Reuters",
    "VRTX": "Vertex Pharmaceuticals", "WMT": "Walmart", "WDC": "Western Digital", "WDAY": "Workday",
    "XEL": "Xcel Energy",
}

DOW30 = {
    "MMM": "3M", "GOOGL": "Alphabet", "AMZN": "Amazon", "AXP": "American Express", "AMGN": "Amgen",
    "AAPL": "Apple", "BA": "Boeing", "CAT": "Caterpillar", "CVX": "Chevron", "CSCO": "Cisco",
    "KO": "Coca-Cola", "DIS": "Disney", "GS": "Goldman Sachs", "HD": "Home Depot", "HON": "Honeywell",
    "IBM": "IBM", "JNJ": "Johnson & Johnson", "JPM": "JPMorgan Chase", "MCD": "McDonald's", "MRK": "Merck",
    "MSFT": "Microsoft", "NKE": "Nike", "NVDA": "NVIDIA", "PG": "Procter & Gamble", "CRM": "Salesforce",
    "SHW": "Sherwin-Williams", "TRV": "Travelers", "UNH": "UnitedHealth", "V": "Visa", "WMT": "Walmart",
}


def _load_sp500():
    path = Path(__file__).parent / "index_lists" / "sp500.csv"
    if not path.exists():
        return {}
    with open(path, newline="") as f:
        return {row["symbol"]: row["company"] for row in csv.DictReader(f)}


SP500 = _load_sp500()

LISTS = {"popular": POPULAR, "nasdaq100": NASDAQ100, "sp500": SP500, "dow30": DOW30}
LIST_LABELS = {"popular": "Popular 50", "nasdaq100": "Nasdaq-100 (QQQ)", "sp500": "S&P 500", "dow30": "Dow Jones 30"}

# Second share classes: members of a list, but not analyzed separately (same company, same earnings)
# Alphabet's two classes (GOOGL, GOOG) are both analyzed, at the user's request.
SAME_COMPANY_AS = {"FOX": "FOXA", "NWS": "NWSA"}

# Share classes of one company should use the same earnings. Yahoo's GOOGL "Reported EPS" for the
# last two quarters includes one-time gains ($9.11 vs a $2.90 estimate, Jul 2026), while GOOG's matches
# the adjusted basis analysts use ($2.85) — so both Alphabet classes use GOOG's earnings.
EARNINGS_FROM = {"GOOGL": "GOOG"}


def all_symbols():
    """Every symbol to analyze (deduplicated, second share classes skipped)."""
    seen = {}
    for lst in LISTS.values():
        for sym, name in lst.items():
            if sym not in SAME_COMPANY_AS:
                seen.setdefault(sym, name)
    return seen


assert len(NASDAQ100) == 101, f"Nasdaq-100 should have 101 securities, has {len(NASDAQ100)}"
assert len(DOW30) == 30, f"Dow should have 30 companies, has {len(DOW30)}"
