"""Download FDIC Summary of Deposits (SOD) branch rows for the analysis.

Scope: every branch, from any bank, in every county that sits in a market
where First Financial Bancorp (holding company RSSDHCR 1071276) or a bank
that later merged into it had a branch, for every year 2016-2026.
Pulling by county (not by each year's MSA code) keeps every county in every
year, because MSA codes change during the window (README, Decisions).

Source: FDIC BankFind API, https://api.fdic.gov/banks/sod and /history
Output: data/raw/merger_events.csv, data/raw/ffbc_branches.csv,
        data/raw/sod_YYYY.csv
Every step prints a row count.
"""
import csv
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

SOD_API = "https://api.fdic.gov/banks/sod"
HISTORY_API = "https://api.fdic.gov/banks/history"
FOCUS_HCR = 1071276            # First Financial Bancorp
YEARS = range(2016, 2027)      # June 30, 2016 through June 30, 2026
PAGE = 10_000                  # API maximum rows per call
RAW = Path("data/raw")

FIELDS = [
    "YEAR", "UNINUMBR", "BRNUM", "BKMO", "BRSERTYP",
    "CERT", "RSSDID", "NAMEFULL", "BKCLASS",
    "RSSDHCR", "NAMEHCR",
    "DEPSUMBR",
    "STALPBR", "STCNTYBR", "CNTYNAMB", "CITYBR", "ZIPBR",
    "MSABR", "MSANAMB", "METROBR", "MICROBR",
]

# Whole-institution events that move every deposit of one bank (OUT_CERT)
# into another (SUR_CERT). Codes and names from the FDIC history endpoint.
MERGER_CODES = {
    211: "Failure - Whole Institution",
    216: "Bridge Bank Resolution",
    221: "Absorption - Without Assistance",
    222: "Consolidated - Without Assistance",
    223: "Merger - Without Assistance",
    224: "Affiliated Institution Merger (corporate reorganization)",
}
HISTORY_FIELDS = [
    "EFFDATE", "CHANGECODE", "CHANGECODE_DESC",
    "OUT_CERT", "OUT_INSTNAME", "SUR_CERT", "SUR_INSTNAME",
    "ACQ_CERT", "ACQ_INSTNAME",
]


def fetch_all(filters, url=SOD_API, fields=FIELDS, sort_by="UNINUMBR"):
    """Return every row matching an API filter, paging past the 10,000-row cap."""
    rows, offset = [], 0
    while True:
        params = {
            "filters": filters,
            "fields": ",".join(fields),
            "limit": PAGE,
            "offset": offset,
            "sort_by": sort_by,
            "sort_order": "ASC",
            "format": "json",
        }
        for attempt in range(3):
            resp = requests.get(url, params=params, timeout=60)
            if resp.ok:
                break
            time.sleep(2 * (attempt + 1))
        resp.raise_for_status()
        body = resp.json()
        total = body["meta"]["total"]
        rows.extend(item["data"] for item in body["data"])
        offset += PAGE
        if offset >= total:
            break
    if len(rows) != total:
        sys.exit(f"Row count mismatch for {filters!r}: got {len(rows)}, API says {total}")
    return rows


def write_csv(path, rows, fields=FIELDS):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def or_list(field, values):
    return f"{field}:(" + " OR ".join(str(v) for v in sorted(values)) + ")"


def chunks(values, size=150):
    """Split a long OR-list into several API calls so URLs stay short."""
    values = sorted(values)
    return [values[i:i + size] for i in range(0, len(values), size)]


def final_survivor(cert, successor):
    """Follow OUT_CERT -> SUR_CERT links to the end of the chain."""
    seen = set()
    while cert in successor:
        if cert in seen:
            sys.exit(f"Merger chain loops at CERT {cert}")
        seen.add(cert)
        cert = successor[cert]
    return cert


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    window = f'EFFDATE:["{YEARS[0]}-07-01" TO "{YEARS[-1]}-06-30"]'

    # Step 1: whole-institution mergers and failures inside the window.
    events = fetch_all(f"{or_list('CHANGECODE', MERGER_CODES)} AND {window}",
                       url=HISTORY_API, fields=HISTORY_FIELDS, sort_by="EFFDATE")
    write_csv(RAW / "merger_events.csv", events, HISTORY_FIELDS)
    print(f"Step 1  Merger and failure events, {YEARS[0]}-07-01 to {YEARS[-1]}-06-30: {len(events):,}")

    # Step 2: First Financial's branches, plus branches of every bank that
    # merged into it during the window (their pre-deal years).
    years = f"YEAR:[{YEARS[0]} TO {YEARS[-1]}]"
    ffbc = fetch_all(f"RSSDHCR:{FOCUS_HCR} AND {years}")
    ffbc_certs = {int(r["CERT"]) for r in ffbc}
    successor = {int(e["OUT_CERT"]): int(e["SUR_CERT"]) for e in events}
    merged_in = {c for c in successor if final_survivor(c, successor) in ffbc_certs}
    predecessors = []
    for part in chunks(merged_in):
        predecessors += fetch_all(f"{or_list('CERT', part)} AND {years}")
    focus = ffbc + [r for r in predecessors if int(r["CERT"]) not in ffbc_certs]
    write_csv(RAW / "ffbc_branches.csv", focus)
    print(f"Step 2  First Financial branch rows: {len(ffbc):,}; banks merged into it: "
          f"{len(merged_in)}; their pre-deal branch rows: {len(focus) - len(ffbc):,}; "
          f"total: {len(focus):,}")

    # Step 3: find every county in those markets. A market is an MSA code
    # (any year) or a county. Nothing is saved from this pass.
    msas = {int(r["MSABR"]) for r in focus if r.get("MSABR") and int(r["MSABR"]) > 0}
    counties = {int(r["STCNTYBR"]) for r in focus if r.get("STCNTYBR")}
    discovered = set(counties)
    for part in chunks(msas):
        for r in fetch_all(f"{years} AND {or_list('MSABR', part)}", fields=["STCNTYBR"]):
            discovered.add(int(r["STCNTYBR"]))
    print(f"Step 3  Markets touched: {len(msas)} MSA codes, {len(counties)} focus counties; "
          f"counties to pull: {len(discovered)}")

    # Step 4: every branch, from any bank, in those counties, every year.
    print("Step 4  All branches in those counties, by year:")
    years_by_county = defaultdict(set)
    for year in YEARS:
        rows = []
        for part in chunks(discovered):
            rows += fetch_all(f"YEAR:{year} AND {or_list('STCNTYBR', part)}")
        for r in rows:
            years_by_county[int(r["STCNTYBR"])].add(year)
        write_csv(RAW / f"sod_{year}.csv", rows)
        print(f"        {year}: {len(rows):,} rows")

    # Step 5: counties without branches in every year. With a county-based
    # pull, these are real gaps (no branches that year), not pull gaps.
    short = {c: sorted(set(YEARS) - ys) for c, ys in years_by_county.items() if len(ys) < len(YEARS)}
    print(f"Step 5  Counties with branches in fewer than {len(YEARS)} years: {len(short)}")
    for c, missing in sorted(short.items()):
        print(f"        {c}: no branches in {missing}")


if __name__ == "__main__":
    main()
