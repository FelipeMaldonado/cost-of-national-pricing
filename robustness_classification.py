"""Classification robustness for the Scotland/B6 wind set (paper appendix).
(i) threshold-invariance: list distinct T_ WIND TLF values around the cut;
(ii) annual realized B6 rent + curtailment under alternative Scotland definitions,
     via the same 30-day stratified estimator;
(iii) the named units that carry the curtailment (independent cross-check)."""
import math, numpy as np, pandas as pd
import gb_empirical_pipeline as ep, bmrs_client as bm, run_annual as ra

# ---- (i) distinct TLF values for transmission WIND units --------------------
reg_raw = bm.fetch_registry()
col={c.lower():c for c in reg_raw.columns}; bmc=col['elexonbmunit']
reg_raw['tlf']=pd.to_numeric(reg_raw['transmissionLossFactor'],errors='coerce')
tw = reg_raw[(reg_raw['fuelType']=='WIND') & (reg_raw[bmc].astype(str).str.startswith('T_'))]
vals = np.sort(tw['tlf'].dropna().unique())
print("Distinct TLF values, transmission WIND units:")
for v in vals: print(f"   {v:+.6f}  (n={(tw['tlf']==v).sum()})")
CUT=-0.0055
sco_max=vals[vals<=CUT].max()   # least-negative Scottish-zone value
eng_min=vals[vals>CUT].min()    # most-negative English/Welsh-zone value
print(f"-> TLF gap straddling the {CUT} cut: [{sco_max:+.6f} (Scotland), "
      f"{eng_min:+.6f} (England)]; no transmission WIND unit lies inside, so any "
      f"cut in this gap gives an identical Scottish set.")

# ---- stratified machinery (shared estimator, identical to run_annual) --------
days = ra.neso_daily_series(); annual_neso=float(days['neso'].sum())
days['bin']=pd.qcut(days['neso'].rank(method='first'),5,labels=False)
binsize=days.groupby('bin').size().to_dict()
sample=pd.read_csv('annual_sample_2024-2025.csv')[['date','bin','neso']]
estim = lambda df, coln: ep.stratified_estimator(df, coln, binsize)

# ---- (ii) sweep Scotland definition -----------------------------------------
defs=[("GSP group only (pre-fix)",-1.0),
      ("TLF<=-0.0045 (incl. Cumbria)",-0.0045),
      ("TLF<=-0.0055 (baseline)",-0.0055),
      ("TLF<=-0.0065 (in-gap)",-0.0065),
      ("TLF<=-0.0200 (N. Scotland only)",-0.0200)]
print("\nAnnual realized B6 rent / curtailment under alternative Scotland definitions:")
base=None
for lbl,thr in defs:
    ep.SCOTLAND_TLF_MAX=thr
    rows=[]
    for _,r in sample.iterrows():
        o=ep.realized_b6_congestion_rent(r['date'])
        rows.append({'bin':int(r['bin']),'rent':o['R_cong_obs'],'vol':o['curtailed_MWh']})
    df=pd.DataFrame(rows)
    rent,_=estim(df,'rent'); vol,_=estim(df,'vol')
    print(f"  {lbl:34s}: rent £{rent/1e6:7.1f}m  vol {vol/1e6:5.2f} TWh  share {100*rent/annual_neso:4.0f}%")

# ---- (iii) named units carrying curtailment (baseline) ----------------------
ep.SCOTLAND_TLF_MAX=-0.0055
reg=ep.classify_units(bm.fetch_registry())
lead=reg_raw.drop_duplicates(bmc).set_index(bmc)['leadPartyName']
vol_by_unit={}
for _,r in sample.iterrows():
    per=ep._periodize_boalf(bm.fetch_boalf(r['date'])); so=per[per['soFlag']] if len(per) else per
    pn=ep._pn_per_period(bm.fetch_pn(r['date'])).set_index(['bmUnit','settlementPeriod'])['mw']
    for a in (so.itertuples(index=False) if len(so) else []):
        if a.bmUnit not in reg.index: continue
        z=reg.loc[a.bmUnit]
        if not (z.get('zone')=='SCO' and z.get('fuel')=='WIND'): continue
        d=a.level-float(pn.get((a.bmUnit,a.settlementPeriod),0.0))
        if d<0: vol_by_unit[a.bmUnit]=vol_by_unit.get(a.bmUnit,0.0)+(-d*a.hours)
s=pd.Series(vol_by_unit).sort_values(ascending=False)
print(f"\nTop 15 curtailed units (sampled days), n_units={len(s)}, total {s.sum()/1e3:.0f} GWh:")
for u,v in s.head(15).items():
    print(f"  {u:14s} {v/1e3:6.1f} GWh  {str(lead.get(u,''))[:40]}")
print(f"  share of curtailment from top 15: {100*s.head(15).sum()/s.sum():.0f}%")
