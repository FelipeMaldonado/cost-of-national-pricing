# Release v0.2.0 — Energy Policy submission

This release accompanies the resubmission *“What Consumers Pay for National Pricing:
Socialised Redispatch Expenditure on the GB Scotland–England Boundary.”* It supersedes
**v0.1.0** (Energy Economics submission).

## What changed since v0.1.0
- **Headline is now a full-year census, not a 30-day sample.** The realised B6
  redispatch expenditure is computed directly for **all 365 days** of FY 2024/25:
  **£1,184m** (£401m / 34% curtailment + £783m / 66% replacement), 9.92 TWh,
  ≈ **62%** of the system-wide constraint cost. New: `run_fullyear.py`.
- **Multi-year trend added:** FY 2022/23 £570m → FY 2023/24 £714m → FY 2024/25
  £1,184m (share 32% → 51% → 62%). New: `run_multiyear.py`, with the NESO
  Daily-BSUoS-Cost resource ids for 2022/23 and 2023/24 wired into
  `gb_empirical_pipeline.py` (`NESO_DBC_RESOURCES`).
- **`robustness_classification.py` rewritten** from the 30-day estimator to the
  full-year (parse-once) computation; reproduces the classification appendix.
- **NESO reframed** as a *consistency check* on the independently constructed measure,
  not an external validation.
- **`run_annual.py` demoted to legacy** (the pre-full-year stratified estimator), kept
  for reference.
- Terminology in the paper settled on **`R_res`** (resource cost), **`C(o,b)`** (paid
  redispatch cost), **`E`** (measured expenditure); the code retains the legacy
  `R_cong` / `R_cong_obs` names (and the CSV columns follow the code) — see the README
  notation note.
- Docs: README, `CITATION.cff`, `.zenodo.json` updated; DOI references switched to the
  **concept DOI** `10.5281/zenodo.20812217` (always resolves to the latest version).

## Files added / changed
- Added: `run_fullyear.py`, `run_multiyear.py`, `fullyear_2022-2023.csv`,
  `fullyear_2023-2024.csv`, `fullyear_2024-2025.csv`, `.zenodo.json`,
  `RELEASE_NOTES_v0.2.0.md`.
- Changed: `gb_empirical_pipeline.py`, `robustness_classification.py`,
  `reproduce_all.py`, `README.md`, `CITATION.cff`, `.gitignore`.

## Publish checklist (do these yourself — outward-facing)

1. **Commit & push** (a branch + PR, or straight to `main`), then tag:
   ```bash
   git tag -a v0.2.0 -m "Energy Policy submission: full-year + multi-year measure"
   git push origin v0.2.0
   ```
2. **GitHub Release** from tag `v0.2.0`, pasting the “What changed” section above as
   the notes.
3. **Zenodo.** The repo’s GitHub–Zenodo webhook creates a **new version** draft under
   concept DOI `10.5281/zenodo.20812217` from the release tarball (code + committed
   files only — it does **not** include the 1.2 GB cache). In the Zenodo draft:
   - upload the full data bundle (see below) as an extra file, so the record is
     code + data and reproduces everything offline;
   - confirm the metadata (`.zenodo.json` pre-fills it), then **Publish** — this mints
     the new version DOI.
   If the webhook is not enabled, use Zenodo → the existing record → **New version**,
   upload the release tarball + the data bundle, and publish.
4. **Build the data bundle** (the full multi-year cache + full-year CSVs):
   ```bash
   zip -r bmrs_cache_full_v0.2.0.zip bmrs_cache fullyear_*.csv neso_daily_*.csv
   ```
   (~1.2 GB; Zenodo allows up to 50 GB per record.)
5. **Manuscript.** The Data Availability statement already cites the concept DOI
   `10.5281/zenodo.20812217`; update the version DOI in the camera-ready if the journal
   wants the exact version.
