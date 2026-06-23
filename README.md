# The Cost of National Pricing — GB electricity market model & empirics

Replication code for *“The Cost of National Pricing: Side Payments and Efficiency
under Self-Dispatch with Redispatch in the GB Electricity Market.”*

It formalises GB arrangements as a **two-stage market** — a network-blind national
clearing (Stage 1) followed by a cost-minimising redispatch (Stage 2) — and
measures the *cost of national pricing* on the Scotland–England (B6) boundary
using public Balancing Mechanism data, validated against NESO’s published
constraint cost.

The three theoretical results the code instantiates:

- **Proposition 1** — in a convex economy, national-pricing-with-redispatch is
  welfare-equivalent to nodal pricing, so any welfare loss is a pure
  commitment-nonconvexity artefact.
- **Proposition 2** — the visible cost decomposes as
  `RC = R_cong + M`: a congestion-rent term `R_cong` (the boundary shadow price ×
  relieved overload, which nodal pricing would collect as merchandising surplus)
  plus a strategic redispatch markup `M`.
- **Proposition 3** — for a budget of flexibility placed across the nested GB
  boundaries, its marginal value rises up the cascade, so the budget should
  water-fill the most upstream binding location first (optimal siting).

---

## Repository layout

| File | What it is |
|---|---|
| `gb_two_stage_skeleton.py` | The optimisation models: nodal DCOPF benchmark, Stage-1 network-blind clearing, Stage-2 redispatch (with the `RC = R_cong + M` decomposition from the boundary duals), and the nodal PE-A make-whole minimisation. Includes the worked **Example 1** instance (`synthetic_b6`). Run directly for a self-contained demo. |
| `bmrs_client.py` | Thin, key-free client for the Elexon BMRS *Insights* API (PN, BOD, BOALF, MID, FUELHH) plus the national demand outturn. Parquet-cached to `./bmrs_cache/`. |
| `gb_empirical_pipeline.py` | Wires the data into the model: builds the faithful **B6 two-zone instance** (`build_zonal_instance`), estimates the strategic markup (`estimate_markup`), computes the bottom-up redispatch cost, and reconciles it against NESO’s published constraint cost (`reconcile`, auto-fetched from the NESO Data Portal). |
| `run_paper.py` | Orchestrator: selects the peak-curtailment day, solves all 48 periods, aggregates the Proposition-2 decomposition, runs the reconciliation, and writes `results_b6_<date>.csv` (the empirical results table, §6). |
| `sensitivity.py` | Comparative-statics sweeps (B6 capability, wind penetration, behind-boundary flexibility, strategic markup) producing the sensitivity tables (§6.1 / Appendix B). |
| `run_annual.py` | Annualises the cost via stratified sampling on NESO's 365-day daily constraint-cost series. Reports the **realised** data-driven B6 congestion rent (the headline, from `realized_b6_congestion_rent`) and the within-unit-day strategic-markup estimate (`estimate_markup_pooled`). |
| `robustness_classification.py` | Robustness of the Scotland/B6 unit classification (Appendix C): threshold-invariance of the loss-factor cut, the headline under alternative definitions, and a named cross-check of the curtailed units. |
| `structural_observed_limit.py` | Structural two-zone annual rent under NESO's **observed** SCOTEX limits (the £754m figure quoted in §6 alongside the fixed-ETYS-limit one). |
| `nonconvex_experiment.py` | Controlled demonstration of Proposition 1 on the purpose-built convex/nonconvex instances with known costs (welfare loss exhibited, decomposed, and recovered by recommitment). Offline; needs only a solver. |
| `flex_siting.py` | Optimal **siting and operation** of flexibility across the nested B4/B6 cascade (multi-period storage LP). Returns the value of a flexibility budget, the optimal N/S split, and the marginal value = downstream boundary shadow prices (Proposition 3). Offline; needs only a solver. |
| `make_gbmap.py` | Generates Figure 2 (`fig_gbmap.pdf`): the full GB network (ETYS substations) and the reduced 29-bus network coloured by the N/S/E zones, from PyPSA-GB open data (auto-fetched). Needs `pandas` + `matplotlib`. |
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

### 5. Annualised cost (network + solver) — §6 "From a day to a year"
```bash
python run_annual.py
```
Pulls NESO's daily constraint cost for all 365 days of the financial year,
stratifies them by cost, solves a random sample per stratum, and reports the
**realised** annual B6 congestion rent (data-driven, ≈ £1.1bn/yr, ±£98m — about
59% of the system-wide constraint cost, correlation 0.97 with the NESO series) and
the within-unit-day markup estimate (mu_hat≈0). Writes `annual_sample_<FY>.csv`.

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
| **Daily/annual constraint cost** | NESO Data Portal “Daily Balancing Costs” | validation + annualisation anchor (the *Constraints* column) |

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

- The **annual headline (≈£1.1bn/yr)** is the *realised, BM-visible* B6 rent
  (≈59% of system constraint cost). It captures transmission-connected Scottish
  wind; curtailment of *embedded* (distribution-connected) wind settles outside the
  Balancing Mechanism and is excluded, so it is, if anything, a mild lower bound.
  (Scotland is identified by GSP group OR zonal transmission loss factor — the TLF
  leg is essential because transmission-connected units carry no GSP group; see
  `robustness_classification.py` and the paper's classification appendix.)
- The **structural two-zone model** reproduces the mechanism and drives the
  sensitivity (relative effects), but its absolute *level* is sensitive to the
  capability assumption (£165m at the ETYS limit vs £754m at observed SCOTEX
  limits — both below the directly measured £1.1bn), which is why the level is
  measured directly rather than modelled (`structural_observed_limit.py`).
- The strategic-markup **RD estimator needs a non-binding control**; on all-binding
  high-wind days it is not point-identified (the code detects this and falls back).
- A full **multi-zone** build (NESO Reduced Model + FES) — which would capture
  embedded curtailment and a complete B6 figure — is left as documented loader
  contracts in `gb_two_stage_skeleton.py` for future work.

---

## Reproducing the results
Empirical tables and figures — one command:
```bash
pip install -r requirements.txt      # pyomo, highspy, requests, pandas, pyarrow
python reproduce_all.py              # runs every script; output in reproduce_all.log
```
This repository contains the **code and data** only; the manuscript itself is not
included here.

## Citation
If you use this code, please cite the paper and the software (see `CITATION.cff`).
The pricing framework this extends is `bichler2022` in `references.bib`.

## License
Code released under the [MIT License](LICENSE). Data retrieved at run time from
the Elexon BMRS Insights API and the NESO Data Portal are © their respective
providers and subject to their own terms of use.
