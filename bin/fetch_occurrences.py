#!/usr/bin/env python3
"""Fetch georeferenced GBIF occurrence records for one species.

Implements SPEC.md C2 (resolve the name inside this job), C3 (coordinates only,
bounded by year), C4 (page within the 300-record API cap), C5 (retry, then still
write the declared output) and C6 (an absent species is not an error).
"""
import argparse
import csv
import json
import logging
import sys
import time
import urllib.parse
import urllib.request

API = "https://api.gbif.org/v1"
PAGE_CAP = 300  # GBIF caps `limit` at 300 per request (SPEC C4)
COLUMNS = ["species", "taxon_key", "latitude", "longitude",
           "year", "month", "country", "basis_of_record"]

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("fetch")


def get_json(path, params, retries=4, backoff=3):
    """GET with retries and backoff (SPEC C5)."""
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except Exception as exc:
            if attempt == retries:
                raise
            wait = backoff * attempt
            log.warning("attempt %d/%d failed (%s); retrying in %ds",
                        attempt, retries, exc, wait)
            time.sleep(wait)


def write_csv(path, rows):
    """Always write the declared output, even when empty (SPEC C5, C6)."""
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    log.info("wrote %s (%d records)", path, len(rows))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--species", required=True,
                    help="scientific name; spaces may be written as underscores, "
                         "because workflow argument lists are whitespace separated")
    ap.add_argument("--year-from", type=int, required=True)
    ap.add_argument("--year-to", type=int, required=True)
    ap.add_argument("--max-records", type=int, default=900)
    ap.add_argument("--output", required=True)
    ap.add_argument("--match-output", required=True,
                    help="where to record the resolved key and confidence (E2, V6)")
    args = ap.parse_args()
    args.species = args.species.replace("_", " ").strip()

    try:
        match = get_json("/species/match", {"name": args.species})
    except Exception as exc:
        log.error("name resolution failed for %r: %s", args.species, exc)
        write_csv(args.output, [])
        json.dump({"species": args.species, "taxon_key": None, "confidence": None,
                   "match_type": "ERROR"}, open(args.match_output, "w"), indent=2)
        return 1  # required input unavailable: fail loudly, output still written

    key = match.get("usageKey")
    json.dump({"species": args.species,
               "taxon_key": key,
               "confidence": match.get("confidence"),
               "match_type": match.get("matchType"),
               "accepted_name": match.get("scientificName")},
              open(args.match_output, "w"), indent=2)

    if not key or match.get("matchType") == "NONE":
        log.error("no GBIF taxon matches %r; writing empty output", args.species)
        write_csv(args.output, [])
        return 0  # absent species is not a failure (SPEC C6)

    log.info("%r resolved to taxonKey=%s (%s, confidence %s)",
             args.species, key, match.get("matchType"), match.get("confidence"))

    rows, offset = [], 0
    while len(rows) < args.max_records:
        page = min(PAGE_CAP, args.max_records - len(rows))
        try:
            data = get_json("/occurrence/search", {
                "taxonKey": key, "hasCoordinate": "true",
                "year": f"{args.year_from},{args.year_to}",
                "limit": page, "offset": offset})
        except Exception as exc:
            log.error("occurrence search failed at offset %d: %s", offset, exc)
            write_csv(args.output, rows)
            return 1

        for r in data.get("results", []):
            rows.append({
                "species": args.species,
                "taxon_key": key,
                "latitude": r.get("decimalLatitude"),
                "longitude": r.get("decimalLongitude"),
                "year": r.get("year"),
                "month": r.get("month"),
                "country": r.get("countryCode"),
                "basis_of_record": r.get("basisOfRecord"),
            })
        if data.get("endOfRecords") or not data.get("results"):
            break
        offset += page

    write_csv(args.output, rows[:args.max_records])
    return 0


if __name__ == "__main__":
    sys.exit(main())
