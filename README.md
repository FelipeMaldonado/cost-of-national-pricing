# The Cost of National Pricing — GB electricity market model & empirics

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.20812217.svg)](https://doi.org/10.5281/zenodo.20812217)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Replication code and data for *“What Consumers Pay for National Pricing: Socialised
Redispatch Expenditure on the GB Scotland–England Boundary.”*

It formalises GB arrangements as a **two-stage market** — a network-blind national
clearing (Stage 1) followed by a cost-minimising redispatch (Stage 2) — and measures,
directly from public Balancing Mechanism settlement records, the **realised redispatch
expenditure** on the Scotland–England (B6) boundary: **£1.18 bn in FY 2024/25 (about
62 % of GB’s system constraint cost)**, computed for all 365 settlement days and cross-
checked against NESO’s published constraint cost as a consistency check.

The three theoretical results the code instantiates:

- **Proposition 1** — in a convex economy, national-pricing-with-redispatch is
  welfare-equivalent to nodal pricing, so any welfare loss is a pure
  commitment-nonconvexity artefact and the redispatch expenditure is a *transfer*.
- **Proposition 2** — the paid redispatch cost is `C(o,b) = R_res + M`: a resource
  cost `R_res` (the boundary shadow price integrated over the relieved overload —
  the production-cost increase, incurred under national and nodal pricing alike) plus
  a strategic markup `M`. The measured **expenditure** `E` is a separate object (gross
  settlement payments), and the **merchandising surplus** a nodal market would collect
  is a third. *(Note: the code predates the paper’s final naming and still uses
  `R_cong`/`R_cong_obs` for what the paper calls the resource cost `R_res` and the
  realised expenditure; the CSV columns follow the code.)*
- **Proposition 3** — for a budget of flexibility across the nested GB boundaries, its
  marginal value rises up the cascade, so the budget should water-fill the most
  upstream binding location first (optimal siting; **Appendix B** in the paper).

---

## Repository layout

| File | What it is |
|---|---|
| `gb_two_stage_skeleton.py` | The optimisation models: nodal DCOPF benchmark, Stage-1 network-blind clearing, Stage-2 redispatch (with the `RC = R_cong + M` decomposition from the boundary duals), and the nodal PE-A make-whole minimisation. Includes the worked **Example 1** instance (`synthetic_b6`). Run directly for a self-contained demo. |
| `bmrs_client.py` | Thin, key-free client for the Elexon BMRS *Insights* API (PN, BOD, BOALF, MID, FUELHH) plus the national demand outturn. Parquet-cached to `./bmrs_cache/`. |
| `gb_empirical_pipeline.py` | Wires the data into the model: builds the faithful **B6 two-zone instance** (`build_zonal_instance`), estimates the strategic markup (`estimate_markup`), computes the bottom-up redispatch cost, and reconciles it against NESO’s published constraint cost (`reconcile`, auto-fetched from the NESO Data Portal). |
| `run_paper.py` | Orchestrator: selects the peak-curtailment day, solves all 48 periods, aggregates the Proposition-2 decomposition, runs the reconciliation, and writes `results_b6_<date>.csv` (the peak-day table, §6.2). |
| `run_fullyear.py` | **The headline.** Computes the realised B6 redispatch expenditure directly for **all 365 days** of FY 2024/25 (curtailment payment + replacement energy from settlement), writing `fullyear_2024-2025.csv`. Resumable (skips days already in the CSV). §6.3. |
| `run_multiyear.py` | The multi-year **trend**: the same full-year measure for FY 2022/23 and FY 2023/24, writing `fullyear_<FY>.csv`. §6.3 figure. |
| `sensitivity.py` | Comparative-statics sweeps (B6 capability, wind penetration, behind-boundary flexibility, strategic markup) producing the sensitivity grids (§6.4 / Appendix C). |
| `run_annual.py` | **Legacy** 30-day stratified estimator (the pre-full-year headline), kept for reference and **superseded by `run_fullyear.py`**. Also reports the within-unit-day strategic-markup estimate (`estimate_markup_pooled`). |
| `robustness_classification.py` | Robustness of the Scotland/B6 unit classification (**Appendix D**), computed over the **full year**: threshold-invariance of the loss-factor cut, the figure under alternative definitions, and a named cross-check of the curtailed units. |
| `structural_observed_limit.py` | Structural two-zone annual rent under NESO's **observed** SCOTEX limits (the £754m figure quoted in §6 alongside the fixed-ETYS-limit one). |
| `nonconvex_experiment.py` | Controlled demonstration of Proposition 1 on the purpose-built convex/nonconvex instances with known costs (welfare loss exhibited, decomposed, and recovered by recommitment). Offline; needs only a solver. |
| `flex_siting.py` | Optimal **siting and operation** of flexibility across the nested B4/B6 cascade (multi-period storage LP). Returns the value of a flexibility budget, the optimal N/S split, and the marginal value = downstream boundary shadow prices (Proposition 3; **Appendix B**). Offline; needs only a solver. |
| `make_gbmap.py` | Generates the GB network-map figure (`fig_gbmap.pdf`): the full GB network (ETYS substations) and the reduced 29-bus network coloured by the N/S/E zones, from PyPSA-GB open data (auto-fetched). Needs `pandas` + `matplotlib`. |
| `reproduce_all.py` | **One-command reproduction**: runs every script above in order, tees to `reproduce_all.log`, and prints a manifest mapping each paper table/figure to its script. |

---

## Quick start

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

`highspy` provides the open-source HiGHS LP/MILP solver (no licence needed). All
data are public and require **no API key** (Elexon BMRS Insights + NESO Data
Portal); you only need outbound network access.

### 1. Offline demo (no network) — reproduces the worked example
```bash
python gb_two_stage_skeleton.py
```
Expected output (the paper’s Example 1):
```
Welfare loss (P1)          : 0.0
RC  (redispatch cost)      : 11600.0
  R_cong (congestion rent) : 11600.0
Gamma = RC - MWP           : 11600.0
```
Run this first: if it works, your solver is set up correctly.

> **Notation:** in the paper’s final terminology this £11,600 is the **resource cost
> `R_res`** (the boundary shadow price × relieved overload, £58 × 200 MW), incurred
> under national *and* nodal pricing. The code’s legacy label `R_cong (congestion
> rent)` refers to this same resource cost. The nodal **merchandising surplus** — the
> congestion rent a nodal market would collect on the *flow* (£58 × 500 MW = £29,000)
> — is a separate quantity, and the measured **expenditure** `E` (gross BM payments) a
> third; see Proposition 2 and the notation note above.

### 2. Connectivity check (network)
```bash
python bmrs_client.py            # prints BMRS dataset row counts
```

### 3. Full empirical run (network + solver)
```bash
python run_paper.py
```
Selects the peak-curtailment day from `CANDIDATE_DAYS`, estimates the markup,
solves every settlement period, writes `results_b6_<date>.csv`, and prints the
daily totals and the reconciliation. The first run downloads and caches a
settlement day per dataset under `./bmrs_cache/`; reruns are fast.

### 4. Sensitivity sweeps (network + solver) — Section 6.1 / Appendix B
```bash
python sensitivity.py
```
Sweeps the four policy levers (B6 capability, wind penetration, behind-boundary
flexibility, strategic markup) and writes `sensitivity_<date>.csv`.

### 5. Full-year expenditure (the headline) — §6.3
```bash
python run_fullyear.py               # FY 2024/25, all 365 days -> fullyear_2024-2025.csv
python run_multiyear.py              # FY 2022/23 + 2023/24 (the trend) -> fullyear_<FY>.csv
```
`run_fullyear.py` computes the realised B6 redispatch expenditure **directly for
every settlement day** — no sampling — and reports the split into curtailment payment
and replacement energy: **£1,184m in FY 2024/25 (£401m / 34% curtailment + £783m /
66% replacement), on 9.92 TWh of BM-visible Scottish-wind curtailment, ≈ 62% of the
system-wide constraint cost.** `run_multiyear.py` adds FY 2022/23 (£570m) and FY
2023/24 (£714m) for the trend. Both are **resumable**: with the committed
`fullyear_*.csv` present they return at once; delete a CSV to recompute that year
(slow — the realised measure is ~24 s/day against the cache).

> The pre-full-year **30-day stratified** estimator (≈ £1.1bn, the previous headline)
> lives in `run_annual.py` and is kept for reference only; the full-year census above
> supersedes it.

---

## Data sources (all public, key-free)

| Object | Source | Used for |
|---|---|---|
| **FPN** (Final Physical Notification) | BMRS `PN` | Stage-1 self-schedule |
| **BOD** (Bid-Offer Data) | BMRS `BOD` | Stage-2 offer/bid price ladders |
| **BOALF** (Bid-Offer Acceptance Level Flagged) | BMRS `BOALF` | Stage-2 redispatch; the **SO-flag** isolates constraint actions |
| **MID** (Market Index Data) | BMRS `MID` | wholesale reference for the markup estimate |
| **FUELHH** (generation by fuel) | BMRS `FUELHH` | faithful zonal generation (incl. *embedded* wind) |
| **National demand outturn** | BMRS `/demand/outturn` | faithful zonal demand |
| **ETYS boundary capability** | NESO ETYS | B6 transfer limit (structural baseline) |
| **B6 (SCOTEX) flow & limit** | NESO “Day-Ahead Constraint Flows and Limits” | observed, outage-adjusted B6 capability (the *limit*; the day-ahead *flow* is an unconstrained forecast and is not used for the realised overload) |
| **Daily/annual constraint cost** | NESO Data Portal “Daily Balancing Costs” | consistency check on the settlement-derived expenditure (the *Constraints* column, summed per financial year) |

---

## How the empirical instance is built

Raw BM physical notifications miss most *embedded* (distribution-connected)
Scottish wind, so a 2-zone instance built from PNs alone never makes B6 bind.
`build_zonal_instance` instead anchors zonal **totals** to system data — GB
generation by fuel (`FUELHH`) and the national demand outturn — and reduces them
to SCO/ENG across B6 with explicit, documented **calibration shares**
(`SCOTLAND_WIND_SHARE`, `SCOTLAND_DEMAND_SHARE`, `SCOTLAND_NONWIND_MW`), while
keeping BOD/BOALF for the per-unit curtailment detail. The instance is balanced
by construction, so the redispatch is energy-conserving.

### Validation (two tiers)
- **Internal:** a flat-band proxy vs the period-held detailed cost (should agree).
- **External (scope-aware):** the bottom-up **Balancing Mechanism** bid-offer cost
  is the dominant *component* of NESO’s **system** constraint cost, which also
  includes non-BM constraint actions (constraint trades, interconnector actions,
  other services) absent from BOALF. The check therefore confirms the BM cost is
  a large, plausible share (≈ 79% on the test day) of — without exceeding — NESO’s
  published total, rather than demanding equality.

---

## Configuration knobs (`run_paper.py` / `gb_empirical_pipeline.py`)

| Setting | Meaning |
|---|---|
| `CANDIDATE_DAYS` | days ranked for peak curtailment |
| `INSTANCE` | `"zonal"` (faithful, B6 binds) or `"pn"` (raw-PN reference) |
| `MARKUP` | `None` ⇒ estimate from data; falls back to `MARKUP_FALLBACK=0.30` when not identified |
| `B6_LIMIT_MW` | ETYS B6 capability (set day-specific if known) |
| `SCOTLAND_*` shares | zonal-reduction calibration (validate against actual B6 flows) |
| `NESO_DBC_RESOURCES` | NESO CKAN resource id per financial year (FY2024/25 wired) |

---

## Known limitations / calibration to-dos

- The **annual headline (£1.18bn/yr in FY 2024/25)** is the *realised, BM-visible* B6
  redispatch expenditure (≈62% of system constraint cost), computed over all 365 days.
  It captures transmission-connected Scottish wind; curtailment of *embedded*
  (distribution-connected) wind settles outside the Balancing Mechanism and is
  excluded, so it is, if anything, a mild lower bound. (Scotland is identified by GSP
  group OR zonal transmission loss factor — the TLF leg is essential because
  transmission-connected units carry no GSP group; see `robustness_classification.py`
  and the paper's classification appendix.)
- The **structural two-zone model** reproduces the mechanism and drives the
  sensitivity (relative effects), but its absolute *level* is sensitive to the
  capability assumption (£165m at the ETYS limit vs £754m at observed SCOTEX
  limits — both below the directly measured £1.18bn), which is why the level is
  measured directly rather than modelled (`structural_observed_limit.py`).
- The **within-unit-day markup comparison** needs a non-binding control period for the
  same unit; on all-binding high-wind days it is not identified (the code detects this
  and falls back). It bounds only the price of accepted turn-up offers, not turn-down
  bids, dispatched volume, or scheduling/withholding conduct.
- A full **multi-zone** build (NESO Reduced Model + FES) — which would capture
  embedded curtailment and a complete B6 figure — is left as documented loader
  contracts in `gb_two_stage_skeleton.py` for future work.

---

## Reproducing the results
Empirical tables and figures — one command:
```bash
pip install -r requirements.txt      # pyomo, highspy, requests, pandas, pyarrow
python reproduce_all.py              # runs every script; output in reproduce_all.log
python reproduce_all.py --list       # list the steps and the artifact each produces
```
This repository contains the **code and canonical outputs** only; the manuscript
itself is not included here.

### Data & reproducibility (GitHub vs Zenodo)
- **GitHub (this repo)** ships the code, the small canonical output CSVs
  (`fullyear_*.csv`, `annual_sample_*`, `neso_daily_*`, `sensitivity_*`, `pg_*`), and a
  **slim ~45 MB settlement cache** under `bmrs_cache/` — enough to run the offline demo
  and the day-level scripts from a fresh clone.
- **The full ~1.2 GB multi-year `bmrs_cache/`** needed to reproduce the full-year and
  multi-year figures **offline** is archived on **Zenodo** (concept DOI
  [10.5281/zenodo.20812217](https://doi.org/10.5281/zenodo.20812217), which always
  resolves to the latest version). Download and unzip it over `bmrs_cache/`, then
  `python reproduce_all.py`.
- **No download needed to reproduce live:** every dataset is public and key-free
  (Elexon BMRS Insights + NESO Data Portal), so the scripts re-fetch and re-cache any
  missing day automatically (results are identical for settled days). The full-year and
  multi-year runs are resumable and append to `fullyear_*.csv` as they go.

## Citation
If you use this code, please cite the paper and the software (see `CITATION.cff`).

## License
Code released under the [MIT License](LICENSE). Data retrieved at run time from
the Elexon BMRS Insights API and the NESO Data Portal are © their respective
providers and subject to their own terms of use.
