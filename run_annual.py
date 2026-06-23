"""
run_annual.py
=============
Annualises the cost of national pricing on the B6 boundary (paper Section 6.x).
A single day cannot speak to the policy-relevant annual magnitude (NESO's ~GBP1.9
bn/yr constraint cost), and daily costs are highly skewed, so we estimate the
annual total by *stratified sampling*:

  1. pull NESO's published daily system constraint cost for every day of the
     financial year (cheap, one CKAN query) -- the stratifying variable and the
     external annual anchor;
  2. split the 365 days into quantile bins by that daily cost;
  3. solve the full model on a random sample of days per bin (run_paper's
     per-day solver) to get the modelled daily B6 cost;
  4. form the stratified estimator  annual = sum_b N_b * mean_b  with its
     standard error, and report the modelled annual B6 figure, its share of the
     NESO annual total, and the modelled-vs-NESO daily relationship.

Run:  python run_annual.py        (needs BMRS network access + an LP solver)
Outputs: annual_sample_<FY>.csv (per sampled day) and a printed summary.
"""
from __future__ import annotations
import math
from pathlib import Path
import numpy as np
import pandas as pd
import requests

import gb_empirical_pipeline as ep

FINANCIAL_YEAR = "2024-2025"
MARKUP = 0.30          # COUNTERFACTUAL markup for the structural Gamma_markup
                       # column only. The data-supported case is mu=0 (the paper's
                       # within-unit-day RD gives mu_hat=0.00, ep.estimate_markup_pooled);
                       # the realized B6 rent headline does not depend on this.
N_BINS = 5             # strata by NESO daily constraint cost
N_PER_BIN = 6          # sampled days per stratum (30 model-days total)
MIN_PERIODS = 40       # drop a sampled day if fewer periods solved (missing data)
SEED = 20241208


def neso_daily_series(fy: str = FINANCIAL_YEAR) -> pd.DataFrame:
    """Daily system constraint cost (GBP) for every day of the FY, cached."""
    cache = Path(f"neso_daily_{fy}.csv")
    if cache.exists():
        return pd.read_csv(cache)
    rid = ep.NESO_DBC_RESOURCES[fy]
    sql = (f'SELECT "SETT_DATE" AS d, SUM("Constraints") AS neso '
           f'FROM "{rid}" GROUP BY "SETT_DATE" ORDER BY "SETT_DATE"')
    r = requests.get(ep.NESO_CKAN_SQL, params={"sql": sql}, timeout=120)
    r.raise_for_status()
    df = pd.DataFrame(r.json()["result"]["records"])
    df["neso"] = df["neso"].astype(float)
    df["date"] = df["d"].str[:10]
    df = df[["date", "neso"]]
    df.to_csv(cache, index=False)
    return df


def run(fy: str = FINANCIAL_YEAR, markup: float = MARKUP) -> pd.DataFrame:
    days = neso_daily_series(fy)
    annual_neso = float(days["neso"].sum())
    days["bin"] = pd.qcut(days["neso"].rank(method="first"), N_BINS,
                          labels=False)

    parts = []
    for b, g in days.groupby("bin"):
        parts.append(g.sample(min(N_PER_BIN, len(g)), random_state=SEED + int(b)))
    sample = pd.concat(parts).reset_index(drop=True)
    print(f"NESO annual constraint cost {fy}: GBP{annual_neso/1e9:.3f}bn "
          f"over {len(days)} days; sampling {len(sample)} days "
          f"({N_PER_BIN}/bin x {N_BINS} bins).\n")

    rows = []
    for _, r in sample.iterrows():
        d = ep.solve_day(r["date"], markup=markup)
        if d["n_solved"] < MIN_PERIODS:
            print(f"  {r['date']}: skipped (only {d['n_solved']} periods)")
            continue
        # realized, data-driven B6 rent (headline): from SO-flagged Scottish-wind
        # curtailment priced at the boundary rent rate -- no share calibration.
        obs = ep.realized_b6_congestion_rent(r["date"])
        d["R_cong_obs"] = obs["R_cong_obs"]
        d["curtailed_MWh"] = obs["curtailed_MWh"]
        d["bin"] = int(r["bin"])
        d["neso"] = float(r["neso"])
        rows.append(d)
        print(f"  {r['date']} bin{int(r['bin'])}: "
              f"realized R_cong=GBP{obs['R_cong_obs']:,.0f}  "
              f"model Gamma_cb=GBP{d['Gamma_costbased']:,.0f}  "
              f"NESO=GBP{r['neso']:,.0f}")

    df = pd.DataFrame(rows)
    out = f"annual_sample_{fy}.csv"
    df.to_csv(out, index=False)

    # ---- stratified estimator: annual = sum_b N_b * mean_b -------------------
    # canonical implementation lives in ep.stratified_estimator so the annual
    # headline, the classification-robustness sweep and the observed-limit
    # structural run all share identical arithmetic.
    binsize = days.groupby("bin").size().to_dict()
    estimate = lambda col: ep.stratified_estimator(df, col, binsize)

    r_obs, r_obs_se = estimate("R_cong_obs")
    curt, _ = estimate("curtailed_MWh")
    g_cb, g_cb_se = estimate("Gamma_costbased")
    g_mk, _ = estimate("Gamma_markup")

    print("\n=== Annualised cost of national pricing on B6, FY%s ===" % fy)
    print("  [headline] realized B6 congestion rent (data-driven, BM-visible):")
    print(f"      GBP{r_obs/1e6:7.1f}m  (+/- {r_obs_se/1e6:.1f}m SE)")
    print(f"      curtailed energy {curt/1e6:.2f} TWh; "
          f"corr(realized, NESO daily) {df['R_cong_obs'].corr(df['neso']):.2f}")
    print("  [structural model, FUELHH-anchored, incl. embedded wind]:")
    print(f"      R_cong GBP{estimate('R_cong')[0]/1e6:.1f}m, "
          f"Gamma_cb GBP{g_cb/1e6:.1f}m (+/- {g_cb_se/1e6:.1f}m), "
          f"Gamma_markup GBP{g_mk/1e6:.1f}m")
    print(f"\n  NESO annual (system-wide) : GBP{annual_neso/1e6:.0f}m")
    print(f"  realized B6 as share of NESO system total: {100*r_obs/annual_neso:.0f}%")

    # Strategic-markup identification (paper headline): within-unit-day RD over the
    # sampled days. This is the ONLY script that drives estimate_markup_pooled, so
    # the reported mu_hat=0.00 reproduces from here.
    try:
        mk = ep.estimate_markup_pooled(list(sample["date"]))
        if mk.get("mu") is not None:
            print(f"\n  [markup] within-unit-day RD: mu_hat={mk['mu']:+.3f} "
                  f"(se {mk['se']:.3f}, n_unit_days={mk['n_unit_days']}); "
                  f"naive premium contrast {mk['mu_naive_premium']:+.2f} "
                  f"-> cost-reflective (mu=0) is the data-supported case.")
    except Exception as e:
        print(f"\n  [markup] estimate_markup_pooled skipped ({type(e).__name__}: {e})")

    print(f"\nWritten: {out}")
    return df


if __name__ == "__main__":
    run()
