"""run_multiyear.py
================
Full-year realised B6 redispatch expenditure for one or more financial years, for
the multi-year trend. Same per-day measure as run_fullyear.py (curtailment payment
+ replacement leg on SO-flagged Scottish-wind turn-down), computed directly from
BMRS settlement data. Resumable: appends each day to fullyear_<FY>.csv immediately
and skips days already done, so it can be re-run to continue after an interruption.

    python run_multiyear.py 2023-2024 2022-2023   # fetch both, in order

The 2024/25 year is already in fullyear_2024-2025.csv (run_fullyear.py). Only the
realised-expenditure legs are computed here; the NESO % share is added separately.
"""
from __future__ import annotations
import sys
import types
import csv
import datetime as _dt
from pathlib import Path

# realized_b6_congestion_rent (the only function used here) needs no solver; if the
# optimisation stack is absent, stub pyomo so the import chain still succeeds.
try:  # pragma: no cover
    import pyomo  # noqa: F401
except ModuleNotFoundError:
    for _n in ("pyomo", "pyomo.environ", "pyomo.opt"):
        sys.modules.setdefault(_n, types.ModuleType(_n))

import pandas as pd
import gb_empirical_pipeline as ep

COLS = ["date", "R_cong_obs", "curtail_payment", "replacement",
        "curtailed_MWh", "binding_periods"]


def fy_dates(fy: str) -> list[str]:
    """"2023-2024" -> every ISO date from 2023-04-01 to 2024-03-31 inclusive."""
    y0 = int(fy[:4])
    start, end = _dt.date(y0, 4, 1), _dt.date(y0 + 1, 3, 31)
    out, d = [], start
    while d <= end:
        out.append(d.isoformat())
        d += _dt.timedelta(days=1)
    return out


def _done(out: Path) -> set[str]:
    if not out.exists():
        return set()
    return set(pd.read_csv(out)["date"].astype(str))


def run(fy: str) -> None:
    out = Path(f"fullyear_{fy}.csv")
    dates = fy_dates(fy)
    done = _done(out)
    todo = [d for d in dates if d not in done]
    print(f"FY{fy}: {len(dates)} days, {len(done)} done, {len(todo)} to fetch.",
          flush=True)
    ep.classify_units(ep.bm.fetch_registry())          # warm the registry cache
    new = not out.exists()
    with open(out, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(COLS)
        for i, d in enumerate(todo, 1):
            try:
                r = ep.realized_b6_congestion_rent(d)
                w.writerow([d, r["R_cong_obs"], r["curtail_payment"],
                            r["replacement"], r["curtailed_MWh"],
                            r["binding_periods"]])
                f.flush()
                if i % 10 == 0 or r["R_cong_obs"] > 5e6:
                    print(f"  [{i}/{len(todo)}] {d}: "
                          f"GBP{r['R_cong_obs']/1e6:6.2f}m", flush=True)
            except Exception as e:
                print(f"  [{i}/{len(todo)}] {d}: FAILED "
                      f"({type(e).__name__}: {e})", flush=True)
    df = pd.read_csv(out).drop_duplicates("date")
    print(f"FY{fy} complete: {len(df)}/{len(dates)} days, "
          f"E = GBP{df['R_cong_obs'].sum()/1e6:.0f}m, "
          f"{df['curtailed_MWh'].sum()/1e6:.2f} TWh.", flush=True)


if __name__ == "__main__":
    years = sys.argv[1:] or ["2022-2023", "2023-2024"]
    for fy in years:
        run(fy)
