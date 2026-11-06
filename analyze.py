"""Answer the business question from the clean star schema.

Business question: in which local markets is First Financial losing deposit
share to competitors, and where should it defend or expand?

Input:  data/clean/*.csv (from clean.py)
Output: reports/tables/*.csv, one table per step. Every step prints a row count.
Deposits are adjusted (BPI cap, review exclusions, D10) unless a column says raw.
Run from the project folder:  python analyze.py
"""
import sys
from pathlib import Path

import pandas as pd

CLEAN, OUT = Path("data/clean"), Path("reports/tables")
FOCUS = "HC-1071276"                 # First Financial Bancorp
FIRST, LAST = 2016, 2026
STRONG_RANK, STRONG_SHARE = 3, 10.0  # strong = top 3 or 10%+ share in LAST
FLAT_PTS = 0.25                      # organic change within +/-0.25 pts = flat
MERGER_YEAR = 2018                   # MainSource merger effective 2018-04-01


def say(step, text):
    print(f"Step {step}  {text}")


def shares(fact, value, owner):
    """Each owner's share (%) of each market-year, on the chosen deposit column and owner basis."""
    g = fact.groupby(["market_id", "year", owner])[value].sum().rename("dep").reset_index()
    g["mkt"] = g.groupby(["market_id", "year"])["dep"].transform("sum")
    g["share"] = (g["dep"] / g["mkt"] * 100).where(g["mkt"] > 0)
    return g.rename(columns={owner: "owner_id"})


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    fact = pd.read_csv(CLEAN / "fact_branch_year.csv")
    markets = pd.read_csv(CLEAN / "dim_market.csv").set_index("market_id")["market_name"]
    owners = pd.read_csv(CLEAN / "dim_owner.csv").set_index("owner_id")["owner_name"]
    ff_markets = sorted(fact.loc[(fact.restated_owner_id == FOCUS) | (fact.reported_owner_id == FOCUS), "market_id"].unique())
    fact = fact[fact.market_id.isin(ff_markets)]
    say(0, f"Markets where First Financial held a branch in any year: {len(ff_markets)}; branch-year rows: {len(fact):,}")

    # Step 1: share by year, three bases.
    org = shares(fact, "deposits_adjusted_000", "restated_owner_id")
    rep = shares(fact, "deposits_adjusted_000", "reported_owner_id")
    raw = shares(fact, "deposits_000", "restated_owner_id")
    grid = pd.MultiIndex.from_product([ff_markets, range(FIRST, LAST + 1)], names=["market_id", "year"]).to_frame(index=False)
    pick = lambda s, n: s[s.owner_id == FOCUS][["market_id", "year", "share"]].rename(columns={"share": n})
    trend = (grid.merge(org.groupby(["market_id", "year"]).mkt.first().rename("market_dep_000").reset_index(), how="left")
             .merge(org[org.owner_id == FOCUS][["market_id", "year", "dep"]].rename(columns={"dep": "ff_dep_000"}), how="left")
             .merge(pick(org, "share_organic"), how="left").merge(pick(rep, "share_reported_owner"), how="left")
             .merge(pick(raw, "share_organic_raw"), how="left"))
    for c in ["ff_dep_000", "share_organic", "share_reported_owner", "share_organic_raw"]:
        trend[c] = trend[c].fillna(0)
    trend.insert(1, "market", trend.market_id.map(markets))
    trend.to_csv(OUT / "share_by_year.csv", index=False)
    say(1, f"share_by_year.csv: {len(trend):,} market-years ({len(ff_markets)} markets x {LAST - FIRST + 1} years)")

    # Market summary used by later steps.
    t0, t1 = trend[trend.year == FIRST].set_index("market_id"), trend[trend.year == LAST].set_index("market_id")
    last = org[org.year == LAST]
    last = last[last.dep > 0].copy()
    last["rank"] = last.groupby("market_id")["dep"].rank(ascending=False, method="min")
    rank = last[last.owner_id == FOCUS].set_index("market_id")["rank"]
    ncomp = last.groupby("market_id").size()
    s = pd.DataFrame({"market": markets.reindex(ff_markets).values}, index=pd.Index(ff_markets, name="market_id"))
    s["share_2016"], s["share_2026"] = t0.share_organic, t1.share_organic
    s["organic_pts"] = s.share_2026 - s.share_2016
    s["acquired_pts"] = (t1.share_reported_owner - t0.share_reported_owner) - s.organic_pts
    s["organic_pts_raw"] = t1.share_organic_raw - t0.share_organic_raw
    s["ff_dep_2016_000"], s["ff_dep_2026_000"] = t0.ff_dep_000, t1.ff_dep_000
    s["market_dep_2016_000"], s["market_dep_2026_000"] = t0.market_dep_000, t1.market_dep_000
    s["rank_2026"], s["competitors_2026"] = rank, ncomp

    # Step 2: who gained where First Financial lost (organic change below -FLAT_PTS).
    losing = s[(s.organic_pts < -FLAT_PTS) & (s.ff_dep_2026_000 > 0)].index
    o0 = org[org.year == FIRST].set_index(["market_id", "owner_id"]).share
    o1 = org[org.year == LAST].set_index(["market_id", "owner_id"]).share
    gains = pd.concat([o0.rename("share_2016"), o1.rename("share_2026")], axis=1).fillna(0).reset_index()
    gains["change_pts"] = gains.share_2026 - gains.share_2016
    gains = gains[gains.market_id.isin(losing) & (gains.owner_id != FOCUS) & (gains.change_pts > 0)]
    gains = gains.sort_values(["market_id", "change_pts"], ascending=[True, False]).groupby("market_id").head(3)
    gains.insert(1, "market", gains.market_id.map(markets))
    gains.insert(3, "owner_name", gains.owner_id.map(owners))
    gains["ff_change_pts"] = gains.market_id.map(s.organic_pts)
    gains.to_csv(OUT / "competitor_gains.csv", index=False)
    say(2, f"competitor_gains.csv: {len(gains)} rows (top 3 gainers in {len(losing)} markets where First Financial lost > {FLAT_PTS} pts and still has branches)")

    # Step 3: before vs after the MainSource merger, by branch origin, in Indiana markets that shrank.
    ms_owner = owners[owners.str.contains("MAINSOURCE", case=False)].index
    origin = fact[fact.year < MERGER_YEAR].groupby("branch_id").reported_owner_id.last()
    ffb = fact[fact.restated_owner_id == FOCUS].copy()
    ffb["origin"] = ffb.branch_id.map(origin).map(lambda o: "MainSource" if o in ms_owner else "First Financial")
    ffb.loc[ffb.branch_id.map(origin).isna(), "origin"] = "Opened or acquired after 2017"
    watch = s[(s.organic_pts < -FLAT_PTS) & s.index.str.startswith("CTY-18")].index
    mkt = trend.set_index(["market_id", "year"]).market_dep_000
    ms = (ffb[ffb.market_id.isin(watch)].groupby(["market_id", "year", "origin"]).deposits_adjusted_000.sum()
          .rename("ff_dep_000").reset_index())
    ms["share"] = ms.ff_dep_000 / ms.set_index(["market_id", "year"]).index.map(mkt) * 100
    ms.insert(1, "market", ms.market_id.map(markets))
    ms.to_csv(OUT / "mainsource_origin_by_year.csv", index=False)
    pre = trend[trend.market_id.isin(watch)].pivot(index="market_id", columns="year", values="share_organic")
    say(3, f"mainsource_origin_by_year.csv: {len(ms)} rows for {len(watch)} Indiana county markets that lost share")

    # Step 4: exits. Every branch First Financial or its predecessors reported in an exited market.
    exited = s[s.ff_dep_2026_000 == 0].index
    fam = set(ms_owner) | {FOCUS}
    rows = []
    for mk in exited:
        d = fact[fact.market_id == mk]
        for b in d[d.reported_owner_id.isin(fam)].branch_id.unique():
            h = d[d.branch_id == b].sort_values("year")
            mine = h[h.reported_owner_id.isin(fam)]
            after = h[h.year > mine.year.max()]
            if len(after):
                status, to = f"sold in {after.year.min()}", owners.get(after.reported_owner_id.iloc[0])
            elif mine.year.max() == LAST:
                status, to = "still open, $0 deposits", ""
            else:
                status, to = f"closed after {mine.year.max()}", ""
            rows.append({"market_id": mk, "market": markets[mk], "branch_id": b, "years_held": f"{mine.year.min()}-{mine.year.max()}",
                         "last_deposits_000": mine.deposits_000.iloc[-1], "status": status, "new_owner": to})
    exits = pd.DataFrame(rows).sort_values(["market", "branch_id"])
    exits.to_csv(OUT / "exits.csv", index=False)
    say(4, f"exits.csv: {len(exits)} branches in {len(exited)} exited markets")

    # Step 5: market growth vs First Financial growth.
    s["market_growth_pct"] = (s.market_dep_2026_000 / s.market_dep_2016_000 - 1) * 100
    s["ff_growth_pct"] = (s.ff_dep_2026_000 / s.ff_dep_2016_000 - 1) * 100
    say(5, f"growth computed for {s.market_growth_pct.notna().sum()} markets; First Financial had no 2016 deposits in "
           f"{int((s.ff_dep_2016_000 == 0).sum())}")

    # Step 6: defend / hold / expand / watch / exited.
    strong = (s.rank_2026 <= STRONG_RANK) | (s.share_2026 >= STRONG_SHARE)
    up, down = s.organic_pts > FLAT_PTS, s.organic_pts < -FLAT_PTS
    s["group"] = "watch"
    s.loc[strong & down, "group"] = "defend"
    s.loc[strong & ~down, "group"] = "hold"
    s.loc[~strong & up, "group"] = "expand candidate"
    s.loc[~strong & ~up & ~down, "group"] = "flat, small"
    s.loc[s.ff_dep_2026_000 == 0, "group"] = "exited"
    s.reset_index().to_csv(OUT / "market_summary.csv", index=False)
    say(6, "market_summary.csv groups: " + "; ".join(f"{k}: {v}" for k, v in s.group.value_counts().items()))

    # Step 7: checks.
    sums = org.groupby(["market_id", "year"]).share.sum()
    checks = {
        "owner shares sum to 100% in every market-year": ((sums - 100).abs() < 1e-9).all(),
        "market deposits tie to the fact table": abs(trend.market_dep_000.sum() - fact.deposits_adjusted_000.sum()) < 1,
        "First Financial deposits tie to the fact table": abs(trend.ff_dep_000.sum()
            - fact.loc[fact.restated_owner_id == FOCUS, "deposits_adjusted_000"].sum()) < 1,
        "every market has a group": s.group.notna().all() and len(s) == len(ff_markets),
    }
    for name, ok in checks.items():
        say(7, f"{'PASS' if ok else 'FAIL'}  {name}")
    if not all(checks.values()):
        sys.exit("A check failed")


if __name__ == "__main__":
    main()
