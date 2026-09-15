"""Classification robustness for the Scotland/B6 wind set (paper appendix).
(i) threshold-invariance: distinct T_ WIND TLF values around the cut;
(ii) full-year (365-day) realised B6 redispatch expenditure + curtailment under
     alternative Scotland definitions, computed directly -- each day is parsed
     once and every classification applied to it (reproduces Table tab:classif);
(iii) the named units that carry the curtailment (independent cross-check)."""
import numpy as np, pandas as pd
import gb_empirical_pipeline as ep, bmrs_client as bm
from gb_empirical_pipeline import (_periodize_boalf, _pn_per_period, _bod_prices,
                                   _mid_reference, SENTINEL_PRICE, ENG_GAS_COST)

FY = "2024-2025"
CAL = f"neso_daily_{FY}.csv"

# ---- (i) distinct TLF values for transmission WIND units --------------------
reg_raw = bm.fetch_registry()
col = {c.lower(): c for c in reg_raw.columns}; bmc = col['elexonbmunit']
reg_raw['tlf'] = pd.to_numeric(reg_raw['transmissionLossFactor'], errors='coerce')
tw = reg_raw[(reg_raw['fuelType'] == 'WIND') & (reg_raw[bmc].astype(str).str.startswith('T_'))]
vals = np.sort(tw['tlf'].dropna().unique())
print("Distinct TLF values, transmission WIND units:")
for v in vals:
    print(f"   {v:+.6f}  (n={(tw['tlf']==v).sum()})")
CUT = -0.0055
sco_max = vals[vals <= CUT].max()   # least-negative Scottish-zone value
eng_min = vals[vals > CUT].min()    # most-negative English/Welsh-zone value
print(f"-> TLF gap straddling the {CUT} cut: [{sco_max:+.6f} (Scotland), "
      f"{eng_min:+.6f} (England)]; no transmission WIND unit lies inside, so any "
      f"cut in this gap gives an identical Scottish set.")

# ---- full-year machinery: parse each day once, apply every classification ----
dates = list(pd.read_csv(CAL)["date"].astype(str))
annual_neso = float(pd.read_csv(CAL)["neso"].sum())
reg_base = ep.classify_units(reg_raw)
fuel = reg_base["fuel"].to_dict()

defs = [("GSP group only (pre-fix)", -1.0),
        ("TLF<=-0.0045 (incl. Cumbria)", -0.0045),
        ("TLF<=-0.0055 (baseline)", -0.0055),
        ("TLF<=-0.0065 (in-gap)", -0.0065),
        ("TLF<=-0.0200 (N. Scotland only)", -0.0200)]
sco_sets = {}
for lbl, thr in defs:
    ep.SCOTLAND_TLF_MAX = thr
    rv = ep.classify_units(reg_raw)
    sco_sets[lbl] = set(rv.index[rv["zone"] == "SCO"])
ep.SCOTLAND_TLF_MAX = -0.0055
sco_base = sco_sets["TLF<=-0.0055 (baseline)"]

agg = {lbl: {"rent": 0.0, "vol": 0.0} for lbl, _ in defs}
vol_by_unit = {}                                     # baseline named cross-check
for d in dates:
    per = _periodize_boalf(bm.fetch_boalf(d))
    so = per[per["soFlag"]] if len(per) else per
    pn = _pn_per_period(bm.fetch_pn(d)).set_index(["bmUnit", "settlementPeriod"])
    px = _bod_prices(bm.fetch_bod(d)).set_index(["bmUnit", "settlementPeriod"])
    mid = _mid_reference(d)
    for a in (so.itertuples(index=False) if len(so) else []):
        if fuel.get(a.bmUnit) != "WIND":
            continue
        f = float(pn["mw"].get((a.bmUnit, a.settlementPeriod), 0.0))
        delta = a.level - f
        if delta >= 0:
            continue
        v = -delta * a.hours
        bid = float(px["bid"].get((a.bmUnit, a.settlementPeriod), -65.0))
        if not (-SENTINEL_PRICE < bid < 0):
            bid = -65.0
        p_rep = max(mid.get(a.settlementPeriod, ENG_GAS_COST), ENG_GAS_COST)
        r = (p_rep - bid) * v
        for lbl, s in sco_sets.items():
            if a.bmUnit in s:
                agg[lbl]["rent"] += r; agg[lbl]["vol"] += v
        if a.bmUnit in sco_base:
            vol_by_unit[a.bmUnit] = vol_by_unit.get(a.bmUnit, 0.0) + v

# ---- (ii) sweep Scotland definition (full year) -----------------------------
print("\nFull-year (365-day) realised B6 redispatch expenditure / curtailment "
      "under alternative Scotland definitions:")
for lbl, _ in defs:
    rent, vol = agg[lbl]["rent"], agg[lbl]["vol"]
    print(f"  {lbl:34s}: exp £{rent/1e6:7.1f}m  vol {vol/1e6:5.2f} TWh  "
          f"share {100*rent/annual_neso:4.0f}%")

# ---- (iii) named units carrying curtailment (baseline, full year) -----------
lead = reg_raw.drop_duplicates(bmc).set_index(bmc)['leadPartyName']
s = pd.Series(vol_by_unit).sort_values(ascending=False)
print(f"\nTop 15 curtailed units (full year), n_units={len(s)}, "
      f"total {s.sum()/1e3:.0f} GWh:")
for u, v in s.head(15).items():
    print(f"  {u:14s} {v/1e3:6.1f} GWh  {str(lead.get(u,''))[:40]}")
print(f"  share of curtailment from top 15: {100*s.head(15).sum()/s.sum():.0f}%")
