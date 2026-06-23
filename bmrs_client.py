"""
bmrs_client.py
==============
Thin client for the Elexon BMRS *Insights* API (the modern, fully public API;
no API key required). Used to pull the datasets that populate the empirical
Stage-1 (self-schedule) and Stage-2 (redispatch) objects of the cost-of-national
-pricing study.

Base URL and dataset codes per the Insights developer portal
(https://developer.data.elexon.co.uk/ ; "All our APIs are public and no API key
is required"). Endpoint/parameter names were correct as of mid-2026; if Elexon
revises them, only the URLs in `fetch_stream` / `fetch_registry` need editing.

Datasets used:
  PN    -- Physical Notifications (the self-schedule profile per BM unit)
  BOD   -- Bid-Offer Data (submitted turn-up offer / turn-down bid price ladders)
  BOALF -- Bid Offer Acceptance Level Flagged (accepted levels; soFlag marks
           system/constraint actions, i.e. redispatch rather than energy balancing)

Requires: requests, pandas, pyarrow (for the parquet cache).
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional
import requests
import pandas as pd

BASE = "https://data.elexon.co.uk/bmrs/api/v1"
CACHE = Path("./bmrs_cache")
CACHE.mkdir(exist_ok=True)


def _settlement_window(date: str) -> tuple[str, str]:
    """GB settlement day runs 23:00 UTC (prev day) -> 23:00 UTC. Return ISO
    `from`/`to` covering the 48 periods of `date` (YYYY-MM-DD)."""
    d = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start = d - timedelta(hours=1)          # 23:00 the day before
    end = start + timedelta(days=1)
    iso = lambda x: x.strftime("%Y-%m-%dT%H:%M:%SZ")
    return iso(start), iso(end)


def _get(url: str, params: dict, retries: int = 3) -> list:
    for attempt in range(retries):
        r = requests.get(url, params=params, timeout=120)
        if r.status_code == 200:
            payload = r.json()
            return payload["data"] if isinstance(payload, dict) and "data" in payload else payload
        if r.status_code in (429, 500, 502, 503):
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
    raise RuntimeError(f"BMRS request failed after {retries} retries: {url}")


def fetch_stream(dataset: str, date: str, use_cache: bool = True) -> pd.DataFrame:
    """Pull one settlement day of a dataset via the /datasets/{code}/stream
    endpoint, cached to parquet."""
    cache_file = CACHE / f"{dataset}_{date}.parquet"
    if use_cache and cache_file.exists():
        return pd.read_parquet(cache_file)
    frm, to = _settlement_window(date)
    url = f"{BASE}/datasets/{dataset}/stream"
    rows = _get(url, {"from": frm, "to": to})
    df = pd.DataFrame(rows)
    if not df.empty:
        df.to_parquet(cache_file, index=False)
    return df


def fetch_registry(use_cache: bool = True) -> pd.DataFrame:
    """BM unit registry: fuelType, gspGroup (e.g. _N / _P = N/S Scotland),
    leadPartyName, etc. Used to classify each BM unit by zone and technology."""
    cache_file = CACHE / "bmunits.parquet"
    if use_cache and cache_file.exists():
        return pd.read_parquet(cache_file)
    rows = _get(f"{BASE}/reference/bmunits/all", {})
    df = pd.DataFrame(rows)
    if not df.empty:
        df.to_parquet(cache_file, index=False)
    return df


# convenience wrappers -------------------------------------------------------
def fetch_pn(date: str) -> pd.DataFrame:
    return fetch_stream("PN", date)

def fetch_bod(date: str) -> pd.DataFrame:
    return fetch_stream("BOD", date)

def fetch_boalf(date: str) -> pd.DataFrame:
    return fetch_stream("BOALF", date)

def fetch_mid(date: str) -> pd.DataFrame:
    """Market Index Data: the wholesale (day-ahead/within-day) reference price per
    settlement period, by data provider (APXMIDP, N2EXMIDP), with traded volume.
    Used as the wholesale reference for the strategic-markup estimate."""
    return fetch_stream("MID", date)


def fetch_fuelhh(date: str, use_cache: bool = True) -> pd.DataFrame:
    """GB generation by fuel type, half-hourly (FUELHH): includes WIND (with much
    embedded/distribution-connected output that never appears in BM PNs), CCGT,
    NUCLEAR, NPSHYD, interconnectors, etc. Used to anchor faithful *zonal*
    generation totals. NB: FUELHH's stream filters on publish time, so we pull by
    `publishDateTime` window and keep the target settlement date."""
    cache_file = CACHE / f"FUELHH_{date}.parquet"
    if use_cache and cache_file.exists():
        return pd.read_parquet(cache_file)
    d = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    frm = (d - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    to = (d + timedelta(days=1, hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = _get(f"{BASE}/datasets/FUELHH",
                {"publishDateTimeFrom": frm, "publishDateTimeTo": to})
    df = pd.DataFrame(rows)
    if not df.empty and "settlementDate" in df:
        df = df[df["settlementDate"] == date].copy()
    if not df.empty:
        df.to_parquet(cache_file, index=False)
    return df


def fetch_demand(date: str, use_cache: bool = True) -> pd.DataFrame:
    """National demand outturn per settlement period: initialDemandOutturn (INDO,
    national) and initialTransmissionSystemDemandOutturn (ITSDO, transmission
    system). Used to anchor faithful zonal demand."""
    cache_file = CACHE / f"DEMAND_{date}.parquet"
    if use_cache and cache_file.exists():
        return pd.read_parquet(cache_file)
    rows = _get(f"{BASE}/demand/outturn",
                {"settlementDateFrom": date, "settlementDateTo": date})
    df = pd.DataFrame(rows)
    if not df.empty:
        df.to_parquet(cache_file, index=False)
    return df


if __name__ == "__main__":
    # smoke test (requires network in your environment)
    d = "2025-01-01"
    reg = fetch_registry()
    print("registry rows:", len(reg), "| columns:", list(reg.columns)[:8])
    for ds in ("PN", "BOD", "BOALF"):
        df = fetch_stream(ds, d)
        print(f"{ds} {d}: {len(df)} rows | columns: {list(df.columns)[:10]}")
