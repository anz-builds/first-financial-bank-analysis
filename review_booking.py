"""Flag and review branches whose deposits look centrally booked (decision D9).

Input:  data/clean/fact_branch_year.csv and the dimension tables (from clean.py)
        data/raw/sod_*.csv (service type) and data/raw/merger_events.csv
        data/reference/booking_overrides.csv (manual decisions with sources)
Output: data/reference/booking_review.csv, one row per flagged branch.

Run from the project folder:  python review_booking.py
Rerun it only after new raw data: clean.py, then this script, then clean.py
again so the fact table picks up the new decisions.

How it works
1. Use deposits after the BPI cap (`deposits_capped_000`, built in clean.py).
2. Flag a branch when, in any year, it is over 10x its market's median branch,
   or it moves by 5%+ of its market's deposits from one year to the next.
3. Review a flagged branch only if removing it moves First Financial's
   2016->2026 share change in its market by at least 0.5 pts (O7).
4. A reviewed branch is excluded when any rule below fires, kept otherwise.
   Drops and spikes count only when the deposits cross markets: the same
   owner's other branches in that market absorbed less than half the move (O8).
   - headquarters drop: a main office falls to half or less as its bank merges,
     changes owner, or stops being the main office
   - ownership-change drop: any branch falls to half or less when its owner changes
   - spike and fall: rises 50%+ in a year while the rest of the market moves
     under 20%, then falls to half or less of the peak
   - out of scale: holds 25%+ of market deposits with 10% or fewer of its branches
5. Manual overrides replace the rule result for reviewed branches.
"""
import glob
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
FOCUS_OWNER = "HC-1071276"        # First Financial Bancorp
FIRST_YEAR, LAST_YEAR = 2016, 2026

SWING_SHARE = 0.05                # one-year move of 5%+ of market deposits
MEDIAN_MULTIPLE = 10              # over 10x the market's median branch
REVIEW_CUTOFF_PTS = 0.5           # minimum impact on the focus share change
DROP_RATIO = 0.5                  # "falls to half or less"
SPIKE_RATIO = 1.5                 # "rises 50% or more"
STABLE_MARKET = 0.2               # rest of market moves under 20%
ABSORBED_LIMIT = 0.5              # same-market absorption below this = crossed markets
SCALE_SHARE, SCALE_BRANCHES = 0.25, 0.10


def load():
    """Fact table with service type, merger events, market and owner names, overrides."""
    fact = pd.read_csv(ROOT / "data/clean/fact_branch_year.csv")
    service = pd.concat(
        [pd.read_csv(p, usecols=["YEAR", "UNINUMBR", "BRSERTYP"])
         for p in glob.glob(str(ROOT / "data/raw/sod_*.csv"))]
    ).rename(columns={"YEAR": "year", "UNINUMBR": "branch_id"})
    fact = (fact.merge(service, on=["year", "branch_id"], how="left")
                .sort_values(["branch_id", "year"]).reset_index(drop=True))
    events = pd.read_csv(ROOT / "data/raw/merger_events.csv")
    events["effective"] = pd.to_datetime(events["EFFDATE"])
    markets = pd.read_csv(ROOT / "data/clean/dim_market.csv").set_index("market_id")
    owners = pd.read_csv(ROOT / "data/clean/dim_owner.csv").set_index("owner_id")
    overrides = pd.read_csv(ROOT / "data/reference/booking_overrides.csv")
    overrides = {int(r.branch_id): (r.decision, r.reason) for r in overrides.itertuples()}
    return fact, events, markets, owners, overrides


def add_market_context(fact):
    """Capped deposits as `dep`, plus market totals, medians and branch counts."""
    fact["dep"] = fact["deposits_capped_000"]
    by_market_year = fact.groupby(["market_id", "year"])
    fact["mkt_tot"] = by_market_year.dep.transform("sum")
    fact["mkt_med"] = by_market_year.dep.transform("median")
    fact["mkt_branches"] = by_market_year.branch_id.transform("count")
    fact["owner_branches"] = (fact.groupby(["market_id", "year", "reported_owner_id"])
                                  .branch_id.transform("count"))
    return fact


def flag_branches(fact):
    """Branch IDs that are out of scale or swing by 5%+ of their market in any year."""
    prev_dep = fact.groupby("branch_id").dep.shift().fillna(0)
    next_dep = fact.groupby("branch_id").dep.shift(-1).fillna(0)
    swing = (((fact.dep - prev_dep).abs() / fact.mkt_tot >= SWING_SHARE)
             | ((next_dep - fact.dep).abs() / fact.mkt_tot >= SWING_SHARE))
    oversized = fact.dep > MEDIAN_MULTIPLE * fact.mkt_med.where(fact.mkt_med > 0)
    return fact[swing | oversized].branch_id.unique()


def focus_share_change(rows):
    """First Financial's restated share in FIRST_YEAR -> LAST_YEAR, in points."""
    share = rows.groupby("year").apply(
        lambda x: x.loc[x.restated_owner_id == FOCUS_OWNER, "dep"].sum() / x.dep.sum() * 100,
        include_groups=False)
    return share.get(LAST_YEAR, 0) - share.get(FIRST_YEAR, 0)


def impact_by_branch(fact, flagged, market_of):
    """How far removing each flagged branch moves the focus share change in its market."""
    base = {mk: focus_share_change(rows) for mk, rows in fact.groupby("market_id")}
    impact = {}
    for b in flagged:
        mk = market_of[b]
        without_b = fact[(fact.market_id == mk) & (fact.branch_id != b)]
        impact[b] = abs(focus_share_change(without_b) - base[mk])
    return impact


class Rules:
    """The four review rules, with the crossed-markets test they share (O8)."""

    def __init__(self, fact, events):
        self.fact, self.events = fact, events

    def absorbed_in_market(self, branch, market, owners, year, delta):
        """Share of a branch's change that the same owners' other branches in the
        same market absorbed (moving the opposite way)."""
        f = self.fact
        others = f[(f.market_id == market) & (f.branch_id != branch) & f.reported_owner_id.isin(owners)]
        change = others.loc[others.year == year, "dep"].sum() - others.loc[others.year == year - 1, "dep"].sum()
        return max(0.0, -change / delta) if delta else 0.0

    def crosses_markets(self, branch, market, rows, i):
        before, after = rows.iloc[i - 1], rows.iloc[i]
        owners = {before.reported_owner_id, after.reported_owner_id}
        delta = after.dep - before.dep
        return self.absorbed_in_market(branch, market, owners, after.year, delta) < ABSORBED_LIMIT

    def merged_out(self, cert, year):
        """True if the bank merged out between the year-1 and year surveys (June 30)."""
        e = self.events
        return ((e.OUT_CERT == cert)
                & (e.effective > pd.Timestamp(f"{year - 1}-06-30"))
                & (e.effective <= pd.Timestamp(f"{year}-06-30"))).any()

    def drops(self, branch, market, rows):
        """Headquarters drop and ownership-change drop. The last match wins."""
        hq_drop = owner_drop = None
        for i in range(1, len(rows)):
            before, after = rows.iloc[i - 1], rows.iloc[i]
            if after.year != before.year + 1 or after.dep > DROP_RATIO * before.dep:
                continue
            owner_changed = (self.merged_out(before.cert, after.year)
                             or after.cert != before.cert
                             or after.reported_owner_id != before.reported_owner_id)
            if not self.crosses_markets(branch, market, rows, i):
                continue
            if before.is_main_office and (owner_changed or not after.is_main_office):
                hq_drop = (f"main office fell {before.dep / 1000:.0f}M -> {after.dep / 1000:.0f}M "
                           f"in {after.year} as bank merged/changed")
            elif owner_changed:
                owner_drop = (f"fell {before.dep / 1000:.0f}M -> {after.dep / 1000:.0f}M "
                              f"in {after.year} when owner changed")
        return hq_drop, owner_drop

    def spike_and_fall(self, branch, market, rows):
        """A one-year spike in a steady market, later followed by a real drop."""
        dep, year = rows.dep, rows.year
        rest = (rows.mkt_tot - rows.dep).values

        def consecutive(i):
            return year[i] == year[i - 1] + 1

        def market_steady(i):
            return abs(rest[i] / rest[i - 1] - 1) < STABLE_MARKET

        for i in range(1, len(rows)):
            spiked = (consecutive(i) and dep[i - 1] > 0 and dep[i] >= SPIKE_RATIO * dep[i - 1]
                      and market_steady(i) and self.crosses_markets(branch, market, rows, i))
            if not spiked:
                continue
            for j in range(i + 1, len(rows)):
                fell = (consecutive(j) and dep[j] <= DROP_RATIO * dep[i] and dep[j] < dep[j - 1]
                        and market_steady(j) and self.crosses_markets(branch, market, rows, j))
                if fell:
                    return (f"rose {dep[i - 1] / 1000:.0f}M -> {dep[i] / 1000:.0f}M in {year[i]}, "
                            f"fell to {dep[j] / 1000:.0f}M by {year[j]}; rest of market moved <20%")
        return None

    @staticmethod
    def out_of_scale(rows):
        hits = rows[(rows.dep / rows.mkt_tot >= SCALE_SHARE)
                    & (rows.owner_branches / rows.mkt_branches <= SCALE_BRANCHES)]
        if not len(hits):
            return None
        r = hits.iloc[0]
        return (f"{r.year}: {r.dep / r.mkt_tot:.0%} of market deposits from "
                f"{int(r.owner_branches)} of {int(r.mkt_branches)} branches")


def review(fact, events, markets, owners, overrides):
    flagged = flag_branches(fact)
    market_of = fact.groupby("branch_id").market_id.first()
    impact = impact_by_branch(fact, flagged, market_of)
    rules = Rules(fact, events)

    records = []
    for b in flagged:
        rows = fact[fact.branch_id == b].sort_values("year").reset_index(drop=True)
        mk = market_of[b]
        reviewed = impact[b] >= REVIEW_CUTOFF_PTS
        hq_drop, owner_drop = rules.drops(b, mk, rows)
        spike = rules.spike_and_fall(b, mk, rows)
        scale = rules.out_of_scale(rows)
        fired = [name for name, hit in [("headquarters drop", hq_drop), ("ownership-change drop", owner_drop),
                                        ("spike and fall", spike), ("out of scale", scale)] if hit]
        if reviewed:
            decision = "exclude" if fired else "keep"
            if b in overrides:
                decision = overrides[b][0]
                fired = fired + ["manual override: " + overrides[b][1]]
        else:
            decision = f"not reviewed (impact < {REVIEW_CUTOFF_PTS} pts)"
        records.append({
            "branch_id": b,
            "market_id": mk,
            "market": markets.loc[mk, "market_name"],
            "owners": " > ".join(dict.fromkeys(rows.reported_owner_id.map(owners.owner_name).str[:30])),
            "impact_pts": round(impact[b], 2),
            "reviewed": reviewed,
            "deposits_by_year_$M": "; ".join(f"{y}:{v / 1000:.0f}" for y, v in zip(rows.year, rows.dep)),
            "main_office_any_year": bool(rows.is_main_office.any()),
            "service_types": ",".join(sorted({str(int(x)) for x in rows.BRSERTYP.dropna()})),
            "decision": decision,
            "criterion": ", ".join(fired) if reviewed else "",
            "evidence": " | ".join(v for v in [hq_drop, owner_drop, spike, scale] if v) if reviewed else "",
        })
    return pd.DataFrame(records).sort_values(["decision", "impact_pts"], ascending=[True, False])


def main():
    fact, events, markets, owners, overrides = load()
    fact = add_market_context(fact)
    result = review(fact, events, markets, owners, overrides)
    result.to_csv(ROOT / "data/reference/booking_review.csv", index=False)
    print(result.decision.value_counts().to_string())
    print("total flagged:", len(result))
    excluded = result[result.decision == "exclude"]
    print(excluded[["market", "owners", "impact_pts", "criterion", "evidence"]]
          .to_string(index=False, max_colwidth=90))


if __name__ == "__main__":
    main()
