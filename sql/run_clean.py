"""Run sql/clean.sql in SQLite, then check its output matches clean.py's.

Usage (from the project folder): python sql/run_clean.py
Run clean.py first: the check compares against data/clean/.
Writes data/sod.db (gitignored) and data/clean_sql/*.csv.
"""
import glob
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW, CLEAN, OUT = ROOT / "data/raw", ROOT / "data/clean", ROOT / "data/clean_sql"
REF = ROOT / "data/reference"
DB = Path(os.environ.get("SOD_DB", ROOT / "data/sod.db"))  # override only for testing
TABLES = {  # table -> key columns used to line rows up
    "fact_branch_year": ["year", "branch_id"],
    "dim_market": ["market_id"],
    "dim_county": ["county_fips"],
    "dim_owner": ["owner_id"],
    "dim_year": ["year"],
}
BOOL_COLS = {"is_main_office", "is_savings_institution", "is_focus"}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)

    # Step 1: load the raw files into SQLite.
    raw = pd.concat([pd.read_csv(f) for f in sorted(glob.glob(str(RAW / "sod_*.csv")))])
    raw.to_sql("raw_sod", con, if_exists="replace", index=False)
    pd.read_csv(RAW / "merger_events.csv").to_sql("raw_merger_events", con, if_exists="replace", index=False)
    review = pd.read_csv(REF / "booking_review.csv")[["branch_id", "decision"]]
    review.to_sql("ref_booking_review", con, if_exists="replace", index=False)
    adjust = pd.read_csv(REF / "booking_adjustments.csv")
    adjust.to_sql("ref_booking_adjustments", con, if_exists="replace", index=False)
    print(f"Step 1  Loaded raw_sod: {len(raw):,} rows; review decisions: {len(review)}; "
          f"booking-office rows: {len(adjust)}")

    # Steps 2-6: the SQL.
    con.executescript((ROOT / "sql/clean.sql").read_text(encoding="utf-8"))
    count = lambda t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    certs = con.execute("SELECT COUNT(DISTINCT CERT) FROM raw_sod").fetchone()[0]
    if count("cert_successor") != certs:
        sys.exit("A merger chain did not end (loop or depth over 50)")
    print(f"Step 2  Counties: {count('dim_county')}; markets: {count('dim_market')}")
    print(f"Step 3  Banks: {count('cert_successor'):,}; merged into another by 2026: "
          f"{con.execute('SELECT COUNT(*) FROM cert_successor WHERE cert <> final_cert').fetchone()[0]:,}")
    by_method = con.execute("""SELECT restate_method, COUNT(*) FROM fact_branch_year
                               GROUP BY restate_method ORDER BY restate_method""").fetchall()
    print("Step 4  Restated owner by method: " + "; ".join(f"{m}: {n:,}" for m, n in by_method))
    print(f"Step 5  Owners: {count('dim_owner'):,}")
    print(f"Step 6  Fact rows: {count('fact_branch_year'):,}; years: {count('dim_year')}")
    capped_rows = con.execute("SELECT COUNT(*) FROM fact_branch_year "
                              "WHERE deposits_capped_000 < deposits_000").fetchone()[0]
    print(f"Step 7  BPI cap applies in {count('capped_market')} markets; rows capped: {capped_rows:,}")
    by_status = con.execute("""SELECT booking_status, COUNT(*) FROM fact_branch_year
                               GROUP BY booking_status ORDER BY booking_status""").fetchall()
    print("Step 8-9  Branch-year rows by booking status: " + "; ".join(f"{s}: {n:,}" for s, n in by_status))
    d10_found = con.execute("SELECT COUNT(*) FROM fact_branch_year "
                            "WHERE booking_status = 'manual booking cap'").fetchone()[0]
    if d10_found != len(adjust):
        sys.exit(f"Only {d10_found} of {len(adjust)} booking-office rows found")

    # Step 10: export, then compare with clean.py's output table by table.
    all_match = True
    for table, keys in TABLES.items():
        sql_df = pd.read_sql(f"SELECT * FROM {table}", con)
        sql_df.to_csv(OUT / f"{table}.csv", index=False)
        py_df = pd.read_csv(CLEAN / f"{table}.csv")
        for df in (sql_df, py_df):
            for col in BOOL_COLS & set(df.columns):
                df[col] = df[col].astype(bool)
        sql_df = sql_df[py_df.columns].sort_values(keys).reset_index(drop=True)
        py_df = py_df.sort_values(keys).reset_index(drop=True)
        same_shape = sql_df.shape == py_df.shape
        diffs = (~((sql_df == py_df) | (sql_df.isna() & py_df.isna()))).sum().sum() if same_shape else None
        ok = same_shape and diffs == 0
        all_match &= ok
        print(f"Step 10  {'MATCH' if ok else 'DIFFER'}  {table}: Python {len(py_df):,} rows, "
              f"SQL {len(sql_df):,} rows, cells that differ: {diffs}")
    py_fact = pd.read_csv(CLEAN / "fact_branch_year.csv")
    sql_fact = pd.read_csv(OUT / "fact_branch_year.csv")
    same_totals = True
    for col in ["deposits_000", "deposits_capped_000", "deposits_adjusted_000"]:
        same = py_fact.groupby("year")[col].sum().equals(sql_fact.groupby("year")[col].sum())
        same_totals &= same
        print(f"Step 10  {'MATCH' if same else 'DIFFER'}  {col} by year")
    con.close()
    if not (all_match and same_totals):
        sys.exit("SQL output differs from clean.py output")
    print("Both versions produce identical tables.")


if __name__ == "__main__":
    main()
