"""One-time WRDS login + I/B/E/S access check.

Run:
    .venv/bin/python wrds_login.py

Asks for your WRDS username and password (WRDS will send a Duo push to approve), checks the
login works, then saves it to ~/.pgpass (readable only by you) so ibes.py connects without prompting.

Uses psycopg2 directly: the `wrds` package pins an old pandas that crashes on Python 3.14.
"""

import getpass
import os
from pathlib import Path

import psycopg2

HOST, PORT, DB = "wrds-pgdata.wharton.upenn.edu", 9737, "wrds"

# WRDS usernames are lowercase, and the database login is case-sensitive
username = input("WRDS username: ").strip().lower()
password = getpass.getpass("WRDS password (hidden): ")
print("Connecting — approve the Duo push on your phone…")

try:
    conn = psycopg2.connect(host=HOST, port=PORT, dbname=DB, user=username, password=password,
                            sslmode="require", connect_timeout=90)
except Exception as e:
    print(f"\n❌ Login failed: {str(e).strip().splitlines()[-1][:200]}")
    print("   Check: password typed correctly · Duo push approved within ~1 minute ·")
    print("   account activated (log in once at wrds-www.wharton.upenn.edu to set up Duo).")
    raise SystemExit(1)

# Save to ~/.pgpass (replacing any old WRDS line); colons/backslashes must be escaped
pgpass = Path.home() / ".pgpass"
escaped = password.replace("\\", "\\\\").replace(":", "\\:")
lines = [l for l in (pgpass.read_text().splitlines() if pgpass.exists() else []) if not l.startswith(HOST + ":")]
lines.append(f"{HOST}:{PORT}:{DB}:{username}:{escaped}")
pgpass.write_text("\n".join(lines) + "\n")
os.chmod(pgpass, 0o600)
print("Saved login to ~/.pgpass")

print("\nChecking I/B/E/S access...")
try:
    with conn.cursor() as cur:
        cur.execute("select count(*), max(statpers) from ibes.statsum_epsus where oftic = 'AAPL' and fpi = '1'")
        n, latest = cur.fetchone()
    print(f"✅ I/B/E/S access OK — {n} monthly AAPL consensus records; latest data month: {latest}")
    if username != "lc2000111":
        print(f"   Note: update WRDS_USERNAME in ibes.py to '{username}'.")
except Exception as e:
    print(f"❌ No I/B/E/S access: {e}\n   Your WRDS account may not include I/B/E/S — check with the Yale library.")
conn.close()
