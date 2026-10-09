#!/usr/bin/env python3
"""Pegasus workflow generator for the species occurrence comparison (see SPEC.md).

Shape (SPEC §3):

    for each species:  fetch -> summarise
    all species:       merge

Two jobs per species plus one merge.

Sites follow pegasus-isi/pegasus-gromacs: jobs run on a site named "compute",
defined by a centrally hosted site catalog (-s access-pegasus.yml, ...;
https://github.com/pegasushub/pegasus-site-catalogs) or by one in
~/.pegasusrc. The generator writes no site catalog; on a plain HTCondor pool
with no catalog, use -e condorpool. It never plans or submits: it prints the
pegasus-plan command (the notebook submits from an explicit cell).
"""
import argparse
import logging
import os
import re
import sys
from pathlib import Path

from Pegasus.api import *

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("generator")

DEFAULT_SPECIES = ["Danaus plexippus", "Vanessa cardui", "Apis mellifera"]
DEFAULT_YEAR_FROM = 2015
DEFAULT_YEAR_TO = 2024

# Per-tool resource configuration for the transformation catalog.
TOOL_CONFIGS = {
    "fetch_occurrences": {"memory": "1 GB", "cores": 1},
    "summarise_species": {"memory": "2 GB", "cores": 1},
    "merge_species":     {"memory": "2 GB", "cores": 1},
}


def slug(name):
    """A filesystem-safe stem derived from the requested name (SPEC §8, issue 1)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()


class SpeciesOccurrenceWorkflow:
    wf = None
    sc = None
    tc = None
    rc = None
    props = None

    def __init__(self, dagfile="workflow.yml", container_sif=None):
        self.dagfile = dagfile
        self.wf_dir = str(Path(__file__).parent.resolve())
        self.shared_scratch_dir = os.path.join(self.wf_dir, "scratch")
        self.local_storage_dir = os.path.join(self.wf_dir, "output")
        self.container_sif = container_sif

    def write(self):
        """Write all catalogs and workflow to files."""
        if self.sc is not None:
            self.sc.write()
        self.props.write()
        self.rc.write()
        self.tc.write()
        self.wf.write(file=self.dagfile)

    # ------------------------------------------------------------------
    # Plan / run / monitor (thin wrappers over the Pegasus API Workflow
    # object, for interactive use e.g. from a Jupyter notebook)
    # ------------------------------------------------------------------
    def plan_submit(self, exec_site_name="compute", raise_errors=False):
        try:
            self.wf.plan(
                dir="submit",
                sites=[exec_site_name],
                output_sites=["local"],
                cleanup="none",
                verbose=1,
                submit=True,
            )
        except PegasusClientError as e:
            print(e)
            if raise_errors:
                raise

    def status(self):
        try:
            self.wf.status(long=True)
        except PegasusClientError as e:
            print(e)

    def wait(self):
        try:
            self.wf.wait()
        except PegasusClientError as e:
            print(e)

    def statistics(self):
        try:
            self.wf.statistics()
        except PegasusClientError as e:
            print(e)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    def create_pegasus_properties(self, hosted_site_catalog=None):
        self.props = Properties()
        self.props["pegasus.transfer.threads"] = "16"
        if hosted_site_catalog:
            # Use one of Pegasus' centrally hosted site catalogs instead of
            # a locally generated one. pegasus-plan downloads and caches the
            # named file from the catalog repository at plan time.
            # https://pegasus.isi.edu/documentation/reference-guide/catalogs.html#centrally-hosted-site-catalogs
            self.props["pegasus.catalog.site.repo.file"] = hosted_site_catalog

    # ------------------------------------------------------------------
    # Site Catalog
    #
    # Not used by the CLI below — pegasus-plan resolves the site catalog from
    # a centrally hosted one instead (see -s/--hosted-site-catalog and
    # create_pegasus_properties above). Kept for programmatic/notebook use
    # when a self-contained, locally generated HTCondor site catalog is wanted.
    # ------------------------------------------------------------------
    def create_sites_catalog(self, exec_site_name="compute"):
        self.sc = SiteCatalog()

        local = Site("local").add_directories(
            Directory(
                Directory.SHARED_SCRATCH, self.shared_scratch_dir
            ).add_file_servers(
                FileServer("file://" + self.shared_scratch_dir, Operation.ALL)
            ),
            Directory(
                Directory.LOCAL_STORAGE, self.local_storage_dir
            ).add_file_servers(
                FileServer("file://" + self.local_storage_dir, Operation.ALL)
            ),
        )

        exec_site = (
            Site(exec_site_name)
            .add_condor_profile(universe="vanilla")
            .add_pegasus_profile(style="condor")
        )

        self.sc.add_sites(local, exec_site)

    # ------------------------------------------------------------------
    # Transformation Catalog
    # ------------------------------------------------------------------
    def create_transformation_catalog(self, exec_site_name="compute"):
        self.tc = TransformationCatalog()
        container = None
        if self.container_sif:
            sif = self.container_sif if os.path.isabs(self.container_sif) \
                else os.path.join(self.wf_dir, self.container_sif)
            if not os.path.exists(sif):
                log.warning("container not found at %s; build it first", sif)
            container = Container("species_container",
                                  container_type=Container.SINGULARITY,
                                  image="file://" + sif, image_site="local")
            # The container must be registered in the catalog, not merely
            # referenced by the transformations that use it.
            self.tc.add_containers(container)

        for name, cfg in TOOL_CONFIGS.items():
            t = Transformation(name, site=exec_site_name,
                               pfn=os.path.join(self.wf_dir, "bin", name + ".py"),
                               is_stageable=True, container=container)
            t.add_pegasus_profile(memory=cfg["memory"], cores=cfg["cores"])
            self.tc.add_transformations(t)

    # ------------------------------------------------------------------
    # Replica Catalog
    # ------------------------------------------------------------------
    def create_replica_catalog(self):
        # No input files: each fetch job downloads its records from GBIF.
        self.rc = ReplicaCatalog()

    # ------------------------------------------------------------------
    # Workflow DAG
    # ------------------------------------------------------------------
    def create_workflow(self, species, year_from, year_to, max_records):
        self.wf = Workflow("species-occurrence", infer_dependencies=True)
        stats_files = []

        for name in species:
            s = slug(name)
            occurrences = File(f"{s}_occurrences.csv")
            match = File(f"{s}_match.json")
            stats = File(f"{s}_stats.json")
            figure = File(f"{s}_map.png")

            fetch = Job("fetch_occurrences", _id=f"fetch_{s}", node_label=f"fetch_{s}")
            # Pegasus argument lists are whitespace separated, so a
            # two-word species name must not be passed as a bare space.
            fetch.add_args("--species", name.replace(" ", "_"),
                           "--year-from", str(year_from),
                           "--year-to", str(year_to),
                           "--max-records", str(max_records),
                           "--output", occurrences,
                           "--match-output", match)
            # SPEC C7: raw records stay on the execution side
            fetch.add_outputs(occurrences, match, stage_out=False,
                              register_replica=False)
            fetch.add_dagman_profile(retry="2")   # SPEC C5
            self.wf.add_jobs(fetch)

            summarise = Job("summarise_species", _id=f"summarise_{s}",
                            node_label=f"summarise_{s}")
            summarise.add_args("--input", occurrences,
                               "--match", match,
                               "--species", name.replace(" ", "_"),
                               "--year-from", str(year_from),
                               "--year-to", str(year_to),
                               "--max-records", str(max_records),
                               "--stats-output", stats,
                               "--figure-output", figure)
            summarise.add_inputs(occurrences, match)
            summarise.add_outputs(stats, figure, stage_out=True,
                                  register_replica=False)
            self.wf.add_jobs(summarise)

            stats_files.append(stats)

        table = File("comparison.csv")
        chart = File("comparison.png")
        merge = Job("merge_species", _id="merge_all", node_label="merge_all")
        merge.add_args("--inputs", *stats_files,
                       "--table-output", table, "--figure-output", chart)
        merge.add_inputs(*stats_files)
        merge.add_outputs(table, chart, stage_out=True, register_replica=False)
        self.wf.add_jobs(merge)



def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s
  %(prog)s --species "Danaus plexippus" "Apis mellifera" -s access-pegasus.yml
  %(prog)s -e condorpool     # plain HTCondor pool, no site catalog

Writes the workflow and its catalogs; it does not plan or submit. Plan with
the command it prints, or from the notebook (plan_submit()).
""")
    ap.add_argument("--species", nargs="+", default=DEFAULT_SPECIES)
    ap.add_argument("--year-from", type=int, default=DEFAULT_YEAR_FROM)
    ap.add_argument("--year-to", type=int, default=DEFAULT_YEAR_TO)
    ap.add_argument("--max-records", type=int, default=900)
    ap.add_argument("--container-sif",
                    default="Apptainer/Species_Container.sif")
    ap.add_argument(
        "-s", "--hosted-site-catalog", metavar="FILE", type=str, default=None,
        help="Name of a Pegasus centrally hosted site catalog to plan against "
        "(e.g. access-pegasus.yml), instead of a locally generated one. Sets "
        "pegasus.catalog.site.repo.file; see "
        "https://pegasus.isi.edu/documentation/reference-guide/catalogs.html"
        "#centrally-hosted-site-catalogs")
    ap.add_argument(
        "-e", "--execution-site-name", metavar="STR", type=str,
        default="compute", help="Execution site name (default: compute)")
    ap.add_argument("-o", "--output", default="workflow.yml",
                    help="Output file (default: workflow.yml)")
    args = ap.parse_args()

    if args.year_from > args.year_to:
        sys.exit("--year-from must not be after --year-to")

    wf = SpeciesOccurrenceWorkflow(args.output, args.container_sif)
    wf.create_pegasus_properties(hosted_site_catalog=args.hosted_site_catalog)
    wf.create_transformation_catalog(exec_site_name=args.execution_site_name)
    wf.create_replica_catalog()
    wf.create_workflow(args.species, args.year_from, args.year_to,
                       args.max_records)
    wf.write()

    log.info("species: %s", ", ".join(args.species))
    log.info("years: %d-%d, max %d records each", args.year_from, args.year_to,
             args.max_records)
    log.info("execution site: %s", args.execution_site_name)
    log.info("hosted site catalog: %s", args.hosted_site_catalog
             or "(none — supply your own site catalog)")
    log.info("wrote %s (%d jobs)", args.output, 2 * len(args.species) + 1)
    print(f"\nPlan and submit:\n  pegasus-plan --dir submit "
          f"-s {args.execution_site_name} -o local --submit {args.output}")


if __name__ == "__main__":
    main()
