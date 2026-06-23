"""
sensitivity.py
==============
Comparative statics for Section 6.1 / Appendix B of "The Cost of National
Pricing". Sweeps the four policy-relevant levers on the faithful B6 two-zone
instance and reports how the daily cost of national pricing responds:

  * B6 transmission capability  (transmission build-out)
  * Scottish wind penetration   (Clean Power 2030 trajectory)
  * flexibility behind the boundary (storage / flexible demand north of B6)
  * strategic markup mu          (BM competition)

For each lever value we solve every settlement period of the selected day,
cost-reflectively (for R_cong) and with the markup (for M), and aggregate to
daily GBP (per-period power x PERIOD_HOURS). Writes sensitivity_<date>.csv and
prints paper-ready tables.

Run:  python sensitivity.py        (needs BMRS network access + an LP solver)
"""
from __future__ import annotations
import pandas as pd

import gb_empirical_pipeline as ep
from gb_two_stage_skeleton import solve_redispatch

DAY = "2024-12-08"          # the paper's selected peak-curtailment day
N_PERIODS = 48
H = 0.5                     # half-hour settlement period: MW -> MWh

# baseline (central calibration)
BASE = dict(b6_limit=ep.B6_LIMIT_MW, wind_scale=1.0, flex_mw=0.0)
MARKUP_BASE = 0.30

B6_GRID = [4000.0, 5000.0, 6100.0, 7000.0, 8000.0, 9000.0]
WIND_GRID = [0.8, 1.0, 1.2, 1.4, 1.6]
FLEX_GRID = [0.0, 1000.0, 2000.0, 3000.0, 4000.0]
MU_GRID = [0.0, 0.15, 0.30, 0.45]


def _daily(day: str, *, markup: float = 0.0, **kw) -> dict:
    """Solve all periods at one parameter set; return daily totals (GBP).
    M is defined as the *incremental* cost of the markup, RC(mu) - RC(0), so the
    decomposition Gamma = RC(mu) = R_cong + M holds exactly."""
    p = dict(BASE, **kw)
    rc0 = rcong = rcm = 0.0
    for t in range(1, N_PERIODS + 1):
        try:
            sys, blind = ep.build_zonal_instance(day, t, **p)
            r0 = solve_redispatch(sys, blind, commitment_policy="fixed", markup=0.0)
            rc0 += r0["RC"] * H
            rcong += r0["R_cong"] * H
            if markup:
                rm = solve_redispatch(sys, blind, commitment_policy="fixed", markup=markup)
                rcm += rm["RC"] * H
        except Exception:
            continue
    m = (rcm - rc0) if markup else 0.0
    gamma = rcm if markup else rc0
    return {"RC": rc0, "R_cong": rcong, "M": m, "Gamma": gamma}


def sweep(day: str = DAY) -> pd.DataFrame:
    rows = []
    print(f"Sensitivity sweeps on {day} (daily GBP)\n")

    print("== B6 capability (MW) ==")
    for b in B6_GRID:
        d = _daily(day, b6_limit=b)
        rows.append({"lever": "B6_limit_MW", "value": b, **d})
        print(f"  B6={b:>6.0f}: R_cong={d['R_cong']:>12,.0f}")

    print("\n== Wind penetration (x baseline output) ==")
    for w in WIND_GRID:
        d = _daily(day, wind_scale=w)
        rows.append({"lever": "wind_scale", "value": w, **d})
        print(f"  wind x{w:>3.1f}: R_cong={d['R_cong']:>12,.0f}")

    print("\n== Flexibility behind the boundary (MW relocated north) ==")
    for f in FLEX_GRID:
        d = _daily(day, flex_mw=f)
        rows.append({"lever": "flex_mw", "value": f, **d})
        print(f"  flex={f:>5.0f}: R_cong={d['R_cong']:>12,.0f}")

    print("\n== Strategic markup mu ==")
    for mu in MU_GRID:
        d = _daily(day, markup=mu)
        rows.append({"lever": "markup_mu", "value": mu, **d})
        print(f"  mu={mu:>4.2f}: M={d['M']:>12,.0f}  Gamma={d['Gamma']:>12,.0f}")

    df = pd.DataFrame(rows)
    out = f"sensitivity_{day}.csv"
    df.to_csv(out, index=False)
    print(f"\nWritten: {out}")
    return df


if __name__ == "__main__":
    sweep()
