"""
flex_siting.py
==============
S2: optimal *siting and operation* of flexibility across nested GB boundaries.

This turns the first-order flexibility-equivalence remark of the paper into a
solved optimisation. A radial three-zone chain

    North Scotland (N) --B4--> South Scotland (S) --B6--> England (E)

carries cheap wind south to demand in E across two nested cuts: B4 = {N}|{S,E}
and B6 = {N,S}|{E}. A budget K of flexible storage power may be sited behind the
boundaries (in N and/or S) and operated over the day (state-of-charge dynamics,
round-trip efficiency). We choose siting k_z and operation jointly to minimise
the cost of serving demand (expensive English gas backup when wind cannot get
through), subject to the boundary limits.

Key objects returned:
  * optimal siting split (k_N, k_S) for a budget K;
  * the value of flexibility V(K) = gas cost avoided vs K=0 (concave: diminishing
    returns), and its marginal value dV/dK (the budget shadow price);
  * the boundary shadow prices pi_{B4,t}, pi_{B6,t}, which the marginal value of
    siting equals (the multi-boundary, multi-period generalisation of the paper's
    flexibility-equivalence: a MW sited in N relieves *both* nested cuts, a MW in
    S only B6).

This is a linear program (continuous siting); `solve(K)` solves one budget,
`value_curve` sweeps K, `siting_split` reports where the budget goes.

Run:  python flex_siting.py        (needs only an LP solver, no network)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List
import math
import pyomo.environ as pyo


# --------------------------------------------------------------------------- #
# Stylised 24-hour nested-boundary instance (controlled, documented)
# --------------------------------------------------------------------------- #
T = list(range(24))
C_GAS = 80.0           # GBP/MWh, English backup gas (wind is free)
B4_LIMIT = 2500.0      # MW, North->South Scotland cut (binds: northern wind trapped)
B6_LIMIT = 4300.0      # MW, Scotland->England cut (also binds)
ETA = 0.9             # storage round-trip (one-way sqrt applied each leg)
DURATION = 4.0         # hours of energy per MW of sited power (4h storage)
DEM_N, DEM_S = 300.0, 400.0   # local Scottish demand (MW, flat)


def _wind(cap, peak_hours):
    """Diurnal wind profile (MW) for a zone: high overnight/midday, low evening."""
    prof = []
    for t in T:
        # two humps (night + midday), trough in the evening peak
        cf = 0.55 + 0.40 * math.cos((t - 3) / 24 * 2 * math.pi) \
                  + 0.20 * math.cos((t - 13) / 24 * 2 * math.pi)
        prof.append(max(0.0, min(1.0, cf)) * cap)
    return prof


def _demand_E():
    """English demand (MW): evening peak, anti-correlated with wind."""
    return [4500 + 1500 * math.exp(-((t - 18) ** 2) / 8.0) for t in T]


WIND_N = _wind(6000.0, None)   # large northern wind, much of it trapped behind B4
WIND_S = _wind(3000.0, None)   # southern wind: valuable to store behind B6
DEM_E = _demand_E()


# --------------------------------------------------------------------------- #
# The siting + operation LP
# --------------------------------------------------------------------------- #
def build(K: float, fix_split: Dict[str, float] | None = None) -> pyo.ConcreteModel:
    """Multi-period LP. K = total flexible-storage power budget (MW) to site in
    {N,S}. If `fix_split` is given (e.g. {'N':x,'S':y}) the siting is fixed to it
    (for the 'site it all in one zone' counterfactuals)."""
    m = pyo.ConcreteModel()
    m.T = pyo.Set(initialize=T, ordered=True)
    Z = ["N", "S"]                                  # zones that can host storage
    m.Z = pyo.Set(initialize=Z)

    # siting (continuous capacity), budget Sum k_z <= K
    m.k = pyo.Var(m.Z, domain=pyo.NonNegativeReals)
    if fix_split is not None:
        for z in Z:
            m.k[z].fix(fix_split[z])
    else:
        m.budget = pyo.Constraint(expr=sum(m.k[z] for z in m.Z) <= K)

    # operation
    m.wuN = pyo.Var(m.T, domain=pyo.NonNegativeReals)   # wind used in N
    m.wuS = pyo.Var(m.T, domain=pyo.NonNegativeReals)
    m.gE = pyo.Var(m.T, domain=pyo.NonNegativeReals)    # English gas
    m.F4 = pyo.Var(m.T)                                 # flow N->S
    m.F6 = pyo.Var(m.T)                                 # flow S->E
    m.ch = pyo.Var(m.Z, m.T, domain=pyo.NonNegativeReals)
    m.dis = pyo.Var(m.Z, m.T, domain=pyo.NonNegativeReals)
    m.soc = pyo.Var(m.Z, m.T, domain=pyo.NonNegativeReals)

    # wind availability
    m.wn = pyo.Constraint(m.T, rule=lambda m, t: m.wuN[t] <= WIND_N[t])
    m.ws = pyo.Constraint(m.T, rule=lambda m, t: m.wuS[t] <= WIND_S[t])

    # zone balance (radial chain)
    m.balN = pyo.Constraint(m.T, rule=lambda m, t:
        m.wuN[t] + m.dis["N", t] - m.ch["N", t] - DEM_N - m.F4[t] == 0)
    m.balS = pyo.Constraint(m.T, rule=lambda m, t:
        m.wuS[t] + m.dis["S", t] - m.ch["S", t] - DEM_S + m.F4[t] - m.F6[t] == 0)
    m.balE = pyo.Constraint(m.T, rule=lambda m, t:
        m.gE[t] + m.F6[t] - DEM_E[t] == 0)

    # nested boundary limits (the duals are the congestion prices)
    m.b4hi = pyo.Constraint(m.T, rule=lambda m, t: m.F4[t] <= B4_LIMIT)
    m.b4lo = pyo.Constraint(m.T, rule=lambda m, t: m.F4[t] >= -B4_LIMIT)
    m.b6hi = pyo.Constraint(m.T, rule=lambda m, t: m.F6[t] <= B6_LIMIT)
    m.b6lo = pyo.Constraint(m.T, rule=lambda m, t: m.F6[t] >= -B6_LIMIT)

    # storage: power tied to sited capacity, energy to capacity x duration
    m.chcap = pyo.Constraint(m.Z, m.T, rule=lambda m, z, t: m.ch[z, t] <= m.k[z])
    m.discap = pyo.Constraint(m.Z, m.T, rule=lambda m, z, t: m.dis[z, t] <= m.k[z])
    m.soccap = pyo.Constraint(m.Z, m.T,
                              rule=lambda m, z, t: m.soc[z, t] <= m.k[z] * DURATION)

    # state of charge dynamics (cyclic: end of day == start)
    rt = math.sqrt(ETA)
    def soc_rule(m, z, t):
        prev = m.soc[z, T[-1]] if t == T[0] else m.soc[z, t - 1]
        return m.soc[z, t] == prev + rt * m.ch[z, t] - m.dis[z, t] / rt
    m.socdyn = pyo.Constraint(m.Z, m.T, rule=soc_rule)

    # objective: minimise English gas cost (= maximise cheap wind delivered)
    m.obj = pyo.Objective(expr=sum(C_GAS * m.gE[t] for t in m.T),
                          sense=pyo.minimize)
    m.dual = pyo.Suffix(direction=pyo.Suffix.IMPORT)
    return m


def _solver():
    for s in ("appsi_highs", "cbc", "glpk"):
        try:
            opt = pyo.SolverFactory(s)
            if opt is not None and opt.available():
                return opt
        except Exception:
            continue
    raise RuntimeError("No LP solver; pip install highspy")


def solve(K: float, fix_split: Dict[str, float] | None = None) -> Dict:
    m = build(K, fix_split)
    _solver().solve(m)
    gas_cost = float(pyo.value(m.obj))
    k = {z: float(pyo.value(m.k[z])) for z in ["N", "S"]}
    # congestion prices (per period) from the boundary-limit duals
    pi4 = {t: abs(float(m.dual[m.b4hi[t]])) for t in T}
    pi6 = {t: abs(float(m.dual[m.b6hi[t]])) for t in T}
    return {"K": K, "gas_cost": gas_cost, "k": k,
            "pi4_total": sum(pi4.values()), "pi6_total": sum(pi6.values()),
            "budget_dual": (abs(float(m.dual[m.budget]))
                            if fix_split is None and hasattr(m, "budget") else None)}


def value_curve(Ks: List[float]) -> List[Dict]:
    base = solve(0.0)["gas_cost"]
    out = []
    for K in Ks:
        r = solve(K)
        r["value"] = base - r["gas_cost"]           # gas cost avoided
        out.append(r)
    return out


def run():
    print("=== S2: optimal flexibility siting across nested B4/B6 ===")
    base = solve(0.0)
    print(f"No storage (K=0): gas cost = GBP{base['gas_cost']:,.0f}; "
          f"binding totals pi_B4={base['pi4_total']:.0f}, pi_B6={base['pi6_total']:.0f}\n")

    print("Value of flexibility V(K) and optimal siting split:")
    print(f"  {'K (MW)':>8} {'V (GBP)':>12} {'dV/dK':>8} {'k_N':>7} {'k_S':>7}")
    for K in (500, 1000, 2000, 3000, 4000, 5000):
        r = solve(K)
        v = base["gas_cost"] - r["gas_cost"]
        dv = r["budget_dual"] or 0.0
        print(f"  {K:>8.0f} {v:>12,.0f} {dv:>8.1f} {r['k']['N']:>7.0f} {r['k']['S']:>7.0f}")

    print("\nSiting matters -- value of all-N vs all-S vs optimal, by budget:")
    print(f"  {'K (MW)':>8} {'all-N':>12} {'all-S':>12} {'optimal':>12}")
    for K in (1000, 2000, 3000, 4000):
        vN = base["gas_cost"] - solve(K, fix_split={"N": K, "S": 0.0})["gas_cost"]
        vS = base["gas_cost"] - solve(K, fix_split={"N": 0.0, "S": K})["gas_cost"]
        vO = base["gas_cost"] - solve(K)["gas_cost"]
        print(f"  {K:>8.0f} {vN:>12,.0f} {vS:>12,.0f} {vO:>12,.0f}")


if __name__ == "__main__":
    run()
