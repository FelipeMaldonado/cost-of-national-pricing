"""
reproduce_all.py
================
One-command reproduction of every quantitative artifact in
"What Consumers Pay for National Pricing" (Energy Policy submission).

Runs each pipeline script in dependency order, tees output to
``reproduce_all.log``, and prints a manifest mapping every paper table/figure to
the script that produces it and the file it writes.

Usage
-----
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt          # pyomo, highspy, requests, pandas, pyarrow
    python reproduce_all.py                   # run everything
    python reproduce_all.py run_annual sensitivity   # run a subset (by name)
    python reproduce_all.py --list            # list the steps and exit

Requirements: a working HiGHS solver (via highspy) and network access to the
public, key-free Elexon BMRS and NESO Data Portal APIs. A populated
``bmrs_cache/`` makes the run reproducible offline for the cached days; without
it, the scripts fetch live data (results are identical for settled days).
"""
from __future__ import annotations
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "reproduce_all.log"

# Use the active interpreter (so the venv's python is picked up automatically).
PYTHON = sys.executable

# (step name, script, paper artifact it produces, output file written) ---------
# The full-year and multi-year runs are resumable: with the committed fullyear_*.csv
# present they finish immediately; deleting a CSV re-computes that year (slow: the
# realised measure is ~24 s/settlement-day against the cache, longer if re-fetching).
STEPS = [
    ("make_gbmap",      "make_gbmap.py",
     "GB network map figure",                                    "fig_gbmap.pdf"),
    ("nonconvex",       "nonconvex_experiment.py",
     "Nonconvex welfare-loss instance (Prop 1(b), Sec 6.1)",     "(stdout)"),
    ("run_paper",       "run_paper.py",
     "B6 peak-day resource-cost decomposition + reconciliation (Sec 6.2)", "results_b6_2024-12-08.csv"),
    ("run_fullyear",    "run_fullyear.py",
     "Full-year B6 redispatch expenditure, FY2024/25 headline (Sec 6.3)",  "fullyear_2024-2025.csv"),
    ("run_multiyear",   "run_multiyear.py",
     "Multi-year B6 expenditure trend, FY2022/23-2023/24 (Sec 6.3)",       "fullyear_2022-2023.csv, fullyear_2023-2024.csv"),
    ("sensitivity",     "sensitivity.py",
     "Comparative-statics sweeps (Sec 6.4 + Appendix C)",        "sensitivity_2024-12-08.csv"),
    ("structural_obs",  "structural_observed_limit.py",
     "Structural level at observed SCOTEX limits (Sec 6.3)",     "(stdout)"),
    ("flex_siting",     "flex_siting.py",
     "Flexibility siting across nested boundaries (Prop 3, Appendix B)",   "(stdout/figure)"),
    ("robustness",      "robustness_classification.py",
     "Scotland/B6 classification robustness, full year (Appendix D)",      "(stdout)"),
    ("run_annual",      "run_annual.py",
     "Legacy 30-day stratified estimate (superseded by run_fullyear)",     "annual_sample_2024-2025.csv"),
]


def _preflight() -> None:
    """Warn early if the core dependencies or the solver are missing."""
    missing = []
    for mod in ("pyomo", "highspy", "requests", "pandas", "pyarrow"):
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    if missing:
        print(f"[preflight] WARNING: missing packages {missing}; "
              f"run `pip install -r requirements.txt` first.\n")
    if not (ROOT / "bmrs_cache").exists():
        print("[preflight] note: bmrs_cache/ not found -- scripts will fetch live "
              "BMRS/NESO data (needs network).\n")


def _run_step(name: str, script: str, log) -> tuple[str, float]:
    """Run one script as a subprocess, tee its output to console + log."""
    banner = f"\n{'='*70}\n### {name}  ({script})\n{'='*70}"
    print(banner, flush=True); log.write(banner + "\n")
    t0 = time.time()
    proc = subprocess.Popen(
        [PYTHON, str(ROOT / script)], cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line); sys.stdout.flush(); log.write(line)
    proc.wait()
    dt = time.time() - t0
    status = "OK" if proc.returncode == 0 else f"FAIL(exit {proc.returncode})"
    print(f"--- {name}: {status} in {dt:.0f}s", flush=True)
    return status, dt


def main(argv: list[str]) -> int:
    if "--list" in argv:
        for n, s, art, out in STEPS:
            print(f"  {n:16s} {s:30s} -> {art}")
        return 0
    selected = [a for a in argv if not a.startswith("-")]
    steps = [s for s in STEPS if (not selected or s[0] in selected)]
    if selected and not steps:
        print(f"No steps match {selected}. Use --list to see names.")
        return 2

    _preflight()
    results = []
    with open(LOG, "w") as log:
        log.write(f"reproduce_all.py run, python={PYTHON}\n")
        for name, script, artifact, out in steps:
            status, dt = _run_step(name, script, log)
            results.append((name, artifact, out, status, dt))

    # ---- manifest -----------------------------------------------------------
    print(f"\n{'='*70}\nMANIFEST  (paper artifact <- script <- output)\n{'='*70}")
    print(f"{'artifact':46s} {'output file':30s} {'status'}")
    for name, artifact, out, status, dt in results:
        print(f"{artifact[:45]:46s} {out[:30]:30s} {status}")
    n_fail = sum(1 for *_, status, _ in results if not status.startswith("OK"))
    print(f"\nLog written to {LOG.name}. "
          f"{len(results)-n_fail}/{len(results)} steps OK.")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
