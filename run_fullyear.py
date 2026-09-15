"""
run_fullyear.py
===============
EXACT full-year realised B6 redispatch expenditure (FY 2024/25) — computed for
every one of the 365 settlement days, not a 30-day stratified sample. This answers
reviewer 2's main empirical concern: since BMRS is public, compute the whole year.

For each day it sums the realised redispatch *expenditure* on SO-flagged Scottish-
wind curtailment (the socialised transfer), split into its two legs:
    expenditure = curtailment payment (paid to turn wind down)
                + replacement leg    (priced at the period wholesale/MID)
Directly observed: accepted MWh (BOALF level - PN). Constructed from prices x
quantities: the GBP legs (bid from BOD, replacement from MID). Nothing is modelled.

Robust to a long network run: appends each day to the CSV immediately and skips
days already done, so it can be re-run to resume.

    python run_fullyear.py            # compute/resume
    python run_fullyear.py --summary  # just print the annual summary from the CSV
"""
from __future__ import annotations
import sys
import csv
from pathlib import Path
import pandas as pd

import gb_empirical_pipeline as ep

FY = "2024-2025"
CAL = Path(f"neso_daily_{FY}.csv")        # the 365-day calendar + NESO daily cost
OUT = Path(f"fullyear_{FY}.csv")
COLS = ["date", "R_cong_obs", "curtail_payment", "replacement",
        "curtailed_MWh", "binding_periods"]


def _done() -> set[str]:
    if not OUT.exists():
        return set()
    return set(pd.read_csv(OUT)["date"].astype(str))


def summary() -> None:
    df = pd.read_csv(OUT).drop_duplicates("date")
    neso = pd.read_csv(CAL)
    annual_neso = float(neso["neso"].sum())
    exp = df["R_cong_obs"].sum()
    cur = df["curtail_payment"].sum()
    rep = df["replacement"].sum()
    vol = df["curtailed_MWh"].sum()
    ndays = len(df)
    print(f"\n=== Full-year realised B6 redispatch expenditure, FY{FY} "
          f"({ndays}/365 days) ===")
    print(f"  Redispatch EXPENDITURE (socialised transfer): GBP{exp/1e6:8.1f}m")
    print(f"    of which curtailment payment (turn wind down): GBP{cur/1e6:7.1f}m "
          f"({100*cur/exp:.0f}%)")
    print(f"    of which replacement leg (priced at MID)     : GBP{rep/1e6:7.1f}m "
          f"({100*rep/exp:.0f}%)")
    print(f"  BM-visible Scottish-wind curtailment          : {vol/1e6:.2f} TWh")
    print(f"  NESO published system constraint cost (FY)    : GBP{annual_neso/1e6:.0f}m")
    print(f"  B6 expenditure as share of NESO system total  : {100*exp/annual_neso:.0f}%"
          f"   (consistency check, NOT independent validation)")
    if ndays < 365:
        print(f"  NOTE: {365-ndays} day(s) not yet computed — re-run to complete.")


def run() -> None:
    dates = list(pd.read_csv(CAL)["date"].astype(str))
    done = _done()
    todo = [d for d in dates if d not in done]
    print(f"FY{FY}: {len(dates)} days, {len(done)} already done, computing {len(todo)}.")
    ep.classify_units(ep.bm.fetch_registry())            # warm the registry cache
    new = not OUT.exists()
    with open(OUT, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(COLS)
        for i, d in enumerate(todo, 1):
            try:
                r = ep.realized_b6_congestion_rent(d)
                w.writerow([d, r["R_cong_obs"], r["curtail_payment"],
                            r["replacement"], r["curtailed_MWh"], r["binding_periods"]])
                f.flush()
                if i % 10 == 0 or r["R_cong_obs"] > 5e6:
                    print(f"  [{i}/{len(todo)}] {d}: "
                          f"GBP{r['R_cong_obs']/1e6:6.2f}m  {r['curtailed_MWh']:.0f} MWh")
            except Exception as e:
                print(f"  [{i}/{len(todo)}] {d}: FAILED ({type(e).__name__}: {e})")
    summary()


if __name__ == "__main__":
    if "--summary" in sys.argv:
        summary()
    else:
        run()
