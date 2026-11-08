"""Build the star schema for Power BI from the raw FDIC files.

Input:  data/raw/sod_YYYY.csv, data/raw/merger_events.csv  (from download.py)
        data/reference/booking_review.csv     (review decisions, from review_booking.py)
        data/reference/booking_adjustments.csv (manual booking caps: D10, O17, O19)
Output: data/clean/fact_branch_year.csv
        data/clean/dim_market.csv, data/clean/dim_county.csv
        data/clean/dim_owner.csv, data/clean/dim_year.csv
        data/reference/cert_successor.csv
Every step prints a row count. Decisions referenced (D1...) are in the README.

Deposit columns in the fact table:
  deposits_000           as reported to the FDIC
  deposits_capped_000    after the BPI cap (D9)
  deposits_adjusted_000  after the cap, the review exclusions (D9) and the manual booking caps
"""
import glob
import sys
from pathlib import Path

import pandas as pd

RAW, CLEAN, REF = Path("data/raw"), Path("data/clean"), Path("data/reference")
FOCUS_OWNER = "HC-1071276"          # First Financial Bancorp
LAST_YEAR = 2026
SAVINGS_CLASSES = {"SB", "SL", "SA", "SI"}  # savings institutions (BKCLASS, O11)
CAP_MULTIPLE, CAP_PERCENTILE, CAP_MIN_YEARS = 3, 0.99, 9  # BPI cap, 9 of 11 years (D9)


def say(step, text):
    print(f"Step {step}  {text}")


def owner_id(rssdhcr, cert):
    """Holding company if there is one, otherwise the bank itself (D2)."""
    return f"HC-{int(rssdhcr)}" if pd.notna(rssdhcr) and int(rssdhcr) > 0 else f"CERT-{int(cert)}"


def main():
    CLEAN.mkdir(parents=True, exist_ok=True)
    REF.mkdir(parents=True, exist_ok=True)

    # Step 1: load the raw yearly files.
    files = sorted(glob.glob(str(RAW / "sod_*.csv")))
    raw = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    raw["MSABR"] = raw["MSABR"].fillna(0).astype(int)
    raw["STCNTYBR"] = raw["STCNTYBR"].astype(int)
    raw["DEPSUMBR"] = raw["DEPSUMBR"].fillna(0)
    say(1, f"Raw branch-year rows loaded from {len(files)} files: {len(raw):,}")
    if raw.duplicated(["YEAR", "UNINUMBR"]).any():
        sys.exit("Duplicate YEAR + UNINUMBR rows in raw data")

    # Step 2: one fixed county -> market map, taken from each county's latest
    # year (2026 for every county that has 2026 branches) (D1, D5).
    latest = raw.sort_values("YEAR").groupby("STCNTYBR").tail(1)
    dim_county = pd.DataFrame({
        "county_fips": latest["STCNTYBR"],
        "county_name": latest["CNTYNAMB"] + ", " + latest["STALPBR"],
        "state": latest["STALPBR"],
        "map_year": latest["YEAR"],
        "market_id": [f"MSA-{m}" if m > 0 else f"CTY-{c}"
                      for m, c in zip(latest["MSABR"], latest["STCNTYBR"])],
        "market_name": [n if m > 0 else f"{cn}, {st} (county)"
                        for m, n, cn, st in zip(latest["MSABR"], latest["MSANAMB"],
                                                latest["CNTYNAMB"], latest["STALPBR"])],
    }).sort_values("county_fips").reset_index(drop=True)
    fallback = int((dim_county["map_year"] != LAST_YEAR).sum())
    dim_market = (dim_county.groupby(["market_id", "market_name"])
                  .agg(market_type=("market_id", lambda s: "MSA" if s.iloc[0].startswith("MSA") else "County"),
                       states=("state", lambda s: ", ".join(sorted(set(s)))),
                       counties=("county_fips", "count"))
                  .reset_index())
    say(2, f"Counties: {len(dim_county)} ({fallback} mapped from a year before {LAST_YEAR}); "
           f"markets: {len(dim_market)} ({(dim_market.market_type == 'MSA').sum()} MSA, "
           f"{(dim_market.market_type == 'County').sum()} county)")
    codes = raw.groupby("STCNTYBR")["MSABR"].apply(set)
    switched = int(codes.map(lambda c: len(c - {0}) > 1).sum())
    in_out = int(codes.map(lambda c: 0 in c and len(c - {0}) == 1).sum())
    say(2, f"Counties whose MSA status changed during the window: {switched + in_out} "
           f"({switched} switched metro code, {in_out} moved into or out of a metro area)")

    # Step 3: every bank -> the bank holding it in 2026, following merger
    # chains to their end (D6).
    events = pd.read_csv(RAW / "merger_events.csv")
    successor = dict(zip(events["OUT_CERT"].astype(int), events["SUR_CERT"].astype(int)))

    def final(cert):
        seen = set()
        while cert in successor:
            if cert in seen:
                sys.exit(f"Merger chain loops at CERT {cert}")
            seen.add(cert)
            cert = successor[cert]
        return cert

    certs = sorted(raw["CERT"].astype(int).unique())
    ref = pd.DataFrame({"cert": certs, "final_cert": [final(c) for c in certs]})
    ref.to_csv(REF / "cert_successor.csv", index=False)
    say(3, f"Banks in data: {len(ref):,}; merged into another bank by {LAST_YEAR}: "
           f"{(ref.cert != ref.final_cert).sum():,}")

    # Step 4: reported owner = who owned the branch that year.
    raw["reported_owner_id"] = [owner_id(h, c) for h, c in zip(raw["RSSDHCR"], raw["CERT"])]
    raw["owner_name"] = raw["NAMEHCR"].where(raw["RSSDHCR"].fillna(0) > 0, raw["NAMEFULL"])

    # Step 4: restated owner = the 2026 owner (D2, D6).
    certs_2026 = set(raw.loc[raw["YEAR"] == LAST_YEAR, "CERT"].astype(int))
    now = raw[raw["YEAR"] == LAST_YEAR]
    branch_2026 = dict(zip(now["UNINUMBR"], now["reported_owner_id"]))
    # latest owner seen for each bank, used for banks at the end of a chain
    by_cert = raw.sort_values("YEAR").groupby("CERT").tail(1)
    cert_latest_owner = dict(zip(by_cert["CERT"].astype(int), by_cert["reported_owner_id"]))
    last_row = raw.sort_values("YEAR").groupby("UNINUMBR").tail(1)
    final_of = dict(zip(ref.cert, ref.final_cert))

    method, restated = {}, {}
    for u, cert, own in zip(last_row["UNINUMBR"], last_row["CERT"].astype(int), last_row["reported_owner_id"]):
        if u in branch_2026:
            restated[u], method[u] = branch_2026[u], "open in 2026"
        elif final_of[cert] != cert and final_of[cert] in cert_latest_owner:
            restated[u], method[u] = cert_latest_owner[final_of[cert]], "merger chain"
        elif cert in certs_2026:
            restated[u], method[u] = own, "closed, bank still operating"
        else:
            restated[u], method[u] = own, "closed, no successor in data"
    raw["restated_owner_id"] = raw["UNINUMBR"].map(restated)
    raw["restate_method"] = raw["UNINUMBR"].map(method)
    counts = raw.groupby("restate_method").size()
    say(4, "Restated owner by method (branch-year rows): "
           + "; ".join(f"{k}: {v:,}" for k, v in counts.items()))

    owners = (raw[["reported_owner_id", "owner_name", "YEAR"]]
              .rename(columns={"reported_owner_id": "owner_id"})
              .sort_values("YEAR").groupby("owner_id").tail(1))
    dim_owner = owners[["owner_id", "owner_name"]].copy()
    dim_owner["owner_type"] = dim_owner["owner_id"].str[:2].map({"HC": "Holding company", "CE": "Bank (no holding company)"})
    dim_owner["is_focus"] = dim_owner["owner_id"].eq(FOCUS_OWNER)
    dim_owner = dim_owner.sort_values("owner_id").reset_index(drop=True)
    say(5, f"Owners: {len(dim_owner):,} ({int(dim_owner.is_focus.sum())} focus: "
           f"{dim_owner.loc[dim_owner.is_focus, 'owner_name'].iloc[0]})")

    # Step 6: the fact table.
    county_market = dict(zip(dim_county["county_fips"], dim_county["market_id"]))
    fact = pd.DataFrame({
        "year": raw["YEAR"].astype(int),
        "branch_id": raw["UNINUMBR"].astype(int),
        "cert": raw["CERT"].astype(int),
        "county_fips": raw["STCNTYBR"],
        "market_id": raw["STCNTYBR"].map(county_market),
        "deposits_000": raw["DEPSUMBR"],
        "is_main_office": raw["BKMO"].eq(1),
        "bank_class": raw["BKCLASS"],
        "is_savings_institution": raw["BKCLASS"].isin(SAVINGS_CLASSES),
        "reported_owner_id": raw["reported_owner_id"],
        "restated_owner_id": raw["restated_owner_id"],
        "restate_method": raw["restate_method"],
    }).sort_values(["year", "branch_id"]).reset_index(drop=True)
    dim_year = pd.DataFrame({"year": sorted(fact["year"].unique())})
    say(6, f"Fact rows: {len(fact):,}; years: {len(dim_year)}")

    # Step 7: BPI cap. Each branch-year above 3x its market-year 99th
    # percentile is cut to that cap, only in markets where it binds in 9+ years (D9).
    p99 = fact.groupby(["market_id", "year"])["deposits_000"].transform(lambda s: s.quantile(CAP_PERCENTILE))
    cap = (CAP_MULTIPLE * p99).round(3)  # 3 decimals so SQLite and pandas agree to the last digit
    binds = fact["deposits_000"] > cap
    years_binding = fact[binds].groupby("market_id")["year"].nunique()
    capped_markets = sorted(years_binding[years_binding >= CAP_MIN_YEARS].index)
    capped = binds & fact["market_id"].isin(capped_markets)
    fact["deposits_capped_000"] = fact["deposits_000"].where(~capped, cap)
    names = dim_market.set_index("market_id")["market_name"]
    total = fact["deposits_000"].sum()

    def removed(amount):
        return f"${amount / 1e6:,.1f}B summed over all years ({amount / total:.1%} of reported deposits)"

    say(7, f"BPI cap applies in {len(capped_markets)} markets ({'; '.join(names[m] for m in capped_markets)}); "
           f"rows capped: {int(capped.sum()):,}; removed: {removed((fact.deposits_000 - fact.deposits_capped_000).sum())}")

    # Step 8: review decisions for flagged branches (D9). An excluded branch
    # is removed in every year.
    review = pd.read_csv(REF / "booking_review.csv")
    decision = dict(zip(review["branch_id"], review["decision"]))
    status_of = {"exclude": "excluded", "keep": "kept"}
    fact["booking_status"] = [status_of.get(decision.get(b), "not reviewed") if b in decision else "not flagged"
                              for b in fact["branch_id"]]
    excluded = fact["booking_status"].eq("excluded")
    fact["deposits_adjusted_000"] = fact["deposits_capped_000"].where(~excluded, 0)
    by_status = review["decision"].map(lambda d: status_of.get(d, "not reviewed")).value_counts()
    say(8, f"Flagged branches: {len(review)} = " + " + ".join(f"{by_status.get(s, 0)} {s}" for s in
           ["excluded", "kept", "not reviewed"]) + f"; excluded branch-year rows: {int(excluded.sum()):,}; "
           f"removed: {removed(fact.loc[excluded, 'deposits_capped_000'].sum())}")

    # Step 9: manual booking caps (D10 First Financial in Cincinnati; O19 Merchants
    # in Indianapolis). Each listed
    # branch-year keeps at most max_deposits_000.
    adjust = pd.read_csv(REF / "booking_adjustments.csv")
    limit = fact[["year", "branch_id"]].merge(adjust[["year", "branch_id", "max_deposits_000"]],
                                              on=["year", "branch_id"], how="left")["max_deposits_000"]
    hit = limit.notna()
    before = fact["deposits_adjusted_000"].copy()
    fact["deposits_adjusted_000"] = fact["deposits_adjusted_000"].where(~hit, before.clip(upper=limit))
    fact.loc[hit, "booking_status"] = "manual booking cap"
    say(9, f"Manual booking-cap rows: {int(hit.sum())} of {len(adjust)} listed; "
           f"removed: {removed((before - fact.deposits_adjusted_000).sum())}")

    # Step 10: checks. Any failure stops the script.
    checks = {
        "every row has a market": fact["market_id"].notna().all(),
        "every market is in dim_market": fact["market_id"].isin(dim_market["market_id"]).all(),
        "every reported owner is in dim_owner": fact["reported_owner_id"].isin(dim_owner["owner_id"]).all(),
        "every restated owner is in dim_owner": fact["restated_owner_id"].isin(dim_owner["owner_id"]).all(),
        "row count matches raw": len(fact) == len(raw),
        "deposits by year match raw": fact.groupby("year")["deposits_000"].sum()
            .equals(raw.groupby("YEAR")["DEPSUMBR"].sum().rename_axis("year")),
        "no negative deposits": (fact["deposits_000"] >= 0).all(),
        "every manual booking-cap row found": int(hit.sum()) == len(adjust),
        "every reviewed branch is in the data": review["branch_id"].isin(fact["branch_id"]).all(),
        "adjusted <= capped <= reported": ((fact.deposits_adjusted_000 <= fact.deposits_capped_000)
                                           & (fact.deposits_capped_000 <= fact.deposits_000)).all(),
    }
    for name, ok in checks.items():
        say(10, f"{'PASS' if ok else 'FAIL'}  {name}")
    if not all(checks.values()):
        sys.exit("A check failed; nothing written to data/clean")

    fact.to_csv(CLEAN / "fact_branch_year.csv", index=False)
    dim_market.to_csv(CLEAN / "dim_market.csv", index=False)
    dim_county.to_csv(CLEAN / "dim_county.csv", index=False)
    dim_owner.to_csv(CLEAN / "dim_owner.csv", index=False)
    dim_year.to_csv(CLEAN / "dim_year.csv", index=False)
    say(11, f"Wrote 5 tables to {CLEAN}/ and cert_successor.csv to {REF}/")


if __name__ == "__main__":
    main()
