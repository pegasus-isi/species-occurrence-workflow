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

# 2. generate the workflow
./workflow_generator.py \
    --species "Danaus plexippus" "Vanessa cardui" "Apis mellifera" \
    --year-from 2015 --year-to 2024 --max-records 900

# 3. submit
pegasus-plan --submit -s compute -o local workflow.yml

# 4. watch
pegasus-status <run-directory>
```

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--species` | three insects | Scientific names, space separated |
| `--year-from`, `--year-to` | 2015, 2024 | Bounds which records are eligible |
| `--max-records` | 900 | Cap per species; see non-constraint N7 |
| `--container-sif` | `Apptainer/Species_Container.sif` | Built image |
| `-e`, `--execution-site` | `compute` | Site to plan against (the name hosted catalogs give their site) |
| `-s`, `--hosted-site-catalog` | (none; `~/.pegasusrc` if set) | Hosted site catalog to plan against, e.g. `unity.yml` |
| `--site-style` | `auto` | `auto`: keep an existing `sites.yml` entry or hosted catalog, else write `compute` as an HTCondor pool; `condor`/`slurm`: (re)write it; `none`: leave `sites.yml` alone |
| `--queue`, `--project` | — | Batch partition and allocation account (Slurm) |
| `--site-scratch` | `./work` | Slurm shared scratch visible to workers and the submit host |
| `--site-profile`, `--tag-profile` | — | Extra profiles on the site or on tagged jobs; repeatable |
| `--shared-filesystem` | `auto` | Read inputs directly from the submit host (on for Slurm, off for HTCondor) |
| `--sites-yml` | `sites.yml` | Local site catalog |

### Sites

The workflow names no scheduler: each job states memory and a wall-clock
runtime, and plans against a site named `compute`. With `-s unity.yml` (or a
hosted catalog set in `~/.pegasusrc`) the hosted catalog defines it; with no
options `custom_sites.py` writes it to `sites.yml` as an HTCondor pool. For a
Slurm cluster:

```bash
./workflow_generator.py --site-style slurm --queue cpu --project my_lab
pegasus-plan --submit -s compute -o local workflow.yml
```

`custom_sites.py` also runs standalone (`./custom_sites.py --help`).

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
