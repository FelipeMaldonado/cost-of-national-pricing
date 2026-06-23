"""Structural two-zone annual rent under OBSERVED SCOTEX limits (use_scotex=True),
same 30-day stratified estimator as run_annual. Recovers the 'observed-limit'
structural figure quoted alongside the fixed-ETYS-limit one."""
import pandas as pd
import gb_empirical_pipeline as ep, run_annual as ra
days = ra.neso_daily_series(); annual_neso=float(days['neso'].sum())
days['bin']=pd.qcut(days['neso'].rank(method='first'),5,labels=False)
binsize=days.groupby('bin').size().to_dict()
sample=pd.read_csv('annual_sample_2024-2025.csv')[['date','bin','neso']]
estim = lambda df, coln: ep.stratified_estimator(df, coln, binsize)  # shared estimator
rows=[]
for _,r in sample.iterrows():
    d=ep.solve_day(r['date'], markup=0.30, build_kwargs={"use_scotex": True})
    if d['n_solved']<40: 
        print(f"  {r['date']}: only {d['n_solved']} periods"); continue
    rows.append({'bin':int(r['bin']),'g_cb':d['Gamma_costbased'],'r_cong':d['R_cong']})
    print(f"  {r['date']}: Gamma_cb £{d['Gamma_costbased']:,.0f}")
df=pd.DataFrame(rows)
g,gse=estim(df,'g_cb')
print(f"\n=== Structural annual under OBSERVED SCOTEX limits ===")
print(f"  Gamma_costbased (structural rent): £{g/1e6:.0f}m (+/- {gse/1e6:.0f}m SE)")
print(f"  share of NESO system total: {100*g/annual_neso:.0f}%")
