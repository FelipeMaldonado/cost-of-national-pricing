"""
gb_empirical_pipeline.py
========================
Wires real Elexon BMRS data into `gb_two_stage_skeleton` to produce the first
*empirical* Gamma for the B6 (Scotland -> England) boundary, replacing the
synthetic instance.

Pipeline (all anchored to the GB settlement architecture):
  Stage 1 (self-schedule)  <- PN   (Physical Notifications), aggregated to SCO/ENG
  offer/bid prices         <- BOD  (Bid-Offer Data) per BM unit
  Stage 2 (redispatch)     <- BOALF with soFlag == True  (constraint actions only)

It also computes an *observed* redispatch cost directly from SO-flagged BOALF x
BOD prices, as an empirical anchor to validate the modelled RC, and selects the
highest-curtailment day in a window automatically.

NOTE / approximations (this is the first wiring, to be refined):
  * Zone split uses the BM unit registry gspGroup (_N, _P = Scotland) with a
    fuelType=='WIND' tag; verify against the live registry.
  * Per-BMU period price proxy = first turn-down bid band (pair -1) and first
    turn-up offer band (pair +1) from BOD.
  * BOA volume proxy = (instructed level - PN level), integrated to MW per period.
  * B6 limit defaults to the ETYS B6 capability; set the day-specific value if known.

Requires: pandas, plus the deps of bmrs_client and gb_two_stage_skeleton.
"""

from __future__ import annotations

import datetime as _dt
import math
from functools import lru_cache
from typing import Dict, List, Tuple

import pandas as pd
import requests

import bmrs_client as bm
from gb_two_stage_skeleton import (
    Boundary,
    Generator,
    Load,
    System,
    gamma_decomposition,
    solve_nodal_dcopf,
    solve_pea,
    solve_redispatch,
)

SCOTLAND_GSP = {"_N", "_P"}  # Northern (SSEH) and Southern (SPD) Scotland
# Zonal transmission-loss-factor cut for Scotland. GB TLFs are assigned by
# geographic zone; Scotland comprises the most-negative zones (furthest from the
# GB demand centre): transmission-connected Scottish wind sits at -0.0200
# (North Scotland) or -0.0066 (South Scotland), while the next zone south
# (Cumbria/NW England -- Robin Rigg, Walney, Morecambe) is -0.0045. The clean gap
# at ~-0.0055 separates Scotland. This is needed because GSP Group is blank for
# transmission-connected (T_) units, so the GSP test alone misclassifies every
# transmission-connected Scottish wind farm (Seagreen, Moray, Beatrice,
# Whitelee, ...) -- which dominate B6 curtailment -- as England.
SCOTLAND_TLF_MAX = -0.0055
B6_LIMIT_MW = 6100.0  # ETYS B6 capability (set day-specific if known)
N_TOP_WIND = 8  # keep this many Scottish wind BMUs individually


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def classify_units(reg: pd.DataFrame) -> pd.DataFrame:
    """Return a frame indexed by bmUnit with columns {zone, fuel}.

    A unit is classified Scottish (north of B6) if its GSP Group is _N/_P
    (distribution-/embedded-connected) OR its zonal transmission loss factor is at
    most SCOTLAND_TLF_MAX. The TLF leg is essential: GSP Group is blank for
    transmission-connected (T_) units, so the GSP test alone drops every
    transmission-connected Scottish wind farm, which carry the bulk of B6
    curtailment.
    """
    df = reg.copy()
    # column names in the registry vary; normalise the ones we need
    col = {c.lower(): c for c in df.columns}
    bm_col = col.get("elexonbmunit", col.get("bmunit", col.get("nationalgridbmunit")))
    gsp_col = col.get("gspgroupid", col.get("gspgroup"))
    fuel_col = col.get("fueltype", col.get("bmunittype"))
    tlf_col = col.get("transmissionlossfactor")
    tlf = (pd.to_numeric(df[tlf_col], errors="coerce").to_numpy()
           if tlf_col else np.full(len(df), np.nan))
    out = pd.DataFrame(
        {
            "bmUnit": df[bm_col],
            "gsp": df[gsp_col] if gsp_col else "",
            "fuel": (df[fuel_col].astype(str).str.upper() if fuel_col else ""),
        }
    )
    out["zone"] = [
        "SCO" if (str(g) in SCOTLAND_GSP or (t == t and t <= SCOTLAND_TLF_MAX))
        else "ENG"
        for g, t in zip(out["gsp"], tlf)
    ]
    return out.set_index("bmUnit")


# --------------------------------------------------------------------------- #
# Helpers to reduce raw datasets to per-BMU, per-period quantities
# --------------------------------------------------------------------------- #
def _pn_per_period(pn: pd.DataFrame) -> pd.DataFrame:
    """Average levelFrom/levelTo to one MW per (bmUnit, settlementPeriod)."""
    df = pn.copy()
    df["mw"] = (df["levelFrom"] + df["levelTo"]) / 2.0
    return df.groupby(["bmUnit", "settlementPeriod"], as_index=False)["mw"].mean()


def _bod_prices(bod: pd.DataFrame) -> pd.DataFrame:
    """First turn-up offer (pair +1) and first turn-down bid (pair -1) per
    (bmUnit, settlementPeriod)."""
    df = bod.copy()
    pair = "bidOfferPairNumber" if "bidOfferPairNumber" in df else "pairId"
    up = (
        df[df[pair] == 1]
        .groupby(["bmUnit", "settlementPeriod"])["offer"]
        .first()
        .rename("offer")
    )
    dn = (
        df[df[pair] == -1]
        .groupby(["bmUnit", "settlementPeriod"])["bid"]
        .first()
        .rename("bid")
    )
    return pd.concat([up, dn], axis=1).reset_index()


def _periodize_boalf(boalf: pd.DataFrame) -> pd.DataFrame:
    """Explode each BOALF acceptance across the settlement periods it spans.

    BMRS BOALF is *time-indexed*: each acceptance carries
    `settlementPeriodFrom`/`settlementPeriodTo` and `timeFrom`/`timeTo`, but no
    single `settlementPeriod` column (most acceptances sit in one period; spans of
    a few periods occur). We map one acceptance to one row per covered period,
    each carrying the mean instructed `level` ((levelFrom+levelTo)/2), the
    `soFlag`, and the acceptance duration `hours` apportioned evenly across the
    periods it covers. Returns [bmUnit, settlementPeriod, level, soFlag, hours]."""
    cols = ["bmUnit", "settlementPeriod", "level", "soFlag", "hours"]
    if boalf is None or boalf.empty:
        return pd.DataFrame(columns=cols)
    df = boalf.copy()
    df["level"] = (df["levelFrom"] + df["levelTo"]) / 2.0
    if "soFlag" not in df.columns:
        df["soFlag"] = False
    rows = []
    for r in df.itertuples(index=False):
        p0 = int(getattr(r, "settlementPeriodFrom"))
        p1 = int(getattr(r, "settlementPeriodTo"))
        if p1 < p0:
            p0, p1 = p1, p0
        periods = range(p0, p1 + 1)
        n = len(periods)
        hrs = _hours(getattr(r, "timeFrom"), getattr(r, "timeTo")) / n
        for p in periods:
            rows.append((r.bmUnit, p, float(r.level), bool(r.soFlag), hrs))
    return pd.DataFrame(rows, columns=cols)


def _so_redispatch(boalf: pd.DataFrame, pn: pd.DataFrame) -> pd.DataFrame:
    """SO-flagged accepted volume per (bmUnit, settlementPeriod): instructed
    level minus PN level (turn-down negative, turn-up positive)."""
    per = _periodize_boalf(boalf)
    so = per[per["soFlag"]] if not per.empty else per
    if so.empty:
        return pd.DataFrame(columns=["bmUnit", "settlementPeriod", "delta_mw"])
    instr = (
        so.groupby(["bmUnit", "settlementPeriod"], as_index=False)["level"]
        .mean()
        .rename(columns={"level": "instr"})
    )
    pnp = _pn_per_period(pn).rename(columns={"mw": "pn"})
    m = instr.merge(pnp, on=["bmUnit", "settlementPeriod"], how="left").fillna(
        {"pn": 0.0}
    )
    m["delta_mw"] = m["instr"] - m["pn"]  # <0 turn-down (curtailment)
    return m[["bmUnit", "settlementPeriod", "delta_mw"]]


# --------------------------------------------------------------------------- #
# Peak-day selection
# --------------------------------------------------------------------------- #
def find_peak_curtailment_day(dates: List[str]) -> Tuple[str, pd.Series]:
    """Rank candidate days by total SO-flagged Scottish-wind turn-down volume."""
    reg = classify_units(bm.fetch_registry())
    scores = {}
    for d in dates:
        try:
            boalf, pn = bm.fetch_boalf(d), bm.fetch_pn(d)
        except Exception:
            continue
        rd = _so_redispatch(boalf, pn)
        rd = rd.join(reg, on="bmUnit")
        turndown = -rd.loc[
            (rd["zone"] == "SCO") & (rd["fuel"] == "WIND") & (rd["delta_mw"] < 0),
            "delta_mw",
        ].sum()
        scores[d] = turndown
    s = pd.Series(scores).sort_values(ascending=False)
    return (s.index[0] if len(s) else dates[0]), s


# --------------------------------------------------------------------------- #
# The wired loader  (replaces the stub in the skeleton)
# --------------------------------------------------------------------------- #
def load_bmrs_boalf_fpn(date: str, period: int) -> Tuple[System, Dict]:
    """Build a *single-period* 2-zone (SCO/ENG) System and Stage-1 `blind` dict
    from raw BM PNs for one settlement period.

    NOTE (2026-06): RETAINED FOR REFERENCE ONLY -- not used in any reported
    figure. Raw PNs miss embedded Scottish wind (~1.5 GW visible vs ~8-12 GW
    real), so B6 never binds in this instance; every reported structural result
    uses ``build_zonal_instance`` (the FUELHH/demand-anchored builder) instead.
    Reachable only via ``solve_day(..., instance="pn")``/``run_paper`` with
    ``INSTANCE != "zonal"``, which no script sets. Kept to document why the
    naive PN-only reduction is inadequate."""
    reg = classify_units(bm.fetch_registry())
    pn = _pn_per_period(bm.fetch_pn(date))
    pn = pn[pn["settlementPeriod"] == period].join(reg, on="bmUnit")
    prices = _bod_prices(bm.fetch_bod(date)).set_index(["bmUnit", "settlementPeriod"])

    def price(bmu, kind, default):
        try:
            v = prices.loc[(bmu, period), kind]
            return float(v) if pd.notna(v) else default
        except KeyError:
            return default

    t = period
    gens: List[Generator] = []
    blind_y: Dict[Tuple[str, int], float] = {}
    loads: List[Load] = []

    # demand: PN < 0 are consumption BM units, aggregated per zone
    dem = pn[pn["mw"] < 0].assign(mw=lambda d: -d["mw"])
    for z in ("SCO", "ENG"):
        loads.append(Load(f"{z}_dem", z, mw=float(dem[dem["zone"] == z]["mw"].sum())))

    gen = pn[pn["mw"] >= 0].copy()
    sco_wind = gen[(gen["zone"] == "SCO") & (gen["fuel"] == "WIND")]
    top_ids = (
        sco_wind.groupby("bmUnit")["mw"]
        .sum()
        .sort_values(ascending=False)
        .head(N_TOP_WIND)
        .index
    )

    # individual Scottish wind units (real turn-down bids)
    for bmu in top_ids:
        mw = float(sco_wind[sco_wind["bmUnit"] == bmu]["mw"].sum())
        g = Generator(
            bmu,
            "SCO",
            cost=1.0,
            pmax=max(mw, 1.0),
            can_commit=False,
            bid=price(bmu, "bid", -50.0),
        )
        gens.append(g)
        blind_y[(bmu, t)] = mw

    def add_aggregate(name, zone, mask, cost):
        mw = float(gen[mask]["mw"].sum())
        gg = Generator(name, zone, cost=cost, pmax=max(mw, 1.0), can_commit=False)
        gens.append(gg)
        blind_y[(name, t)] = mw
        return gg

    add_aggregate(
        "SCO_other",
        "SCO",
        (gen["zone"] == "SCO") & (~gen["bmUnit"].isin(top_ids)),
        cost=35.0,
    )
    eng_gen = add_aggregate("ENG_gen", "ENG", (gen["zone"] == "ENG"), cost=60.0)
    eng_offer = prices.reset_index().merge(reg, left_on="bmUnit", right_index=True)
    eng_offer = eng_offer[
        (eng_offer["zone"] == "ENG") & (eng_offer["settlementPeriod"] == t)
    ]["offer"].dropna()
    # screen BMRS sentinel/non-positive offers (median of raw offers is often 0)
    eng_offer = eng_offer[(eng_offer > 0) & (eng_offer < SENTINEL_PRICE)]
    # replacement-gas turn-up offer: keep it at or above the unit cost so it is a
    # genuine turn-up price (offer >= bid), never below (which would be arbitrage)
    eng_gen.offer = max(float(eng_offer.median()) if len(eng_offer) else 80.0,
                        eng_gen.cost)
    eng_gen.pmax = eng_gen.pmax + 5000.0  # replacement-gas headroom

    sys = System(
        zones=["SCO", "ENG"],
        periods=[t],
        gens=gens,
        loads=loads,
        boundaries=[Boundary("B6", north=["SCO"], limit=B6_LIMIT_MW)],
    )
    blind = {"y": blind_y, "u": {}}
    return sys, blind


# --------------------------------------------------------------------------- #
# Faithful zonal instance (FUELHH + demand outturn anchored)                   #
# --------------------------------------------------------------------------- #
# Raw BM PNs miss embedded Scottish wind (they showed ~1.5 GW vs ~8-12 GW real),
# so B6 never bound. This builder instead anchors the zonal *totals* to published
# system data -- GB generation by fuel (FUELHH, which includes embedded wind) and
# national demand outturn -- and reduces them to the SCO/ENG split across B6 with
# explicit, documented calibration shares. FPN/BOD/BOALF still supply the
# per-unit curtailment detail (the representative Scottish-wind turn-down bid and
# the English replacement-gas offer). The instance is balanced by construction
# (total generation = total demand), so the redispatch is energy-conserving and
# free of the phantom-rebalancing artefact of the PN-only builder.
SCOTLAND_WIND_SHARE = 0.50     # Scotland's share of GB WIND output (on+offshore+embedded)
SCOTLAND_DEMAND_SHARE = 0.095  # Scotland's share of GB demand (~9-10%)
SCOTLAND_NONWIND_MW = 2500.0   # behind-B6 must-run base (hydro/PS + Torness etc.),
                               # modelled as a local demand offset so WIND is the
                               # marginal curtailment (the empirical reality)
WIND_COST = 2.0
ENG_GAS_COST = 60.0


# NESO "Day-Ahead Constraint Flows and Limits": the actual half-hourly B6
# (Constraint Group SCOTEX) power flow and the *outage-adjusted effective limit*.
# These replace the fixed-share overload with the OBSERVED relieved overload
# s_t = (flow_t - limit_t)^+, the B2 calibration target.
SCOTEX_RESOURCE = "38a18ec1-9e40-465d-93fb-301e80fd1352"


@lru_cache(maxsize=None)
def fetch_scotex(date: str) -> Dict[int, Tuple[float, float]]:
    """Per-settlement-period B6 flow and effective limit (MW) for `date`, from
    NESO's Day-Ahead Constraint Flows and Limits (Constraint Group SCOTEX).
    Returns {settlementPeriod: (flow_MW, limit_MW)}; settlement period is the
    clock half-hour (00:00->1 ... 23:30->48). Cached in-process per day."""
    nxt = (_dt.date.fromisoformat(date) + _dt.timedelta(days=1)).isoformat()
    sql = (f'SELECT "Date (GMT/BST)" AS d, "Limit (MW)" AS lim, "Flow (MW)" AS flow '
           f'FROM "{SCOTEX_RESOURCE}" WHERE "Constraint Group"=\'SCOTEX\' '
           f'AND "Date (GMT/BST)" >= \'{date}\' AND "Date (GMT/BST)" < \'{nxt}\' '
           f'ORDER BY d')
    try:
        r = requests.get(NESO_CKAN_SQL, params={"sql": sql}, timeout=90)
        r.raise_for_status()
        recs = r.json()["result"]["records"]
    except Exception:
        return {}
    out: Dict[int, Tuple[float, float]] = {}
    for rec in recs:
        ts = pd.Timestamp(rec["d"])
        sp = ts.hour * 2 + (1 if ts.minute < 30 else 2)
        out[sp] = (float(rec["flow"]), float(rec["lim"]))
    return out


def _representative_wind_bid(date: str, period: int) -> float:
    """Volume-weighted Scottish-wind turn-down bid (negative, GBP/MWh) from BOD,
    screened of sentinels; the price NESO pays to curtail. Default -65."""
    reg = classify_units(bm.fetch_registry())
    px = _bod_prices(bm.fetch_bod(date))
    px = px[px["settlementPeriod"] == period].join(reg, on="bmUnit")
    w = px[(px["zone"] == "SCO") & (px["fuel"] == "WIND")]["bid"].dropna()
    w = w[(w < 0) & (w > -SENTINEL_PRICE)]
    return float(w.median()) if len(w) else -65.0


def build_zonal_instance(date: str, period: int, *,
                         b6_limit: float = B6_LIMIT_MW,
                         wind_share: float = SCOTLAND_WIND_SHARE,
                         demand_share: float = SCOTLAND_DEMAND_SHARE,
                         sco_nonwind_mw: float = SCOTLAND_NONWIND_MW,
                         wind_scale: float = 1.0,
                         flex_mw: float = 0.0,
                         use_scotex: bool = False
                         ) -> Tuple[System, Dict]:
    """Build a faithful, balanced 2-zone (SCO/ENG) B6 instance for one period.

    SCO generation is wind (curtailable, real negative turn-down bid); the
    behind-B6 must-run base offsets local SCO demand so wind is the marginal
    curtailment. ENG is replacement gas (turn-up at the period wholesale price).
    Total generation = total demand, so balance is energy-conserving. B6 binds
    whenever the Scottish wind surplus exceeds the ETYS capability.

    Sensitivity levers (Section 6.1 of the paper):
      wind_scale : multiply Scottish wind output (Clean Power 2030 trajectory).
      flex_mw    : flexible demand / storage relocated *behind* the boundary; it
                   absorbs surplus locally (SCO demand += flex_mw, ENG demand -=
                   flex_mw, total demand conserved), reducing the B6 flow
                   one-for-one -- the flexibility-equivalence channel."""
    t = period
    # national demand (transmission-system outturn, what the BM dispatches)
    dem = bm.fetch_demand(date)
    drow = dem[dem["settlementPeriod"] == period]
    if drow.empty:
        raise ValueError(f"no demand outturn for {date} period {period}")
    col = ("initialTransmissionSystemDemandOutturn"
           if "initialTransmissionSystemDemandOutturn" in drow else "initialDemandOutturn")
    nat_demand = float(drow[col].iloc[0])

    # GB wind from FUELHH (includes embedded output absent from BM PNs)
    fh = bm.fetch_fuelhh(date)
    fhp = fh[(fh["settlementPeriod"] == period) & (fh["fuelType"] == "WIND")]
    gb_wind = float(fhp["generation"].sum()) if len(fhp) else 0.0

    base_net_dem = max(demand_share * nat_demand - sco_nonwind_mw, 0.0)
    sco_net_dem = base_net_dem + max(flex_mw, 0.0)   # flexibility = extra local demand
    sco_wind = wind_scale * wind_share * gb_wind     # FUELHH-anchored surplus (incl. embedded)

    # B2: use NESO's OBSERVED, outage-adjusted SCOTEX *limit* per period (the
    # capability genuinely varies day to day with faults), rather than a fixed
    # ETYS value. We do NOT use the SCOTEX 'Flow' field: it is a day-ahead
    # *unconstrained* forecast that can exceed B6's physical capacity and so
    # overstates the realised overload (the realised, data-driven headline cost is
    # measured separately by realized_b6_congestion_rent).
    if use_scotex and (period in (sx := fetch_scotex(date))):
        b6_limit = sx[period][1]
    eng_dem = max(nat_demand - sco_net_dem, 0.0)
    eng_gas_fpn = max(nat_demand - sco_wind, 0.0)   # balance: gen == demand

    wind_bid = _representative_wind_bid(date, period)
    mid = _mid_reference(date)
    eng_offer = max(mid.get(period, ENG_GAS_COST), ENG_GAS_COST)

    gens = [
        Generator("SCO_wind", "SCO", cost=WIND_COST, pmax=max(sco_wind, 1.0),
                  can_commit=False, bid=wind_bid),
        Generator("ENG_gas", "ENG", cost=ENG_GAS_COST,
                  pmax=eng_gas_fpn + 10000.0, can_commit=False, offer=eng_offer),
    ]
    loads = [Load("SCO_dem", "SCO", mw=sco_net_dem),
             Load("ENG_dem", "ENG", mw=eng_dem)]
    blind_y = {("SCO_wind", t): sco_wind, ("ENG_gas", t): eng_gas_fpn}
    sys = System(zones=["SCO", "ENG"], periods=[t], gens=gens, loads=loads,
                 boundaries=[Boundary("B6", north=["SCO"], limit=b6_limit)])
    return sys, {"y": blind_y, "u": {}}


# --------------------------------------------------------------------------- #
# Reusable per-day solver (one day -> daily GBP totals)                        #
# --------------------------------------------------------------------------- #
DAILY_KEYS = ("RC_costbased", "RC_markup", "R_cong", "M_markup", "MWP_PEA",
              "Gamma_costbased", "Gamma_markup", "welfare_loss_P1")


def solve_day(date: str, *, markup: float, instance: str = "zonal",
              n_periods: int = 48, period_hours: float = 0.5,
              build_kwargs: dict | None = None) -> Dict:
    """Solve every settlement period of `date` and return the daily GBP totals.

    Each per-period power-rate cost is scaled by `period_hours` (a half-hour
    settlement period -> MWh). M is the incremental markup cost RC(mu)-RC(0), so
    Gamma_markup = R_cong + M exactly. Periods that fail to build/solve are
    skipped; `n_solved` records how many contributed. Shared by run_paper.py
    (single day, detailed) and run_annual.py (multi-day aggregation)."""
    from gb_two_stage_skeleton import (solve_nodal_dcopf, solve_redispatch,
                                       solve_pea)
    build = build_zonal_instance if instance == "zonal" else load_bmrs_boalf_fpn
    bkw = build_kwargs or {}
    h = period_hours
    agg = {k: 0.0 for k in DAILY_KEYS}
    solved = 0
    for t in range(1, n_periods + 1):
        try:
            sys, blind = build(date, t, **bkw)
            nodal = solve_nodal_dcopf(sys)
            rd0 = solve_redispatch(sys, blind, commitment_policy="fixed", markup=0.0)
            rdm = solve_redispatch(sys, blind, commitment_policy="fixed", markup=markup)
            pea = solve_pea(sys, nodal)
        except Exception:
            continue
        agg["RC_costbased"] += rd0["RC"] * h
        agg["RC_markup"] += rdm["RC"] * h
        agg["R_cong"] += rd0["R_cong"] * h
        agg["M_markup"] += (rdm["RC"] - rd0["RC"]) * h
        agg["MWP_PEA"] += pea["MWP_PEA"] * h
        agg["Gamma_costbased"] += (rd0["RC"] - pea["MWP_PEA"]) * h
        agg["Gamma_markup"] += (rdm["RC"] - pea["MWP_PEA"]) * h
        agg["welfare_loss_P1"] += (nodal["W"] - rd0["W_BM"]) * h
        solved += 1
    agg["date"] = date
    agg["n_solved"] = solved
    return agg


def stratified_estimator(df: pd.DataFrame, col: str,
                         binsize: Dict[int, int]) -> Tuple[float, float]:
    """Stratified-sampling total and its standard error for column `col`.

    `df` holds the sampled days (one row each) with a `bin` column; `binsize`
    maps each stratum to its population size N_b. Returns
    (C_hat, SE) for C_hat = sum_b N_b * mean_b with the finite-population
    standard error sum_b N_b^2 * s_b^2 / n_b * (1 - n_b/N_b).

    This is the single canonical implementation of the estimator used for the
    annual headline; run_annual.py, robustness_classification.py and
    structural_observed_limit.py all call it so the arithmetic cannot drift
    between scripts."""
    total = se2 = 0.0
    for b, g in df.groupby("bin"):
        Nb, nb = binsize[b], len(g)
        mb = g[col].mean()
        sb = g[col].std(ddof=1) if nb > 1 else 0.0
        total += Nb * mb
        se2 += (Nb ** 2) * (sb ** 2) / nb * (1 - nb / Nb) if Nb else 0.0
    return float(total), float(math.sqrt(se2))


# --------------------------------------------------------------------------- #
# Strategic markup estimation (Proposition 2's mu, MEASURED not assumed)
# --------------------------------------------------------------------------- #
# BMRS uses +/-9999 in BOD ladders as a "price unavailable / do-not-dispatch"
# sentinel; such bands must be screened out before any price statistic.
SENTINEL_PRICE = 9000.0


def estimate_markup(
    date: str,
    wholesale_ref: float | None = None,
    boundary_binding_periods: set | None = None,
    max_offer: float = SENTINEL_PRICE,
) -> Dict[str, float]:
    """Estimate the strategic turn-up markup mu of Proposition 2 from the data,
    rather than assuming the ~30% figure. We regress the price of SO-flagged
    turn-up *offers* on a wholesale reference, conditional on the B6 boundary
    binding: mu = E[(offer - p_ref)/p_ref | SO-flagged turn-up, boundary binds].

    SCOPE NOTE: this is the SINGLE-DAY, cross-unit premium contrast. On a fully
    binding day it has no non-binding control and returns method
    "raw_premium_no_control" (badly upward-biased; run_paper then falls back to
    the 0.30 counterfactual). The paper's IDENTIFICATION HEADLINE (mu_hat=0.00)
    comes from ``estimate_markup_pooled`` -- the within-unit-day design pooled
    over many days, which nets out unit and day fixed effects. Prefer that for
    any reported markup; this function is the per-day diagnostic run_paper uses
    to label B6 days as (un)identified.

    This is a (sharp) regression-discontinuity-style contrast around the
    boundary-binding threshold: when B6 binds, replacement turn-up south of the
    cut is priced at a premium over the wholesale reference; when it does not,
    SO turn-up should price close to it. The gap identifies mu.

    Parameters
    ----------
    wholesale_ref : the wholesale reference price (GBP/MWh). Accepts a scalar
        (constant across periods) or a {settlementPeriod: price} dict. If None
        (default), the per-period BMRS Market Index Data (MID) reference is used,
        so each offer is compared to its own period's wholesale price.
    boundary_binding_periods : settlement periods in which B6 binds (e.g. those
        with SO-flagged Scottish-wind turn-down). If None, all SO turn-ups are
        used (no discontinuity contrast).
    max_offer : screen out BOD sentinel offers (>= 9999, "unavailable") and any
        non-positive prices before computing the markup.

    Returns a dict with the point estimate `mu`, the binding/non-binding means,
    and the sample size; feed `mu` into solve_redispatch(..., markup=mu)."""
    empty = {"mu": 0.0, "n": 0, "p_ref": None,
             "mean_binding": None, "mean_nonbinding": None}
    rd = _so_redispatch(bm.fetch_boalf(date), bm.fetch_pn(date))
    px = _bod_prices(bm.fetch_bod(date)).set_index(["bmUnit", "settlementPeriod"])

    turnups = rd[rd["delta_mw"] > 0].copy()
    if turnups.empty:
        return empty
    turnups["offer"] = [
        float(px["offer"].get((r.bmUnit, r.settlementPeriod), float("nan")))
        for r in turnups.itertuples()
    ]
    # drop missing and sentinel/non-positive offers (BMRS uses 9999 = unavailable)
    turnups = turnups.dropna(subset=["offer"])
    turnups = turnups[(turnups["offer"] > 0) & (turnups["offer"] < max_offer)]
    if turnups.empty:
        return empty

    # per-period wholesale reference p_ref_t
    if wholesale_ref is None:
        ref = _mid_reference(date)                       # MID, volume-weighted
    elif isinstance(wholesale_ref, dict):
        ref = {int(k): float(v) for k, v in wholesale_ref.items()}
    else:
        ref = {int(p): float(wholesale_ref)
               for p in turnups["settlementPeriod"].unique()}
    turnups["p_ref"] = turnups["settlementPeriod"].map(ref)
    turnups = turnups[turnups["p_ref"].notna() & (turnups["p_ref"] > 0)]
    if turnups.empty:
        return empty
    turnups["premium"] = (turnups["offer"] - turnups["p_ref"]) / turnups["p_ref"]

    def _avg(frame):
        return float(frame["premium"].mean()) if len(frame) else None

    p_ref_rep = float(turnups["p_ref"].mean())
    if boundary_binding_periods is not None:
        mask = turnups["settlementPeriod"].isin(boundary_binding_periods)
        mu_bind = _avg(turnups[mask])
        mu_non = _avg(turnups[~mask])
        if mu_bind is not None and mu_non is not None:
            # clean discontinuity estimate: premium attributable to B6 binding
            mu, method = (mu_bind - mu_non), "discontinuity"
        else:
            # no non-binding control on this day -> raw premium, NOT identified
            mu, method = (mu_bind if mu_bind is not None else 0.0), "raw_premium_no_control"
        return {
            "mu": max(mu, 0.0),
            "n": int(len(turnups)),
            "p_ref": p_ref_rep,
            "mean_binding": mu_bind,
            "mean_nonbinding": mu_non,
            "method": method,
        }

    mu = _avg(turnups) or 0.0
    return {
        "mu": max(mu, 0.0),
        "n": int(len(turnups)),
        "p_ref": p_ref_rep,
        "mean_binding": mu,
        "mean_nonbinding": None,
        "method": "raw_premium_no_control",
    }


def _mid_reference(date: str) -> Dict[int, float]:
    """Per-settlement-period wholesale reference price from BMRS Market Index
    Data: the volume-weighted average across providers (APXMIDP/N2EXMIDP) of
    periods with positive traded volume. Returns {settlementPeriod: price}."""
    mid = bm.fetch_mid(date)
    if mid is None or mid.empty:
        return {}
    df = mid[(mid["price"] > 0) & (mid["volume"] > 0)].copy()
    if df.empty:
        return {}
    df["pv"] = df["price"] * df["volume"]
    g = df.groupby("settlementPeriod").agg(pv=("pv", "sum"), v=("volume", "sum"))
    g["ref"] = g["pv"] / g["v"]
    return {int(p): float(r) for p, r in g["ref"].items()}


def realized_b6_congestion_rent(date: str) -> Dict[str, float]:
    """Realized B6 congestion rent for the day, computed directly from settlement
    data (the robust, data-driven headline). For each SO-flagged Scottish-wind
    turn-down (the realized relieved overload), the rent is the boundary price
    difference times the curtailed energy:
        R_cong = sum (p_replace - bid) * volume
    where bid (<0) is the unit's BOD turn-down price (the curtailment payment) and
    p_replace is the period wholesale/MID price (the replacement leg south of the
    cut). This equals curtailment payment + replacement cost on the curtailed MWh.

    Anchoring s to *realized* BOALF curtailment avoids the day-ahead SCOTEX 'Flow'
    field, which is an unconstrained forecast (it can exceed B6's physical
    capacity) and overstates the realized overload. Returns the rent, the
    curtailed volume, and the binding periods (proxied by SO-flagged curtailment).
    """
    reg = classify_units(bm.fetch_registry())
    per = _periodize_boalf(bm.fetch_boalf(date))
    so = per[per["soFlag"]] if not per.empty else per
    pn = _pn_per_period(bm.fetch_pn(date)).set_index(["bmUnit", "settlementPeriod"])
    px = _bod_prices(bm.fetch_bod(date)).set_index(["bmUnit", "settlementPeriod"])
    mid = _mid_reference(date)
    rent = vol = 0.0
    periods = set()
    for a in (so.itertuples(index=False) if len(so) else []):
        if a.bmUnit not in reg.index:
            continue
        z = reg.loc[a.bmUnit]
        if not (z.get("zone") == "SCO" and z.get("fuel") == "WIND"):
            continue
        fpn = float(pn["mw"].get((a.bmUnit, a.settlementPeriod), 0.0))
        delta = a.level - fpn
        if delta >= 0:
            continue
        v = -delta * a.hours                       # curtailed energy (MWh)
        bid = float(px["bid"].get((a.bmUnit, a.settlementPeriod), -65.0))
        if not (-SENTINEL_PRICE < bid < 0):
            bid = -65.0
        p_rep = max(mid.get(a.settlementPeriod, ENG_GAS_COST), ENG_GAS_COST)
        rent += (p_rep - bid) * v                  # (replace - bid) * volume
        vol += v
        periods.add(a.settlementPeriod)
    return {"R_cong_obs": float(rent), "curtailed_MWh": float(vol),
            "binding_periods": len(periods)}


def binding_periods(date: str) -> set:
    """Settlement periods in which B6 binds, proxied by the presence of
    SO-flagged Scottish-wind turn-down (curtailment) in the period."""
    reg = classify_units(bm.fetch_registry())
    rd = _so_redispatch(bm.fetch_boalf(date), bm.fetch_pn(date)).join(reg, on="bmUnit")
    cur = rd[(rd["zone"] == "SCO") & (rd["fuel"] == "WIND") & (rd["delta_mw"] < 0)]
    return set(cur["settlementPeriod"].unique())


def scotex_binding_periods(date: str) -> set:
    """Settlement periods in which B6 binds, taken directly from the observed
    SCOTEX data: those where the day-ahead flow exceeds the (outage-adjusted)
    limit. Sharper than the curtailment proxy and the basis for the RD control."""
    return {p for p, (f, l) in fetch_scotex(date).items() if f > l}


def _markup_panel(dates: List[str]) -> pd.DataFrame:
    """Long panel for the markup RD: one row per SO-flagged turn-up acceptance,
    with its turn-up premium over the period wholesale (MID) price and a SCOTEX
    binding flag. Sentinel/non-positive offers and missing MID are screened."""
    rows = []
    for d in dates:
        try:
            rd = _so_redispatch(bm.fetch_boalf(d), bm.fetch_pn(d))
            px = _bod_prices(bm.fetch_bod(d)).set_index(["bmUnit", "settlementPeriod"])
            mid = _mid_reference(d)
            binding = scotex_binding_periods(d)
        except Exception:
            continue
        for r in rd[rd["delta_mw"] > 0].itertuples():
            offer = px["offer"].get((r.bmUnit, r.settlementPeriod), float("nan"))
            pref = mid.get(r.settlementPeriod)
            if not (offer == offer) or offer <= 0 or offer >= SENTINEL_PRICE:
                continue
            rows.append({"date": d, "unit": r.bmUnit, "period": int(r.settlementPeriod),
                         "offer": float(offer),
                         "premium": ((offer - pref) / pref
                                     if pref and pref > 0 else float("nan")),
                         "binding": r.settlementPeriod in binding})
    return pd.DataFrame(rows)


def estimate_markup_pooled(dates: List[str]) -> Dict:
    """Regression-discontinuity estimate of the strategic (boundary-induced) markup
    mu of Proposition 2 across many days, identified from binding vs non-binding
    variation. The clean specification is within-unit-day: for a unit that is
    turned up in both B6-binding and non-binding periods of the same day, the
    proportional change in its own offer level,
    (offer_binding - offer_nonbinding)/offer_nonbinding. Unit and day fixed effects
    remove the unit's marginal cost and the day's wholesale level, so the estimate
    is the markup a unit adds specifically when the boundary binds.

    The naive premium-over-wholesale contrast is also returned for comparison; it
    is badly upward-biased because binding periods are low-wholesale (overnight,
    high wind), which inflates the offer/MID ratio mechanically -- the bias the
    within-unit-day offer-level design removes.

    This is the paper's strategic-markup IDENTIFICATION result (mu_hat=0.00; see
    Section 6 and Reproducibility). run_annual.py calls it on the sampled days and
    prints mu_hat so the figure reproduces from a script; see also
    ``estimate_markup`` for the weaker single-day diagnostic."""
    df = _markup_panel(dates)
    empty = {"mu": None, "se": None, "n_unit_days": 0, "n_obs": 0,
             "mu_naive_premium": None}
    if df.empty:
        return empty
    # headline: within-unit-day proportional change in the offer LEVEL
    deltas = []
    for _, g in df.groupby(["date", "unit"]):
        b, nb = g[g["binding"]]["offer"], g[~g["binding"]]["offer"]
        if len(b) and len(nb) and nb.mean() > 0:
            deltas.append((b.mean() - nb.mean()) / nb.mean())
    # naive cross-unit premium-over-MID contrast (biased; for comparison)
    pdf = df.dropna(subset=["premium"])
    naive = []
    for _, g in pdf.groupby("date"):
        bp, nbp = g[g["binding"]]["premium"], g[~g["binding"]]["premium"]
        if len(bp) and len(nbp):
            naive.append(bp.mean() - nbp.mean())
    if not deltas:
        return empty
    s = pd.Series(deltas)
    mu = float(s.mean())
    se = float(s.std(ddof=1) / math.sqrt(len(s))) if len(s) > 1 else float("nan")
    return {"mu": mu, "se": se, "n_unit_days": int(len(s)), "n_obs": int(len(df)),
            "mu_naive_premium": (float(pd.Series(naive).mean()) if naive else None)}


# --------------------------------------------------------------------------- #
# Observed redispatch cost (empirical anchor, computed directly from data)
# --------------------------------------------------------------------------- #
def observed_redispatch_cost(date: str) -> float:
    """Sum over SO-flagged BOAs of accepted volume x applicable BOD price.
    Turn-down (curtailment) priced at bid; turn-up at offer. Half-hour -> MWh."""
    reg = classify_units(bm.fetch_registry())
    rd = _so_redispatch(bm.fetch_boalf(date), bm.fetch_pn(date))
    px = _bod_prices(bm.fetch_bod(date)).set_index(["bmUnit", "settlementPeriod"])
    cost = 0.0
    for _, r in rd.iterrows():
        key = (r["bmUnit"], r["settlementPeriod"])
        mwh = abs(r["delta_mw"]) * 0.5  # half-hour settlement period
        if r["delta_mw"] < 0:  # turn-down: NESO pays the bid
            p = px["bid"].get(key, 0.0)
            cost += -p * mwh  # bid usually <0 for wind => cost>0
        else:  # turn-up: NESO pays the offer
            p = px["offer"].get(key, 0.0)
            cost += p * mwh
    return float(cost)


# --------------------------------------------------------------------------- #
# Detailed bottom-up redispatch cost (period-held integration, decomposed)
# --------------------------------------------------------------------------- #
RECON_TOL = 0.15           # internal check (proxy vs detailed): |gap| > 15% fails
# External check vs NESO is scope-affected: our BM bid-offer cost is the dominant
# component of NESO's *system* constraint cost, which additionally includes
# constraint actions taken OUTSIDE the BM (constraint trades, interconnector
# trades, ODFM and other balancing services) that are not recorded in BOALF. We
# therefore validate that the BM-BOA cost is a large, plausible share of -- but
# does not exceed -- NESO's published total.
EXTERNAL_MIN_SHARE = 0.65  # BM-BOA cost should be >= this fraction of NESO total
EXTERNAL_MAX_RATIO = 1.10  # ... and not exceed it by more than 10%


def _hours(time_from, time_to) -> float:
    a = pd.to_datetime(time_from)
    b = pd.to_datetime(time_to)
    return max((b - a).total_seconds() / 3600.0, 0.0)


def redispatch_cost_detailed(date: str) -> Dict[str, float]:
    """Bottom-up SO-flagged constraint cost, using a *period-held* integration:
    each unit's SO-instructed level is held for the whole settlement period
    (the half-hour the instruction applies to), not just the acceptance's
    ramp window -- the latter (duration-weighting) materially under-counts the
    delivered energy. One delta per (bmUnit, settlementPeriod), priced at the
    applicable BOD band and weighted by 0.5 h. Decomposed into Scottish-wind
    turn-down (curtailment) vs replacement turn-up. This is the figure defended
    against the NESO published total."""
    reg = classify_units(bm.fetch_registry())
    pn = _pn_per_period(bm.fetch_pn(date)).set_index(["bmUnit", "settlementPeriod"])
    px = _bod_prices(bm.fetch_bod(date)).set_index(["bmUnit", "settlementPeriod"])
    per = _periodize_boalf(bm.fetch_boalf(date))
    so = per[per["soFlag"]] if not per.empty else per
    if so.empty:
        return {"total": 0.0, "turndown_curtailment": 0.0,
                "turnup_replacement": 0.0, "scottish_wind_turndown": 0.0}
    # period-held: one instructed level per (bmUnit, period) = mean of the
    # acceptance levels active in the period, held for the full half hour
    held = so.groupby(["bmUnit", "settlementPeriod"], as_index=False)["level"].mean()

    turndown = turnup = 0.0
    sco_wind_turndown = 0.0
    for r in held.itertuples(index=False):
        bmu, t = r.bmUnit, r.settlementPeriod
        fpn = float(pn["mw"].get((bmu, t), 0.0))
        delta = r.level - fpn
        mwh = delta * 0.5              # instruction held over the half-hour period
        if delta < 0:  # turn-down (curtailment)
            p = float(px["bid"].get((bmu, t), 0.0))
            c = -p * abs(mwh)  # bid<0 for CfD wind => cost>0
            turndown += c
            zinfo = reg.loc[bmu] if bmu in reg.index else None
            if (
                zinfo is not None
                and zinfo.get("zone") == "SCO"
                and zinfo.get("fuel") == "WIND"
            ):
                sco_wind_turndown += c
        else:  # turn-up (replacement)
            p = float(px["offer"].get((bmu, t), 0.0))
            turnup += p * mwh
    total = turndown + turnup
    return {
        "total": float(total),
        "turndown_curtailment": float(turndown),
        "turnup_replacement": float(turnup),
        "scottish_wind_turndown": float(sco_wind_turndown),
    }


# --------------------------------------------------------------------------- #
# Validation against NESO's published constraint cost for the day
# --------------------------------------------------------------------------- #
# NESO Data Portal "Daily Balancing Costs (Balancing Services Use of System)".
# Per-settlement-period costs by category; the "Constraints" column is the
# transmission-constraint (redispatch) cost we validate against. One CKAN
# datastore resource per financial year (Apr->Mar); extend the map as needed.
# Resource UUIDs are stable; verify on the portal if a query starts returning 0
# rows. Package: d13d78fc-60d9-4f4d-87b6-e25a20f669c0.
NESO_CKAN_SQL = "https://api.neso.energy/api/3/action/datastore_search_sql"
NESO_DBC_RESOURCES = {
    "2024-2025": "527a5f40-942b-416b-99df-81a51c30d041",  # FY 2024/25 (verified)
    # "2023-2024": "<resource_id>",   # add older/newer years here
    "2025-2026": "46183ba7-48df-4318-9b4b-06828348d46e",
}


def _financial_year(date: str) -> str:
    """GB financial year (Apr 1 -> Mar 31) label, e.g. '2024-2025', for `date`."""
    d = _dt.date.fromisoformat(date)
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{start + 1}"


def neso_daily_constraint_cost(date: str) -> float | None:
    """Independent benchmark: NESO's published *system-wide* daily transmission
    constraint cost (GBP), summed over the 48 settlement periods of `date`.

    Auto-fetched from the NESO Data Portal "Daily Balancing Costs" CKAN datastore
    via its key-free SQL endpoint, summing the "Constraints" column for the day.
    Returns None if the financial year is not mapped or the request/parse fails,
    in which case `reconcile` proceeds without the external check.

    NOTE on scope: this is the whole-system constraint cost (all boundaries, plus
    voltage/thermal actions), so it is the anchor for the *system-wide* bottom-up
    RC (`redispatch_cost_detailed['total']`), not for the B6-only modelled figure.
    The B6 share is reported separately via the Scottish-wind-curtailment
    component of `redispatch_cost_detailed`."""
    fy = _financial_year(date)
    rid = NESO_DBC_RESOURCES.get(fy)
    if rid is None:
        return None
    sql = f'SELECT SUM("Constraints") AS c FROM "{rid}" WHERE "SETT_DATE" = \'{date}\''
    try:
        r = requests.get(NESO_CKAN_SQL, params={"sql": sql}, timeout=60)
        r.raise_for_status()
        recs = r.json()["result"]["records"]
        if not recs or recs[0].get("c") in (None, ""):
            return None
        return float(recs[0]["c"])
    except Exception:
        return None


def reconcile(
    date: str, neso_value: float | None = None, tol: float = RECON_TOL
) -> Dict:
    """Two-tier validation of the bottom-up redispatch cost.

    INTERNAL (proxy vs detailed): both are period-held BM bid-offer costs; they
    should agree to within `tol`, confirming the two code paths are consistent.

    EXTERNAL (BM-BOA vs NESO): the detailed cost is the cost of SO-flagged BM
    bid-offer acceptances, the dominant *component* of NESO's published system
    constraint cost; NESO's total additionally includes non-BM constraint actions
    (constraint trades, interconnector actions, ODFM and other services) absent
    from BOALF. We therefore check that the BM-BOA cost is a large, plausible
    share of -- without exceeding -- NESO's total (EXTERNAL_MIN_SHARE..MAX_RATIO),
    rather than demanding equality, and report the share explicitly."""
    proxy = observed_redispatch_cost(date)   # period-held, first BOD band
    detail = redispatch_cost_detailed(date)  # period-held, decomposed
    neso = neso_value if neso_value is not None else neso_daily_constraint_cost(date)

    def rel(a, b):
        return None if (b in (None, 0)) else (a - b) / b

    internal = rel(proxy, detail["total"])
    external = rel(detail["total"], neso) if neso is not None else None
    bm_share_of_neso = (detail["total"] / neso) if neso else None  # ~0.8 expected
    b6_curtailment = detail["scottish_wind_turndown"]
    b6_share = (b6_curtailment / detail["total"]) if detail["total"] else None
    external_pass = (bm_share_of_neso is not None
                     and EXTERNAL_MIN_SHARE <= bm_share_of_neso <= EXTERNAL_MAX_RATIO)
    out = {
        "date": date,
        "proxy_RC": proxy,
        "detailed_RC": detail["total"],          # BM SO-flagged bid-offer cost
        "detailed_breakdown": detail,
        "neso_published": neso,                  # system-wide (Constraints)
        "bm_share_of_neso": bm_share_of_neso,    # BM-BOA component / NESO total
        "b6_curtailment_component": b6_curtailment,
        "b6_share_of_system": b6_share,
        "internal_discrepancy": internal,        # proxy vs detailed
        "external_discrepancy": external,        # detailed vs NESO (scope-affected)
        "internal_pass": (internal is not None and abs(internal) <= tol),
        "external_pass": external_pass,
    }
    if not out["internal_pass"] and internal is not None:
        out["warning_internal"] = (
            f"proxy vs detailed differ by {internal:+.1%} (> {tol:.0%}): "
            f"investigate the period-held integration / BOD band allocation.")
    if bm_share_of_neso is not None and not external_pass:
        out["warning_external"] = (
            f"BM-BOA cost is {bm_share_of_neso:.0%} of NESO total "
            f"(outside {EXTERNAL_MIN_SHARE:.0%}-{EXTERNAL_MAX_RATIO:.0%}): "
            f"check BMU->zone mapping, SO-flag scope and volume integration.")
    return out


if __name__ == "__main__":
    # candidate high-wind winter 2024/25 days; the peak is chosen from data.
    candidates = ["2024-12-04", "2024-12-08", "2025-01-01", "2025-01-24"]
    day, ranking = find_peak_curtailment_day(candidates)
    print("Selected peak-curtailment day:", day)
    print(ranking)
    # pass NESO's published daily constraint cost here once obtained from the portal
    rec = reconcile(day, neso_value=None)
    for k, v in rec.items():
        print(f"  {k}: {v}")
