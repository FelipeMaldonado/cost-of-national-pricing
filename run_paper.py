"""
run_paper.py
============
Reproduces the empirical result of "The Cost of National Pricing": selects the
peak-curtailment B6 day in a candidate window, then for each settlement period
solves the nodal benchmark, the Stage-2 redispatch (cost-reflective mu=0, plus a
mu=0.30 counterfactual) and the nodal PE-A make-whole minimisation, aggregating
the Gamma decomposition over the day. The modelled redispatch cost is cross-checked
against the redispatch cost observed directly in SO-flagged BOALF x BOD.

Run (needs network access to BMRS + a MILP/LP solver):
    pip install pyomo highspy requests pandas pyarrow
    python run_paper.py

Outputs: results_b6_<date>.csv and a printed summary that populates the peak-day
decomposition table of the paper (tab:results; Table 3 in the current draft).
"""
from __future__ import annotations
import pandas as pd

import gb_empirical_pipeline as ep
from gb_two_stage_skeleton import (
    solve_nodal_dcopf, solve_network_blind, solve_redispatch, solve_pea,
)

CANDIDATE_DAYS = ["2024-12-04", "2024-12-08", "2025-01-01", "2025-01-24"]
# mu is NOT estimated here. The paper's *identified* markup is mu_hat=0.00, from the
# within-unit-day regression discontinuity over many days (ep.estimate_markup_pooled,
# reported by run_annual.py). On a fully-binding peak day there is no non-binding
# control, so a single-day estimate is unidentified and misleading -- we do not run
# it. Here mu=0.30 is simply the fixed COUNTERFACTUAL (the ~30% replacement-gas
# premium; paper Section 1) used for the *_markup column of tab:results.
MARKUP = 0.30          # counterfactual markup for the markup column of tab:results
N_PERIODS = 48
PERIOD_HOURS = 0.5     # a settlement period is half an hour: MW -> MWh, so the
                       # per-period modelled cost (a power-rate) scales to GBP by 0.5
# Instance builder: "zonal" = faithful FUELHH/demand-anchored 2-zone instance
# (B6 binds); "pn" = the raw-PN loader (kept for reference, does not bind B6).
INSTANCE = "zonal"
NESO_DAILY_CONSTRAINT_COST = None   # None => auto-fetched from the NESO Data Portal
                                    # for the chosen day (ep.neso_daily_constraint_cost);
                                    # set a number to override.


def run(date: str | None = None, markup: float = MARKUP) -> pd.DataFrame:
    if date is None:
        date, ranking = ep.find_peak_curtailment_day(CANDIDATE_DAYS)
        print(f"Peak-curtailment day selected: {date}\n{ranking}\n")
    print(f"Cost-reflective mu=0 (data-supported); mu={markup} counterfactual "
          f"for the markup column (see run_annual.py for the identified mu_hat~0).\n")

    build = (ep.build_zonal_instance if INSTANCE == "zonal"
             else ep.load_bmrs_boalf_fpn)
    h = PERIOD_HOURS
    rows = []
    skipped = 0
    for t in range(1, N_PERIODS + 1):
        try:                                        # build + solve one period;
            sys, blind = build(date, t)             # any failure (missing data,
            nodal = solve_nodal_dcopf(sys)          # infeasible or unbounded
            rd_cost = solve_redispatch(sys, blind, commitment_policy="fixed", markup=0.0)
            rd_mkup = solve_redispatch(sys, blind, commitment_policy="fixed", markup=markup)
            pea = solve_pea(sys, nodal)
        except Exception as e:                      # solve) -> skip the period
            print(f"  period {t}: skipped ({type(e).__name__}: {e})")
            skipped += 1
            continue
        # scale per-period power-rate costs to GBP for the half hour (MW -> MWh)
        rows.append({
            "period": t,
            "W_nodal": nodal["W"] * h,
            "W_BM": rd_cost["W_BM"] * h,
            "welfare_loss_P1": (nodal["W"] - rd_cost["W_BM"]) * h,
            "RC_costbased": rd_cost["RC"] * h,
            "RC_markup": rd_mkup["RC"] * h,
            # Proposition 2 decomposition: R_cong from the cost-reflective solve;
            # M as the *incremental* markup cost RC(mu)-RC(0) so that, exactly,
            # Gamma = RC_markup = R_cong + M (the (offer-cost) base premium is
            # already inside R_cong at submitted prices).
            "R_cong": rd_cost["R_cong"] * h,
            "M_markup": (rd_mkup["RC"] - rd_cost["RC"]) * h,
            "MWP_PEA": pea["MWP_PEA"] * h,
            "Gamma_costbased": (rd_cost["RC"] - pea["MWP_PEA"]) * h,
            "Gamma_markup": (rd_mkup["RC"] - pea["MWP_PEA"]) * h,
        })

    if not rows:
        raise SystemExit(f"No solvable periods for {date} ({skipped} skipped). "
                         "Check data availability and the instance builder.")
    if skipped:
        print(f"  ({skipped}/{N_PERIODS} periods skipped)")
    df = pd.DataFrame(rows)
    out = f"results_b6_{date}.csv"
    df.to_csv(out, index=False)

    rec = ep.reconcile(date, neso_value=NESO_DAILY_CONSTRAINT_COST)
    print("\n=== Daily totals (GBP) ===")
    print("  cost-reflective (mu=0, data-supported): R_cong, Gamma_costbased")
    print("  counterfactual  (mu=0.30):              RC_markup, M_markup, Gamma_markup")
    for k in ("RC_costbased", "RC_markup", "R_cong", "M_markup", "MWP_PEA",
              "Gamma_costbased", "Gamma_markup", "welfare_loss_P1"):
        label = k + (" [mu=0.30]" if k.endswith("markup") or k == "M_markup" else "")
        print(f"  {label:28s}: {df[k].sum():,.2f}")

    print("\n=== Reconciliation (validates the headline) ===")
    print(f"  proxy RC (period-held, 1st band): {rec['proxy_RC']:,.0f}")
    print(f"  detailed RC (BM SO-flagged BOA) : {rec['detailed_RC']:,.0f}")
    bd = rec["detailed_breakdown"]
    print(f"    of which Scottish-wind turn-down (B6): {bd['scottish_wind_turndown']:,.0f}")
    print(f"    of which replacement turn-up        : {bd['turnup_replacement']:,.0f}")
    neso = rec['neso_published']
    print("  NESO published constraint cost (system-wide): "
          + (f"{neso:,.0f}" if neso is not None else "unavailable"))
    if rec.get("bm_share_of_neso") is not None:
        print(f"  BM-BOA cost as share of NESO total: {rec['bm_share_of_neso']:.0%}")
    if rec.get("b6_share_of_system") is not None:
        print(f"  B6 (Scottish wind) share of BM-BOA cost: {rec['b6_share_of_system']:.1%}")
    idisc = rec['internal_discrepancy']
    print(f"  internal check (proxy vs detailed): "
          + (f"{idisc:+.1%} {'PASS' if rec['internal_pass'] else 'FAIL'}"
             if idisc is not None else "n/a"))
    if rec.get("warning_internal"):
        print(f"  WARNING: {rec['warning_internal']}")
    if rec.get("warning_external"):
        print(f"  WARNING: {rec['warning_external']}")
    elif rec["external_pass"]:
        print("  PASS: BM-BOA cost is a plausible majority of NESO's total; "
              "the residual is non-BM constraint actions. Headline can be quoted.")
    else:
        print("  NESO value unavailable; cannot run the external check.")
    print(f"\nWritten: {out}")
    return df


if __name__ == "__main__":
    run()
