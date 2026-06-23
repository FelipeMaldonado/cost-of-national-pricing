"""
make_gbmap.py
=============
Generates Figure 2: the GB transmission network and its reduction to the B4/B6
cascade, from PyPSA-GB open data (https://github.com/andrewlyden/PyPSA-GB).

  Left  : the full network -- ETYS substation locations (lat/lon), which trace GB.
  Right : the reduced 29-bus network (British National Grid x/y), coloured by the
          three model zones (North Scotland / South Scotland / England) with the
          nested B4 and B6 cuts drawn.

Writes fig_gbmap.pdf (vector, for \\includegraphics in the paper) and a .png.
Data CSVs are pulled once to /tmp (see FETCH below) or read locally if present.

Run:  python make_gbmap.py     (needs pandas + matplotlib; no solver)
"""
from __future__ import annotations
import os
import math
import urllib.request
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ---- data: PyPSA-GB raw CSVs, cached locally next to this script ------------ #
_RAW = "https://raw.githubusercontent.com/andrewlyden/PyPSA-GB/HEAD/data/network"
_FILES = {
    "pg_substations.csv": f"{_RAW}/ETYS/substation_coordinates.csv",      # site_name, lat, lon
    "pg_reduced_buses.csv": f"{_RAW}/reduced_network/buses.csv",          # name, x, y (EPSG:27700)
}


def _ensure(fname, url):
    if not os.path.exists(fname):
        urllib.request.urlretrieve(url, fname)
    return fname


SUB = _ensure("pg_substations.csv", _FILES["pg_substations.csv"])
BUS = _ensure("pg_reduced_buses.csv", _FILES["pg_reduced_buses.csv"])

# zone split of the reduced buses by BNG northing (metres):
#   North Scotland above B4; South Scotland between B4 and B6; England below B6.
B4_Y = 726_000.0     # between Errochty (761k) and Denny (692k)
B6_Y = 604_000.0     # between Eccles (643k, SCO) and Stella West (566k, ENG)
# approximate latitudes of the same cuts for the geographic (left) panel
B4_LAT, B6_LAT = 56.85, 55.10


def _zone(y):
    return "N. Scotland" if y > B4_Y else ("S. Scotland" if y > B6_Y else "England & Wales")


ZCOL = {"N. Scotland": "#1b6ca8", "S. Scotland": "#2a9d8f", "England & Wales": "#e76f51"}


def main():
    sub = pd.read_csv(SUB)
    bus = pd.read_csv(BUS)
    bus["zone"] = bus["y"].apply(_zone)

    fig, (axL, axR) = plt.subplots(1, 2, figsize=(9.2, 5.6))

    # ---- (a) full network: substations trace GB ---------------------------- #
    axL.scatter(sub["lon"], sub["lat"], s=6, c="0.45", alpha=0.7, linewidths=0)
    for lat, lab in ((B6_LAT, "B6"), (B4_LAT, "B4")):
        axL.axhline(lat, color="crimson", ls="--", lw=1.2)
        axL.text(1.9, lat + 0.04, lab, color="crimson", fontsize=10, ha="right")
    axL.set_aspect(1.0 / math.cos(math.radians(55)))   # de-distort lon at ~55N
    axL.set_title("(a) Full GB transmission network\n(ETYS substations)", fontsize=10)
    axL.set_xlabel("longitude"); axL.set_ylabel("latitude")
    axL.set_xlim(-8.2, 2.4)

    # ---- (b) reduced network + the B4/B6 cascade --------------------------- #
    for z, g in bus.groupby("zone"):
        axR.scatter(g["x"] / 1e3, g["y"] / 1e3, s=55, c=ZCOL[z], label=z,
                    edgecolors="white", linewidths=0.6, zorder=3)
    for yv, lab in ((B6_Y, "B6 (Scotland–England)"), (B4_Y, "B4")):
        axR.axhline(yv / 1e3, color="crimson", ls="--", lw=1.3, zorder=2)
        axR.text((axR.get_xlim()[0] if False else 270), yv / 1e3 + 6, lab,
                 color="crimson", fontsize=9)
    for _, r in bus.iterrows():                          # label a few anchors
        if r["name"] in ("Beauly", "Eccles", "London", "Deeside", "Errochty"):
            axR.annotate(r["name"], (r["x"] / 1e3, r["y"] / 1e3),
                         fontsize=7, xytext=(4, 3), textcoords="offset points")
    axR.set_aspect("equal")
    axR.set_title("(b) Reduced network and the\nnested B4/B6 cascade", fontsize=10)
    axR.set_xlabel("easting (km, OSGB)"); axR.set_ylabel("northing (km, OSGB)")
    axR.legend(loc="lower right", fontsize=8, frameon=True, framealpha=0.92,
               edgecolor="0.8")

    for ax in (axL, axR):
        ax.tick_params(labelsize=8)
    fig.tight_layout()
    fig.savefig("fig_gbmap.pdf", bbox_inches="tight")
    fig.savefig("fig_gbmap.png", dpi=150, bbox_inches="tight")
    print("wrote fig_gbmap.pdf / .png ;  zone counts:",
          bus["zone"].value_counts().to_dict())


if __name__ == "__main__":
    main()
