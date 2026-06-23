"""
nonconvex_experiment.py
=======================
Controlled demonstration of Proposition 1 on a purpose-built nonconvex B6
unit-commitment instance (`synthetic_b6_nonconvex`). Shows:

  * Prop 1(a): in the convex instance (`synthetic_b6`) the realised welfare loss
    is zero -- national-pricing-with-redispatch reproduces the nodal optimum.
  * Prop 1(b): in the nonconvex instance the network-blind Stage-1 commitment
    differs from the network optimum (u~ != u*); under the tight BM the
    wrongly-committed unit is stranded at its minimum stable load, so
    W_BM < W* -- a pure commitment-nonconvexity artefact.
  * Remark 1: recommitment re-optimises the commitment and recovers W*, but only
    through out-of-market commitment actions (a higher socialised redispatch
    cost RC).
  * Comparative static: the loss scales with the degree of nonconvexity (the
    no-load cost), and decomposes into a min-load component (no-load -> 0) and a
    no-load component.

Run:  python nonconvex_experiment.py   (offline; needs only an LP/MILP solver)
"""
from __future__ import annotations
import pandas as pd

from gb_two_stage_skeleton import (
    synthetic_b6, synthetic_b6_nonconvex,
    solve_nodal_dcopf, solve_network_blind, solve_redispatch,
)


def _loss_and_rc(sys, policy, markup=0.0):
    nodal = solve_nodal_dcopf(sys)
    blind = solve_network_blind(sys)
    rd = solve_redispatch(sys, blind, commitment_policy=policy, markup=markup)
    return {
        "W_star": nodal["W"], "W_BM": rd["W_BM"],
        "welfare_loss": nodal["W"] - rd["W_BM"], "RC": rd["RC"],
        "u_star": {k: int(v) for k, v in nodal["u"].items()},
        "u_tilde": {k: int(v) for k, v in blind["u"].items()},
    }


def run() -> pd.DataFrame:
    print("=== Proposition 1(a): convex instance (synthetic_b6) ===")
    c = _loss_and_rc(synthetic_b6(), "fixed")
    print(f"  W* = {c['W_star']:.0f}, W_BM = {c['W_BM']:.0f}, "
          f"welfare loss = {c['welfare_loss']:.0f}  (expect 0)\n")

    print("=== Proposition 1(b): nonconvex instance (synthetic_b6_nonconvex) ===")
    s = synthetic_b6_nonconvex()
    tight = _loss_and_rc(s, "fixed")
    recom = _loss_and_rc(s, "recommit")
    print(f"  u*      = {tight['u_star']}")
    print(f"  u~      = {tight['u_tilde']}   (differs from u* => Prop 1(b) bites)")
    print(f"  tight BM    : W_BM = {tight['W_BM']:.0f}, "
          f"welfare loss = {tight['welfare_loss']:.0f}, RC = {tight['RC']:.0f}")
    print(f"  recommitment: W_BM = {recom['W_BM']:.0f}, "
          f"welfare loss = {recom['welfare_loss']:.0f}, RC = {recom['RC']:.0f}")
    print(f"  => recommitment recovers {tight['welfare_loss']-recom['welfare_loss']:.0f}"
          f" of welfare loss, at +{recom['RC']-tight['RC']:.0f} socialised RC\n")

    print("=== Comparative static: welfare loss vs degree of nonconvexity ===")
    rows = []
    for theta in (0.0, 0.25, 0.5, 0.75, 1.0):
        t = _loss_and_rc(synthetic_b6_nonconvex(noload_scale=theta), "fixed")
        rows.append({"noload_scale": theta, "welfare_loss": t["welfare_loss"],
                     "RC_tight": t["RC"]})
        print(f"  no-load x{theta:.2f}: tight-BM welfare loss = {t['welfare_loss']:.0f}")
    print("  (loss at x0 is the pure min-load component; it grows linearly with "
          "no-load; the convex case above is 0)")
    df = pd.DataFrame(rows)
    df.to_csv("nonconvex_static.csv", index=False)
    print("\nWritten: nonconvex_static.csv")
    return df


if __name__ == "__main__":
    run()
