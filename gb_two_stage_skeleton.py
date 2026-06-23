"""
gb_two_stage_skeleton.py
========================
Runnable skeleton for "The Cost of National Pricing" (extension of Bichler,
Knoerr & Maldonado, ISR 2022) to the GB market.

It implements four optimisation models over a boundary-transfer (NTC) network
that matches how GB constraints are actually defined (ETYS boundaries such as
B6 = Scotland->England), at half-hourly resolution:

  1. solve_nodal_dcopf   -- network-constrained efficient dispatch + commitment
                            (the benchmark; "nodal" = per-zone prices)
  2. solve_network_blind -- Stage 1: copper-plate clearing -> single national
                            price per period + self-schedule (self-dispatch)
  3. solve_redispatch    -- Stage 2: cost-min redispatch to a network-feasible
                            dispatch at offer/bid prices; cost-based or markup;
                            commitment fixed or allowed to recommit
  4. solve_pea           -- nodal PE-A: minimise make-whole payments under
                            linear-anonymous (per-zone) prices (BKM PE-A)

Running the file solves a synthetic 2-zone (Scotland / England) instance with a
binding B6 boundary and nonconvex generators, and prints the Gamma decomposition
of P2.

Requires: pyomo, and a MILP/LP solver (HiGHS via `appsi_highs`, or cbc/glpk).
    pip install pyomo highspy
Data loaders for the real GB artifacts are stubbed at the bottom with the
concrete endpoints to fill in.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional
import pyomo.environ as pyo


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Generator:
    name: str
    zone: str
    cost: float          # variable cost  c_j  (GBP/MWh)
    pmax: float          # max output     (MW)
    pmin: float = 0.0    # min stable load when committed (MW)
    noload: float = 0.0  # no-load / fixed cost h_j when committed (GBP/period)
    can_commit: bool = True   # subject to a commitment binary (nonconvexity)
    # Stage-2 BM prices (GBP/MWh). Defaults to cost; override from BOD data.
    offer: Optional[float] = None   # price to turn UP
    bid: Optional[float] = None     # price to turn DOWN (may be negative, e.g. CfD wind)

    def o(self) -> float:
        return self.cost if self.offer is None else self.offer

    def b(self) -> float:
        return self.cost if self.bid is None else self.bid


@dataclass
class Load:
    name: str
    zone: str
    mw: float            # quantity demanded per period (MW)  -- inelastic block
    value: float = 1e6   # valuation v_i (GBP/MWh); huge => price-inelastic
    flexible: bool = False  # if True, may be curtailed at valuation `value`


@dataclass
class Boundary:
    """A network cut. `north` is the set of zones on one side; the boundary
    flow is the net export of the north side, limited by ETYS capability."""
    name: str
    north: List[str]
    limit: float         # MW transfer capability (ETYS boundary capability)


@dataclass
class System:
    zones: List[str]
    periods: List[int]
    gens: List[Generator]
    loads: List[Load]
    boundaries: List[Boundary] = field(default_factory=list)

    def net_export_expr(self, m, zone_set, t):
        """Net export (gen - load) of a set of zones in period t."""
        gen = sum(m.y[g.name, t] for g in self.gens if g.zone in zone_set)
        dem = sum(m.x[l.name, t] for l in self.loads if l.zone in zone_set)
        return gen - dem


# --------------------------------------------------------------------------- #
# Solver helper
# --------------------------------------------------------------------------- #
def _solver(name: str = "auto"):
    candidates = (["appsi_highs", "cbc", "glpk", "gurobi"]
                  if name == "auto" else [name])
    for s in candidates:
        try:
            opt = pyo.SolverFactory(s)
            if opt is not None and opt.available():
                return opt
        except Exception:
            continue
    raise RuntimeError("No MILP/LP solver available. Try `pip install highspy`.")


class InfeasibleModel(Exception):
    """Raised when a solve does not reach an optimal solution (infeasible or
    unbounded), so callers (e.g. run_paper.py) can skip the period cleanly
    instead of crashing on a cryptic solver load-solution error."""


def _check_optimal(res, tag: str) -> None:
    """Raise InfeasibleModel if the solve clearly did not succeed (infeasible or
    unbounded). Permissive: anything not explicitly bad is allowed through."""
    try:
        tc = str(res.solver.termination_condition).lower()
    except AttributeError:
        return  # some interfaces don't expose this; assume the solve loaded
    if any(bad in tc for bad in ("infeasible", "unbounded", "nosolution")):
        raise InfeasibleModel(f"{tag}: solve terminated '{tc}'")


def _common_vars(m, sys: System, with_commit: bool):
    m.G = pyo.Set(initialize=[g.name for g in sys.gens])
    m.D = pyo.Set(initialize=[l.name for l in sys.loads])
    m.T = pyo.Set(initialize=sys.periods)
    gmap = {g.name: g for g in sys.gens}
    lmap = {l.name: l for l in sys.loads}
    m.y = pyo.Var(m.G, m.T, domain=pyo.NonNegativeReals)   # generation
    m.x = pyo.Var(m.D, m.T, domain=pyo.NonNegativeReals)   # consumption
    if with_commit:
        committable = [g.name for g in sys.gens if g.can_commit]
        m.GC = pyo.Set(initialize=committable)
        m.u = pyo.Var(m.GC, m.T, domain=pyo.Binary)
    return gmap, lmap


def _bounds_and_balance(m, sys, gmap, lmap, with_commit, enforce_boundaries):
    # generator capacity / min-load (with optional commitment)
    def gcap(m, j, t):
        g = gmap[j]
        if with_commit and g.can_commit:
            return m.y[j, t] <= g.pmax * m.u[j, t]
        return m.y[j, t] <= g.pmax
    m.gcap = pyo.Constraint(m.G, m.T, rule=gcap)

    if with_commit:
        def gmin(m, j, t):
            g = gmap[j]
            return m.y[j, t] >= g.pmin * m.u[j, t]
        m.gmin = pyo.Constraint(m.GC, m.T, rule=gmin)

    # demand: inelastic loads fixed; flexible loads may curtail down to 0
    def dcap(m, d, t):
        l = lmap[d]
        return m.x[d, t] <= l.mw
    m.dcap = pyo.Constraint(m.D, m.T, rule=dcap)

    def dfix(m, d, t):
        l = lmap[d]
        return pyo.Constraint.Skip if l.flexible else (m.x[d, t] == l.mw)
    m.dfix = pyo.Constraint(m.D, m.T, rule=dfix)

    # per-period system balance (aggregate)  -- BKM constraint (4)
    def balance(m, t):
        return (sum(m.y[g.name, t] for g in sys.gens)
                == sum(m.x[l.name, t] for l in sys.loads))
    m.balance = pyo.Constraint(m.T, rule=balance)

    # boundary transfer limits (ETYS). Skipped in the network-blind model.
    if enforce_boundaries:
        def bnd(m, bname, t):
            b = next(bb for bb in sys.boundaries if bb.name == bname)
            return (-b.limit,
                    sys.net_export_expr(m, set(b.north), t),
                    b.limit)
        m.BND = pyo.Set(initialize=[b.name for b in sys.boundaries])
        m.bnd = pyo.Constraint(m.BND, m.T, rule=bnd)


def _welfare_expr(m, sys, gmap, lmap, with_commit):
    val = sum(lmap[d].value * m.x[d, t] for d in m.D for t in m.T
              if lmap[d].flexible)  # only flexible demand carries finite value
    vcost = sum(gmap[j].cost * m.y[j, t] for j in m.G for t in m.T)
    ncost = (sum(gmap[j].noload * m.u[j, t] for j in m.GC for t in m.T)
             if with_commit else 0.0)
    return val - vcost - ncost


# --------------------------------------------------------------------------- #
# 1. Nodal DCOPF benchmark (network-constrained efficient dispatch)
# --------------------------------------------------------------------------- #
def solve_nodal_dcopf(sys: System, solver="auto") -> Dict:
    m = pyo.ConcreteModel()
    gmap, lmap = _common_vars(m, sys, with_commit=True)
    _bounds_and_balance(m, sys, gmap, lmap, with_commit=True,
                        enforce_boundaries=True)
    m.obj = pyo.Objective(expr=_welfare_expr(m, sys, gmap, lmap, True),
                          sense=pyo.maximize)
    _solver(solver).solve(m)
    return _extract(m, sys, gmap, with_commit=True, tag="nodal")


# --------------------------------------------------------------------------- #
# 2. Stage 1: network-blind national clearing (self-dispatch)
# --------------------------------------------------------------------------- #
def solve_network_blind(sys: System, solver="auto") -> Dict:
    m = pyo.ConcreteModel()
    gmap, lmap = _common_vars(m, sys, with_commit=True)
    _bounds_and_balance(m, sys, gmap, lmap, with_commit=True,
                        enforce_boundaries=False)          # <-- network invisible
    m.obj = pyo.Objective(expr=_welfare_expr(m, sys, gmap, lmap, True),
                          sense=pyo.maximize)
    _solver(solver).solve(m)
    out = _extract(m, sys, gmap, with_commit=True, tag="blind")
    # national price per period: dual of the aggregate balance. Recover via an
    # LP with commitment fixed (MILP duals are not defined -> fix u, re-solve LP).
    out["lambda_nat"] = _national_price(sys, out["u"], solver)
    return out


def _national_price(sys, ufix, solver) -> Dict[int, float]:
    m = pyo.ConcreteModel()
    gmap, lmap = _common_vars(m, sys, with_commit=False)
    # fix capacities to the committed set
    def gcap(m, j, t):
        g = gmap[j]
        cap = g.pmax * (ufix.get((j, t), 1.0) if g.can_commit else 1.0)
        return m.y[j, t] <= cap
    m.gcap = pyo.Constraint(m.G, m.T, rule=gcap)
    def dcap(m, d, t):
        return m.x[d, t] <= lmap[d].mw
    m.dcap = pyo.Constraint(m.D, m.T, rule=dcap)
    def dfix(m, d, t):
        l = lmap[d]
        return pyo.Constraint.Skip if l.flexible else (m.x[d, t] == l.mw)
    m.dfix = pyo.Constraint(m.D, m.T, rule=dfix)
    def balance(m, t):
        return (sum(m.y[g.name, t] for g in sys.gens)
                == sum(m.x[l.name, t] for l in sys.loads))
    m.balance = pyo.Constraint(m.T, rule=balance)
    m.dual = pyo.Suffix(direction=pyo.Suffix.IMPORT)
    m.obj = pyo.Objective(expr=_welfare_expr(m, sys, gmap, lmap, False),
                          sense=pyo.maximize)
    _solver(solver).solve(m)
    return {t: float(m.dual[m.balance[t]]) for t in sys.periods}


# --------------------------------------------------------------------------- #
# 3. Stage 2: redispatch (Balancing Mechanism)
# --------------------------------------------------------------------------- #
def solve_redispatch(sys: System, blind: Dict, solver="auto",
                     commitment_policy: str = "fixed",   # "fixed" | "recommit"
                     markup: float = 0.0) -> Dict:
    """Move from the self-schedule `blind` to a network-feasible dispatch at
    minimum BM cost. `markup` applies a multiplicative premium to turn-up offers
    (set ~0.30 for the empirical GB gas premium, or per-generator via
    Generator.offer); it materialises the strategic markup term mu_{jt} of
    Proposition 2.

    Besides the redispatch cost RC, the routine returns the Proposition-2
    decomposition RC = R_cong + M + residual, where
      * R_cong = sum_b pi_{b,t} * s_{b,t} is the congestion rent: the binding
        boundary shadow price pi_{b,t} (LP dual of the transfer limit) times the
        relieved overload s_{b,t} (how far the network-blind flow exceeded the
        ETYS capability);
      * M       = sum_j (o^eff_{j,t} - c_{j,t}) (Delta y_{j,t})^+ is the strategic
        turn-up markup (BOD/markup premium above cost on accepted offers);
      * residual collects bid-side deviations from cost (e.g. CfD-backed wind
        whose turn-down bid is negative) and any MILP/convex-hull gap when
        commitment is re-optimised. For cost-reflective o=b=c on the convex LP it
        is ~0 and RC = R_cong (Proposition 2(i)-(ii)).

    NOTE on the markup solve: when markup>0 the boundary dual pi absorbs the
    premium, so the self-reported R_cong is the rent *at marked-up prices*. The
    paper's clean split RC_markup ~= R_cong(cost-reflective) + M reads R_cong from
    the markup=0 companion solve and M from this one; run_paper.py does exactly
    that. With markup=0 (the default) R_cong is the cost-reflective rent."""
    m = pyo.ConcreteModel()
    with_commit = (commitment_policy == "recommit")
    gmap, lmap = _common_vars(m, sys, with_commit=with_commit)
    _bounds_and_balance(m, sys, gmap, lmap, with_commit=with_commit,
                        enforce_boundaries=True)           # <-- network enforced
    ytil = blind["y"]

    if not with_commit:   # tight BM: commitment frozen at the self-schedule u~
        def _committed(j, t):
            g = gmap[j]
            return (not g.can_commit) or blind["u"].get((j, t), 1.0) > 0.5
        # upper bound: only committed units may run (off units capped at 0)
        def lock(m, j, t):
            return m.y[j, t] <= (gmap[j].pmax if _committed(j, t) else 0.0)
        m.lock = pyo.Constraint(m.G, m.T, rule=lock)
        # lower bound: a committed unit respects its minimum stable load -- the BM
        # cannot stand it down without decommitting, which the tight case forbids.
        # This is what strands a wrongly-committed unit at P_min (Proposition 1(b)).
        def lockmin(m, j, t):
            g = gmap[j]
            if g.can_commit and blind["u"].get((j, t), 1.0) > 0.5 and g.pmin > 0:
                return m.y[j, t] >= g.pmin
            return pyo.Constraint.Skip
        m.lockmin = pyo.Constraint(m.G, m.T, rule=lockmin)

    # redispatch volumes (turn-up / turn-down) via split, bounded by physical
    # headroom so the LP cannot cycle (turn one unit up AND down without limit):
    #   up_{jt} <= pmax_j - ytil_{jt}   (headroom above the self-schedule)
    #   dn_{jt} <= ytil_{jt}            (cannot go below zero output)
    m.up = pyo.Var(m.G, m.T, domain=pyo.NonNegativeReals)
    m.dn = pyo.Var(m.G, m.T, domain=pyo.NonNegativeReals)
    def split(m, j, t):
        return m.y[j, t] - ytil[(j, t)] == m.up[j, t] - m.dn[j, t]
    m.split = pyo.Constraint(m.G, m.T, rule=split)
    def upcap(m, j, t):
        return m.up[j, t] <= max(gmap[j].pmax - ytil.get((j, t), 0.0), 0.0)
    m.upcap = pyo.Constraint(m.G, m.T, rule=upcap)
    def dncap(m, j, t):
        return m.dn[j, t] <= max(ytil.get((j, t), 0.0), 0.0)
    m.dncap = pyo.Constraint(m.G, m.T, rule=dncap)

    # Effective turn-up price, floored at the unit's turn-down bid. A rational
    # ladder always has offer >= bid; flooring guards against data artefacts
    # (e.g. an aggregated offer that came back as 0) that would otherwise make the
    # objective reward simultaneous up+dn and render the LP unbounded.
    def oeff(j):
        return max(gmap[j].o() * (1.0 + markup), gmap[j].b())
    rc = sum(oeff(j) * m.up[j, t] - gmap[j].b() * m.dn[j, t]
             for j in m.G for t in m.T)
    # add start-up no-load cost if recommitment is allowed
    if with_commit:
        rc = rc + sum(gmap[j].noload * m.u[j, t] for j in m.GC for t in m.T)
    m.obj = pyo.Objective(expr=rc, sense=pyo.minimize)

    # request LP duals of the boundary limits to read off congestion rent pi_{b,t}
    if not with_commit:
        m.dual = pyo.Suffix(direction=pyo.Suffix.IMPORT)
    res = _solver(solver).solve(m)
    _check_optimal(res, tag="redispatch")
    out = _extract(m, sys, gmap, with_commit=with_commit, tag="redispatch")
    out["RC"] = float(pyo.value(m.obj))
    # Welfare must charge no-load only for *committed* units. Under tight BM the
    # committed set is the self-schedule's u~ (=blind["u"]); _extract defaults
    # with_commit=False units to u=1, which would wrongly charge no-load for units
    # that are off, so use blind["u"] here. Under recommitment use the solved u.
    u_phys = out["u"] if with_commit else blind.get("u", {})
    out["W_BM"] = _physical_welfare(sys, out["y"], out["x"], u_phys)

    # ---- Proposition 2 decomposition: RC = R_cong + M + residual -------------
    up_val = {(j, t): float(pyo.value(m.up[j, t])) for j in m.G for t in m.T}
    M = sum((oeff(j) - gmap[j].cost) * up_val[(j, t)]
            for j in m.G for t in m.T)
    R_cong = _congestion_rent(sys, blind, m, with_commit)
    out["up"] = up_val
    out["dn"] = {(j, t): float(pyo.value(m.dn[j, t])) for j in m.G for t in m.T}
    out["M_markup"] = float(M)
    out["R_cong"] = float(R_cong)
    out["residual"] = float(out["RC"] - R_cong - M)
    return out


def _congestion_rent(sys, blind, m, with_commit) -> float:
    """R_cong = sum_b pi_{b,t} * s_{b,t}: boundary shadow price times relieved
    overload. pi is the LP dual of the boundary transfer limit (available only
    when commitment is fixed, i.e. the convex redispatch LP); s is how far the
    network-blind net export across the boundary exceeded the ETYS capability."""
    if with_commit or not hasattr(m, "dual") or not hasattr(m, "bnd"):
        return 0.0
    ytil = blind["y"]
    lmap = {l.name: l for l in sys.loads}
    total = 0.0
    for b in sys.boundaries:
        north = set(b.north)
        for t in sys.periods:
            gen = sum(ytil.get((g.name, t), 0.0)
                      for g in sys.gens if g.zone in north)
            dem = sum(lmap[l].mw for l in [ld.name for ld in sys.loads]
                      if lmap[l].zone in north)
            blind_flow = gen - dem
            overload = max(abs(blind_flow) - b.limit, 0.0)     # s_{b,t}
            try:
                pi = abs(float(m.dual[m.bnd[b.name, t]]))       # |pi_{b,t}|
            except (KeyError, AttributeError):
                pi = 0.0
            total += pi * overload
    return total


# --------------------------------------------------------------------------- #
# 4. Nodal PE-A (minimise make-whole payments under per-zone LA prices)
# --------------------------------------------------------------------------- #
def solve_pea(sys: System, nodal: Dict, solver="auto") -> Dict:
    """BKM PE-A, boundary-network version. Given the efficient dispatch `nodal`,
    find per-zone, per-period prices minimising total generator+buyer make-whole
    payments subject to hourly individual rationality and non-negative congestion
    revenue. (LA prices here = per-zone, the nodal analogue.)"""
    m = pyo.ConcreteModel()
    Z = list(sys.zones); Tt = list(sys.periods)
    m.lam = pyo.Var(Z, Tt, domain=pyo.NonNegativeReals)       # zonal prices
    m.dJ = pyo.Var([g.name for g in sys.gens], Tt, domain=pyo.NonNegativeReals)
    gmap = {g.name: g for g in sys.gens}
    y = nodal["y"]; u = nodal["u"]

    # generator IR per period: price*output - var cost - noload + MWP >= 0
    def ir(m, j, t):
        g = gmap[j]
        rev = m.lam[g.zone, t] * y[(j, t)]
        cost = g.cost * y[(j, t)] + g.noload * (u.get((j, t), 0.0)
                                                if g.can_commit else 0.0)
        return rev - cost + m.dJ[j, t] >= 0
    m.ir = pyo.Constraint([g.name for g in sys.gens], Tt, rule=ir)

    # non-negative congestion revenue: sum_z price_z * net_export_z >= 0
    def congrev(m, t):
        return sum(m.lam[z, t] *
                   (sum(y[(g.name, t)] for g in sys.gens if g.zone == z)
                    - sum(l.mw for l in sys.loads if l.zone == z))
                   for z in Z) >= 0
    m.congrev = pyo.Constraint(Tt, rule=congrev)

    m.obj = pyo.Objective(
        expr=sum(m.dJ[g.name, t] for g in sys.gens for t in Tt),
        sense=pyo.minimize)
    _solver(solver).solve(m)
    return {"MWP_PEA": float(pyo.value(m.obj)),
            "lambda": {(z, t): float(m.lam[z, t].value) for z in Z for t in Tt}}


# --------------------------------------------------------------------------- #
# Extraction & metrics
# --------------------------------------------------------------------------- #
def _extract(m, sys, gmap, with_commit, tag):
    y = {(j, t): float(pyo.value(m.y[j, t])) for j in m.G for t in m.T}
    x = {(d, t): float(pyo.value(m.x[d, t])) for d in m.D for t in m.T}
    u = {}
    if with_commit:
        u = {(j, t): float(pyo.value(m.u[j, t])) for j in m.GC for t in m.T}
    else:
        u = {(g.name, t): 1.0 for g in sys.gens for t in sys.periods
             if g.can_commit}
    return {"tag": tag, "y": y, "x": x, "u": u,
            "W": float(pyo.value(m.obj)) if tag in ("nodal", "blind") else None}


def _physical_welfare(sys, y, x, u):
    gmap = {g.name: g for g in sys.gens}; lmap = {l.name: l for l in sys.loads}
    val = sum(lmap[d].value * x[(d, t)] for d in [l.name for l in sys.loads]
              for t in sys.periods if lmap[d].flexible)
    vcost = sum(gmap[j].cost * y[(j, t)] for j in gmap for t in sys.periods)
    ncost = sum(gmap[j].noload * u.get((j, t), 0.0) for j in gmap
                for t in sys.periods if gmap[j].can_commit)
    return val - vcost - ncost


def gamma_decomposition(sys, nodal, blind, redispatch, pea) -> Dict:
    RC = redispatch["RC"]
    MWP = pea["MWP_PEA"]
    return {
        "RC_redispatch_cost": RC,
        "R_cong_congestion_rent": redispatch.get("R_cong", 0.0),
        "M_strategic_markup": redispatch.get("M_markup", 0.0),
        "residual": redispatch.get("residual", 0.0),
        "MWP_nodal_PEA": MWP,
        "Gamma_gap": RC - MWP,
        "W_nodal_star": nodal["W"],
        "W_BM_realised": redispatch["W_BM"],
        "welfare_loss_P1": nodal["W"] - redispatch["W_BM"],
        "lambda_nat": blind["lambda_nat"],
    }


# --------------------------------------------------------------------------- #
# Synthetic Scotland / England (B6) instance -- runs out of the box
# --------------------------------------------------------------------------- #
def synthetic_b6() -> System:
    """Clean *convex* Scotland/England (B6) instance used as the worked example
    in the paper (Section 3.1). Cheap Scottish wind north of a constrained
    boundary, dearer English gas south, demand 100 (SCO) + 900 (ENG) = 1000 MW,
    one settlement period.

    Walk-through (cost-reflective o=b=c, no markup):
      Stage 1 (network-blind): wind 800 @2, gas 200 @60 -> lambda_nat = 60.
      B6 net export = 800 - 100 = 700 > limit 500 -> overload s = 200 MW.
      Stage 2 (redispatch): turn wind DOWN 200 (800->600), gas UP 200 (200->400).
        RC = 60*200 - 2*200 = 11,600.
        Boundary shadow price pi = c_ENG - c_SCO = 60 - 2 = 58.
        R_cong = pi * s = 58 * 200 = 11,600 = RC, M = 0 (Proposition 2(i)-(ii)).
      Convex economy => W_BM = W* (Proposition 1(a)) and MWP_PEA = 0,
        so Gamma = RC - MWP = 11,600.
    All four generators are convex (`can_commit=False`); see
    `synthetic_b6_nonconvex` for the Proposition 1(b) commitment variant."""
    gens = [
        Generator("SCO_wind", "SCO", cost=2.0, pmax=800, can_commit=False),
        Generator("ENG_gas", "ENG", cost=60.0, pmax=1000, can_commit=False,
                  offer=60.0),
    ]
    loads = [Load("SCO_dem", "SCO", mw=100), Load("ENG_dem", "ENG", mw=900)]
    # B6 transfer capability well below the Scottish surplus -> binds
    bnd = [Boundary("B6", north=["SCO"], limit=500.0)]
    return System(zones=["SCO", "ENG"], periods=[1], gens=gens,
                  loads=loads, boundaries=bnd)


def synthetic_b6_nonconvex(noload_scale: float = 1.0) -> System:
    """Nonconvex multi-unit B6 instance that genuinely exhibits Proposition 1(b):
    the network-blind Stage-1 commitment differs from the network optimum
    (u~ != u*), so the tight-BM redispatch realises W_BM < W*.

    Demand 100 (SCO) + 1000 (ENG) = 1100 MW; B6 limit 500 MW; one period. Units:
      SCO_wind  cost 2,  pmax 600, non-committable (cheap, behind B6)
      SCO_ccgt  cost 30, pmin 100, pmax 400, no-load 2000, committable
      ENG_base  cost 40, pmin 150, pmax 500, no-load 3000, committable
      ENG_peak  cost 80, pmin 100, pmax 600, no-load 5000, committable

    Walk-through (cost-reflective):
      Stage 1 (copper-plate) commits the *cheap* SCO_ccgt and ENG_base:
        u~ = {SCO_ccgt:1, ENG_base:1}; wind 550, SCO_ccgt 400, ENG_base 150.
      The network optimum stays within B6 with wind alone north of the cut:
        u* = {SCO_ccgt:0, ENG_base:1}; wind 600, ENG_base 500; cost 24,200.
      Tight BM (u=u~) cannot stand SCO_ccgt down: it is stuck at P_min=100 behind
        B6, wasting its 2,000 no-load and displacing 100 MW of wind (+2,800), so
        W*-W_BM = 4,800 -- a pure commitment-nonconvexity artefact (Prop 1(b)).
      Recommitment re-optimises the commitment and recovers W* (Remark 1).
    `noload_scale` multiplies the committable no-load costs, for the comparative
    static on the degree of nonconvexity (the loss -> the min-load component as
    no-load -> 0, and -> 0 in the fully convex relaxation)."""
    h = noload_scale
    gens = [
        Generator("SCO_wind", "SCO", cost=2.0, pmax=600, can_commit=False,
                  bid=-50.0),                       # negative bid: CfD-style
        Generator("SCO_ccgt", "SCO", cost=30.0, pmin=100, pmax=400,
                  noload=2000.0 * h),
        Generator("ENG_base", "ENG", cost=40.0, pmin=150, pmax=500,
                  noload=3000.0 * h, offer=40.0),
        Generator("ENG_peak", "ENG", cost=80.0, pmin=100, pmax=600,
                  noload=5000.0 * h, offer=80.0),
    ]
    loads = [Load("SCO_dem", "SCO", mw=100), Load("ENG_dem", "ENG", mw=1000)]
    bnd = [Boundary("B6", north=["SCO"], limit=500.0)]
    return System(zones=["SCO", "ENG"], periods=[1], gens=gens,
                  loads=loads, boundaries=bnd)


def run_demo():
    sys = synthetic_b6()
    nodal = solve_nodal_dcopf(sys)
    blind = solve_network_blind(sys)
    redis = solve_redispatch(sys, blind, commitment_policy="fixed", markup=0.0)
    pea = solve_pea(sys, nodal)
    g = gamma_decomposition(sys, nodal, blind, redis, pea)
    print("National price (per period):", g["lambda_nat"])
    print(f"W*  (nodal efficient)      : {g['W_nodal_star']:.1f}")
    print(f"W_BM (national+redispatch) : {g['W_BM_realised']:.1f}")
    print(f"Welfare loss (P1)          : {g['welfare_loss_P1']:.1f}")
    print(f"RC  (redispatch cost)      : {g['RC_redispatch_cost']:.1f}")
    print(f"  R_cong (congestion rent) : {g['R_cong_congestion_rent']:.1f}")
    print(f"  M  (strategic markup)    : {g['M_strategic_markup']:.1f}")
    print(f"  residual (bid-side/MIP)  : {g['residual']:.1f}")
    print(f"MWP (nodal PE-A)           : {g['MWP_nodal_PEA']:.1f}")
    print(f"Gamma = RC - MWP           : {g['Gamma_gap']:.1f}")
    return g


# --------------------------------------------------------------------------- #
# Data loaders for the real GB artifacts
# --------------------------------------------------------------------------- #
# CURRENT empirical path (implemented in gb_empirical_pipeline.py):
#   * Faithful 2-zone B6 instance anchored to FUELHH (GB generation by fuel,
#     incl. embedded wind) and the national demand outturn, reduced to SCO/ENG
#     with documented calibration shares:  ep.build_zonal_instance(date, period)
#   * Stage-2 prices/curtailment from BOD + SO-flagged BOALF:  ep._bod_prices,
#     ep._so_redispatch, ep._representative_wind_bid
#   * Raw-PN reference instance (does NOT bind B6):  ep.load_bmrs_boalf_fpn
#
# GOLD-STANDARD upgrade (multi-zone) -- implement these to replace the 2-zone
# reduction with the full NESO Reduced Model + FES. Left as documented contracts
# because they require the user's downloaded data files.
def load_neso_reduced_model(path: str) -> System:
    """NESO GB Reduced Model (28/36 zones, 2024 ETYS), PowerFactory/CSV export.
    https://www.neso.energy/publications/gb-36-bus-electricity-transmission-network-model
    Expected to map: zones->System.zones; generators (by fuel type)->Generator
    with archetype costs (see calibration); inter-zonal boundaries->Boundary with
    ETYS capabilities. Until wired, use ep.build_zonal_instance for the B6 cut."""
    raise NotImplementedError("Use gb_empirical_pipeline.build_zonal_instance for "
                              "the B6 2-zone reduction; implement here for full multi-zone.")

def load_etys_boundaries(path: str) -> List[Boundary]:
    """ETYS boundary capabilities (B-numbers incl. B4/B6). CSV with columns
    {boundary, capability_MW}; B6 default is used by the 2-zone builder."""
    raise NotImplementedError

def load_fes_scenario(path: str, year: int, scenario: str) -> Tuple:
    """FES zonal demand + generation capacity by scenario & renewable trajectory,
    for the Clean Power 2030 sensitivity. CSV with {zone, year, scenario, fuel,
    capacity_MW, demand_MW}. Feeds the wind/demand shares of build_zonal_instance."""
    raise NotImplementedError

# NOTE: the BMRS BOD / BOALF / FPN loaders are IMPLEMENTED, in
# gb_empirical_pipeline.py and bmrs_client.py, not here:
#   bmrs_client.fetch_bod / fetch_boalf / fetch_pn / fetch_mid / fetch_fuelhh /
#   fetch_demand ; ep._bod_prices, ep._so_redispatch, ep.load_bmrs_boalf_fpn.


if __name__ == "__main__":
    run_demo()
