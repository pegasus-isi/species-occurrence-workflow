# Species Occurrence Comparison Workflow

A small Pegasus workflow that compares where and when several species have been
recorded, using public [GBIF](https://www.gbif.org/) occurrence data.

It exists as a teaching example for spec-driven workflow development: [`SPEC.md`](SPEC.md)
was written first, reviewed, and then implemented. Every constraint, non-constraint and
validation criterion in the code traces back to it.

**No API key or account is required.**

## Shape

```
for each species:
    fetch      -> <species>_occurrences.csv
    summarise  -> <species>_stats.json, <species>_map.png

merge (all species) -> comparison.csv, comparison.png
```

Two jobs per species plus one merge.

## Try it without Pegasus

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install pandas matplotlib
./run_manual.sh
```

That runs every stage as a plain script and writes `manual_output/comparison.csv`.

## Run it with Pegasus

```bash
# 1. build the container
apptainer build Apptainer/Species_Container.sif Apptainer/Species_Container.def

# 2. generate the workflow (writes the catalogs; does not submit)
./workflow_generator.py \
    --species "Danaus plexippus" "Vanessa cardui" "Apis mellifera" \
    --year-from 2015 --year-to 2024 --max-records 900

# 3. plan and submit (the generator prints this command; "compute" comes
#    from a site catalog: -s FILE in step 2, or ~/.pegasusrc — see Sites)
pegasus-plan --dir submit -s compute -o local --output-dir "$PWD/output" --submit workflow.yml

# 4. watch
pegasus-status <run-directory>
```

On a plain HTCondor pool with no site catalog, use Pegasus's built-in
`condorpool` site instead:

```bash
./workflow_generator.py -e condorpool
pegasus-plan --dir submit -s condorpool -o local --output-dir "$PWD/output" --submit workflow.yml
```

Outputs land in `./output/`: the printed plan command passes `--output-dir`
(otherwise Pegasus's built-in `local` site would use `./wf-output/`), and the
notebook's catalog stages them there too.

Or run it from the notebook, which drives the same generator class step by
step — it writes the `create_sites_catalog()` placeholder (local + HTCondor
`compute`) — and submits from an explicit cell:

```bash
jupyter notebook Species-Occurrence-Workflow.ipynb
```

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--species` | three insects | Scientific names, space separated |
| `--year-from`, `--year-to` | 2015, 2024 | Bounds which records are eligible |
| `--max-records` | 900 | Cap per species; see non-constraint N7 |
| `--container-sif` | `Apptainer/Species_Container.sif` | Built image |
| `-s`, `--hosted-site-catalog` | (none; `~/.pegasusrc` if set) | [Hosted site catalog](https://github.com/pegasushub/pegasus-site-catalogs/tree/main/conf) to plan against, e.g. `access-pegasus.yml`, `unity.yml` |
| `-e`, `--execution-site-name` | `compute` | Execution site name; `condorpool` on a plain HTCondor pool with no site catalog |
| `-o`, `--output` | `workflow.yml` | Output workflow file |

### Sites

The generator writes no site catalog. Jobs run on a site named `compute`,
which a [centrally hosted site catalog](https://github.com/pegasushub/pegasus-site-catalogs/tree/main/conf)
defines: name it with `-s access-pegasus.yml` (or `unity.yml`, ...), or once
in `~/.pegasusrc` (`pegasus.catalog.site.repo.file = ...`). On a plain
HTCondor pool with no catalog, generate with `-e condorpool` — Pegasus builds
a default `condorpool` site — and plan with `-s condorpool`.

## Outputs

| File | Contents |
|---|---|
| `<species>_stats.json` | Record count, latitude range and median, year range, top countries, taxon key and match confidence |
| `<species>_map.png` | Scatter of record coordinates |
| `comparison.csv` | One row per requested species |
| `comparison.png` | Records per species, and latitudinal spread |

Raw occurrence files stay on the execution side and are not staged back (SPEC C7).

## Things worth knowing

- **A species with no records is not an error.** It appears in the comparison table with
  a zero count, and the run completes (SPEC C6, V5).
- **Occurrence counts reflect observer effort**, not abundance. The output describes
  records, not distributions (SPEC N3).
- **The record cap is not stratified by year.** GBIF returns its own ordering, weighted
  towards recent submissions, so a capped request tends to draw from the most recent
  years within the range (SPEC N7).

## License

Apache 2.0
