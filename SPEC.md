# SPEC: Species Occurrence Comparison Workflow

## 1. Goal

For a set of species, retrieve georeferenced occurrence records from GBIF, summarise
where and when each species has been recorded, and produce one table and one figure
comparing them.

The workflow is deliberately small. It exists as a teaching example for spec-driven
Pegasus workflow development, so clarity of structure matters more than depth of
analysis.

## 2. Inputs

| Input | Source | Notes |
|---|---|---|
| Species names | Command line (`--species`) | Scientific names, e.g. `"Danaus plexippus"` |
| Occurrence records | GBIF `api.gbif.org` | Public, **no API key**, no account |
| Year range | Command line (`--year-from`, `--year-to`) | Bounds the query so reruns are comparable |

Two GBIF endpoints are used:

- `/v1/species/match?name=<name>` resolves a scientific name to a numeric `usageKey`.
- `/v1/occurrence/search` returns records for that key. **`limit` is capped at 300 per
  request**, so more than 300 records requires paging with `offset`.

## 3. Architecture and stages

```
for each species:
    fetch      -> <species>_occurrences.csv
    summarise  -> <species>_stats.json, <species>_map.png

merge (all species) -> comparison.csv, comparison.png
```

Two jobs per species plus one merge. With four species that is nine jobs.

## 4. Constraints — the workflow MUST

- **C1**: Require no credentials. A reader with a clean machine can run it.
- **C2**: Resolve species names to GBIF taxon keys **inside the fetch job**, not on the
  submit host, so the submit host needs no network access at generation time.
- **C3**: Request only records with coordinates (`hasCoordinate=true`), and bound every
  query by the requested year range.
- **C4**: Page the occurrence API correctly, respecting the 300-record cap per request,
  and stop at the requested maximum.
- **C5**: Retry failed API calls with backoff. On persistent failure, still write the
  declared output file, then exit non-zero.
- **C6**: A species with zero records is **not** an error. Write an empty file with the
  correct header, log it, and exit 0, so one absent species cannot fail the run.
- **C7**: Only the final comparison table, the figures and the per-species statistics are
  staged out. Raw occurrence files stay on the execution side.
- **C8**: Pass species names to jobs with spaces encoded as underscores, and decode them
  in the scripts. Workflow argument lists are whitespace separated, so a bare two-word
  name arrives as two arguments and the job exits on an argument-parsing error before it
  can write anything.

## 5. Non-constraints — the workflow need NOT

- **N1**: Produce identical results over time. GBIF is a living database and record
  counts grow. Reruns are comparable within a fixed year range, not identical.
- **N2**: Handle synonyms, subspecies or ambiguous name matches. Whatever
  `species/match` returns as the accepted key is what we use, and the match confidence is
  recorded so a reader can see when it was uncertain.
- **N3**: Correct for sampling bias. Occurrence density reflects observer effort as much
  as species range. The output describes records, not distributions, and says so.
- **N4**: Draw a real basemap. A scatter of coordinates on plain axes is sufficient;
  coastlines would add a heavyweight geospatial dependency for no analytical gain.
- **N5**: Support more than a few thousand records per species. This is an illustration,
  not a data-mining pipeline.
- **N6**: Run on any particular infrastructure. A laptop or an HTCondor pool are both
  fine.
- **N7**: Sample evenly across the requested year range. GBIF returns records in its own
  order, which is weighted towards recent submissions, so a capped request tends to draw
  from the most recent years rather than spreading across the range. The year bounds
  restrict which records are *eligible*; they do not stratify the sample. Discovered on
  the first run: 600 monarch records requested for 2015-2024 all came from 2024.

## 6. Expected outcomes

- **E1**: One CSV per species with the declared columns, at most `--max-records` rows.
- **E2**: One statistics JSON per species: record count, latitude and longitude range,
  median latitude, first and last year, top five recording countries, and the taxon key
  and match confidence used.
- **E3**: One scatter figure per species, plus a comparison table and a combined figure
  covering all requested species.

## 7. Validation criteria

| ID | Criterion | Pass condition |
|---|---|---|
| V1 | Schema | Every occurrence CSV has exactly the columns `species,taxon_key,latitude,longitude,year,month,country,basis_of_record`. |
| V2 | Coordinates present | No row has an empty latitude or longitude; all latitudes in [-90, 90] and longitudes in [-180, 180]. |
| V3 | Bounds respected | Row count is at most `--max-records`; every `year` lies within the requested range. |
| V4 | Completeness | The comparison table has exactly one row per requested species, including any that returned no records. |
| V5 | Empty-species tolerance | A run including a deliberately absent name (`"Nonexistentus fakus"`) completes successfully, with that species present in the table showing a zero count. |
| V6 | Provenance | Each statistics JSON records the taxon key, match confidence, year range and the maximum-records setting used. |

## 8. Open design issues

1. **Where to resolve species names.** Resolving on the submit host would let the
   generator name output files after the accepted taxon key. Resolving inside the job
   keeps the submit host offline and makes the workflow runnable where the submit host
   has no outbound access. **Chosen: resolve inside the job** (C2). File names are
   derived from the requested name instead.

2. **How to bound the query.** Options were a record cap, a year range, or both. A cap
   alone makes reruns incomparable as the database grows, since new records displace old
   ones. **Chosen: both** — a year range for comparability, a cap for run time.

3. **Whether an absent species fails the run.** For a comparison across several species,
   failing everything because one name returned nothing is the wrong behaviour.
   **Chosen: absent species are recorded with a zero count** (C6, V5).

4. **Map rendering.** Deferred. Plain scatter axes for now (N4); a proper basemap is a
   possible later addition if the figures are ever used for anything beyond illustration.

## 9. Deliverables

```
species-occurrence-workflow/
├── SPEC.md                  # this file
├── workflow_generator.py    # Pegasus DAG generator
├── bin/
│   ├── fetch_occurrences.py
│   ├── summarise_species.py
│   └── merge_species.py
├── Apptainer/Species_Container.def
├── run_manual.sh            # runs every stage without Pegasus
└── README.md
```
