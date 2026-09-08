#!/usr/bin/env python3
"""Summarise one species' occurrence records (SPEC E2, E3).

Reads the CSV written by fetch_occurrences.py and produces a statistics JSON and
a scatter figure. Handles the empty-input case without failing (SPEC C6, V5).
"""
import argparse
import json
import logging
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("summarise")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--match", required=True, help="match JSON from the fetch job")
    ap.add_argument("--species", required=True)
    ap.add_argument("--year-from", type=int, required=True)
    ap.add_argument("--year-to", type=int, required=True)
    ap.add_argument("--max-records", type=int, required=True)
    ap.add_argument("--stats-output", required=True)
    ap.add_argument("--figure-output", required=True)
    args = ap.parse_args()
    args.species = args.species.replace("_", " ").strip()

    df = pd.read_csv(args.input)
    match = json.load(open(args.match))

    # Provenance, recorded whether or not there are records (SPEC V6)
    stats = {
        "species": args.species,
        "taxon_key": match.get("taxon_key"),
        "match_confidence": match.get("confidence"),
        "accepted_name": match.get("accepted_name"),
        "year_from": args.year_from,
        "year_to": args.year_to,
        "max_records": args.max_records,
        "record_count": int(len(df)),
    }

    if len(df):
        stats.update({
            "lat_min": round(float(df.latitude.min()), 4),
            "lat_max": round(float(float(df.latitude.max())), 4),
            "lat_median": round(float(df.latitude.median()), 4),
            "lon_min": round(float(df.longitude.min()), 4),
            "lon_max": round(float(df.longitude.max()), 4),
            "first_year": int(df.year.min()),
            "last_year": int(df.year.max()),
            "top_countries": df.country.value_counts().head(5).to_dict(),
        })
    else:
        log.warning("no records for %r; writing a zero-count summary", args.species)

    with open(args.stats_output, "w") as fh:
        json.dump(stats, fh, indent=2)
    log.info("wrote %s", args.stats_output)

    fig, ax = plt.subplots(figsize=(9, 4.6))
    if len(df):
        ax.scatter(df.longitude, df.latitude, s=9, alpha=0.5,
                   color="#0ea5e9", edgecolors="none")
        ax.set_title(f"{args.species} — {len(df)} records, "
                     f"{args.year_from}–{args.year_to}")
    else:
        ax.text(0.5, 0.5, f"no records for {args.species}", ha="center",
                va="center", transform=ax.transAxes, color="#64748b")
        ax.set_title(f"{args.species} — no records")
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.set_xlim(-180, 180)
    ax.set_ylim(-90, 90)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(args.figure_output, dpi=140)
    log.info("wrote %s", args.figure_output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
