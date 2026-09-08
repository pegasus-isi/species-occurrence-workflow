#!/usr/bin/env python3
"""Merge per-species statistics into one comparison table and figure (SPEC E3, V4)."""
import argparse
import json
import logging
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("merge")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inputs", nargs="+", required=True, help="statistics JSON files")
    ap.add_argument("--table-output", required=True)
    ap.add_argument("--figure-output", required=True)
    args = ap.parse_args()

    rows = []
    for path in args.inputs:
        s = json.load(open(path))
        top = s.get("top_countries") or {}
        rows.append({
            "species": s["species"],
            "taxon_key": s.get("taxon_key"),
            "records": s.get("record_count", 0),
            "lat_median": s.get("lat_median"),
            "lat_min": s.get("lat_min"),
            "lat_max": s.get("lat_max"),
            "first_year": s.get("first_year"),
            "last_year": s.get("last_year"),
            "top_country": next(iter(top), None),
        })

    # One row per requested species, including any with no records (SPEC V4)
    df = pd.DataFrame(rows).sort_values("records", ascending=False)
    df.to_csv(args.table_output, index=False)
    log.info("wrote %s (%d species)", args.table_output, len(df))

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.8))

    ax1.barh(df.species, df.records, color="#0ea5e9")
    ax1.set_xlabel("records retrieved")
    ax1.set_title("Records per species")
    ax1.invert_yaxis()

    present = df[df.records > 0]
    if len(present):
        ax2.hlines(present.species, present.lat_min, present.lat_max,
                   color="#94a3b8", linewidth=3)
        ax2.plot(present.lat_median, present.species, "o", color="#0284c7")
        ax2.set_xlabel("latitude (line = range, dot = median)")
        ax2.set_title("Latitudinal spread of records")
        ax2.invert_yaxis()
        ax2.grid(alpha=0.25, axis="x")
    else:
        ax2.text(0.5, 0.5, "no records", ha="center", va="center",
                 transform=ax2.transAxes)

    fig.tight_layout()
    fig.savefig(args.figure_output, dpi=140)
    log.info("wrote %s", args.figure_output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
