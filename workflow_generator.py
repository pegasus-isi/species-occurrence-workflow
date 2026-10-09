#!/usr/bin/env python3
"""Pegasus workflow generator for the species occurrence comparison (see SPEC.md).

Shape (SPEC §3):

    for each species:  fetch -> summarise
    all species:       merge

Two jobs per species plus one merge.

Sites: the workflow names no scheduler. Jobs state cores, memory and a
wall-clock runtime, and plan against a site named "compute". A hosted site
catalog (-s unity.yml, ...) defines it; otherwise custom_sites.py writes it to
sites.yml as an HTCondor pool, or for Slurm via --site-style slurm --queue
--project.
"""
import argparse
import logging
import os
import re
import sys
from pathlib import Path

from Pegasus.api import (OS, Arch, Container, File, Job, Properties,
                         ReplicaCatalog, Transformation, TransformationCatalog,
                         Workflow)

# Site-catalog handling. Keeps this generator free of scheduler details.
sys.path.insert(0, str(Path(__file__).parent.resolve()))
from custom_sites import (  # noqa: E402
    HOSTED_SITE, STYLES, ensure_sites_yml, hosted_catalog, is_batch_site,
    parse_profile, parse_tag_profile, worker_package_url,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("generator")

DEFAULT_SPECIES = ["Danaus plexippus", "Vanessa cardui", "Apis mellifera"]
DEFAULT_YEAR_FROM = 2015
DEFAULT_YEAR_TO = 2024

# Per-tool resources — the only resource statements the workflow makes,
# portable on any site. runtime is a wall-clock budget in seconds; batch sites
# (Slurm) refuse or kill jobs without one, so keep it generous.
TOOL_CONFIGS = {
    "fetch_occurrences": {"memory": "1 GB", "runtime": 1800},
    "summarise_species": {"memory": "2 GB", "runtime": 1800},
    "merge_species":     {"memory": "2 GB", "runtime": 1800},
}

# The container's OS, for the Pegasus worker package staged into it. Must
# match the image's base, not the submit host: python:3.11-slim currently
# resolves to Debian 13 (trixie).
CONTAINER_PLATFORM = "x86_64_deb_13"


def slug(name):
    """A filesystem-safe stem derived from the requested name (SPEC §8, issue 1)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()


class SpeciesOccurrenceWorkflow:
    def __init__(self, dagfile="workflow.yml", container_sif=None):
        self.dagfile = dagfile
        self.wf_dir = str(Path(__file__).parent.resolve())
        self.scratch = os.path.join(self.wf_dir, "scratch")
        self.storage = os.path.join(self.wf_dir, "output")
        self.container_sif = container_sif
        # Set by main() from the site in use (see the site-catalog block there).
        self.worker_package_url = None
        self.bind_workflow_dir = False

    def properties(self, sites_yml="sites.yml", bypass_input_staging=False,
                   hosted_site_catalog=None):
        # Data staging (condorio on a condor pool, nonsharedfs on Slurm) comes
        # from the site entry custom_sites.py writes, not from here.
        self.props = Properties()
        if hosted_site_catalog:
            # A centrally hosted site catalog (pegasushub
            # pegasus-site-catalogs) defines the execution site; pegasus-plan
            # downloads and caches it, and merges sites.yml over it.
            self.props["pegasus.catalog.site.repo.file"] = hosted_site_catalog
        # Symlink rather than copy when an input already sits on the
        # execution site. A no-op otherwise, so always on.
        self.props["pegasus.transfer.links"] = "true"
        if bypass_input_staging:
            # Jobs read inputs (notably the .sif) straight from the submit
            # host's paths. Only valid where workers share a filesystem with
            # the submit host (a Slurm cluster) — never on a condor pool.
            self.props["pegasus.transfer.bypass.input.staging"] = "true"
        if self.worker_package_url:
            # Stage the container-compatible worker package named in the TC
            # (pegasus::worker) and never download one from inside a job.
            self.props["pegasus.transfer.worker.package"] = "true"
            self.props["pegasus.transfer.worker.package.strict"] = "false"
            self.props["pegasus.transfer.worker.package.autodownload"] = "false"
        if os.path.isfile(sites_yml):
            # Lets pegasus-plan find sites.yml from any directory.
            self.props["pegasus.catalog.site"] = "YAML"
            self.props["pegasus.catalog.site.file"] = os.path.abspath(sites_yml)

    def transformations(self):
        """Nothing here names a site: the scripts and the .sif live on
        "local" (the submit host) and are shipped to wherever the job runs."""
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
            if self.bind_workflow_dir:
                # Batch sites stage inputs as symlinks into the workflow
                # directory, and PegasusLite starts containers with
                # --no-home, so without this bind every job fails with exit
                # 127. Never on a condor pool: the directory does not exist
                # on its workers.
                container.add_pegasus_profile(
                    container_arguments=f"--bind {self.wf_dir}")
            # The container must be registered in the catalog, not merely
            # referenced by the transformations that use it.
            self.tc.add_containers(container)

        for name, cfg in TOOL_CONFIGS.items():
            t = Transformation(name, site="local",
                               pfn=os.path.join(self.wf_dir, "bin", name + ".py"),
                               is_stageable=True, container=container)
            t.add_pegasus_profile(memory=cfg["memory"], runtime=cfg["runtime"])
            self.tc.add_transformations(t)

        if self.worker_package_url:
            self.tc.add_transformations(
                Transformation("worker", namespace="pegasus", site="local",
                               pfn=self.worker_package_url, is_stageable=True,
                               arch=Arch.X86_64, os_type=OS.LINUX))

    def build(self, species, year_from, year_to, max_records):
        self.wf = Workflow("species-occurrence", infer_dependencies=True)
        self.rc = ReplicaCatalog()
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

    def write(self):
        self.props.write()
        self.rc.write()
        self.tc.write()
        self.wf.write(file=self.dagfile)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--species", nargs="+", default=DEFAULT_SPECIES)
    ap.add_argument("--year-from", type=int, default=DEFAULT_YEAR_FROM)
    ap.add_argument("--year-to", type=int, default=DEFAULT_YEAR_TO)
    ap.add_argument("--max-records", type=int, default=900)
    # Site options. Every one has a default, so a run with no arguments plans
    # on "compute": the hosted catalog's site when one is named (-s or
    # ~/.pegasusrc), else an HTCondor pool written to sites.yml. dest
    # "execution_site" is what Studio matches to keep its "Where it runs"
    # choice in sync.
    ap.add_argument("-e", "--execution-site", "--execution-site-name",
                    dest="execution_site", default=HOSTED_SITE,
                    help=f"site to plan against (default: {HOSTED_SITE!r}, "
                         "the name hosted catalogs give their site)")
    ap.add_argument("-s", "--hosted-site-catalog", metavar="FILE",
                    help="centrally hosted site catalog to plan against, e.g. "
                         "unity.yml (github.com/pegasushub/"
                         "pegasus-site-catalogs); written to "
                         "pegasus.properties. Default: the one named in "
                         "~/.pegasusrc, if any.")
    ap.add_argument("--site-style", choices=("auto",) + STYLES + ("none",),
                    default="auto",
                    help="auto (default): keep a sites.yml entry or hosted "
                         "catalog if one exists, else add an HTCondor site. "
                         "condor/slurm: (re)write this site's entry. none: "
                         "leave sites.yml alone.")
    ap.add_argument("--queue", metavar="PARTITION",
                    help="batch partition/queue jobs submit to")
    ap.add_argument("--project", metavar="ACCOUNT",
                    help="allocation/account charged on a batch site")
    ap.add_argument("--site-scratch", metavar="DIR",
                    help="Slurm: shared scratch visible to workers and the "
                         "submit host (default: ./work)")
    ap.add_argument("--site-profile", action="append", default=[],
                    type=parse_profile, metavar="NS:KEY=VALUE",
                    help="extra profile on the execution site; repeatable")
    ap.add_argument("--tag-profile", action="append", default=[],
                    type=parse_tag_profile, metavar="TAG:NS:KEY=VALUE",
                    help="profile for jobs carrying a tag; repeatable")
    ap.add_argument("--shared-filesystem", choices=("auto", "yes", "no"),
                    default="auto",
                    help="let jobs read inputs directly from the submit host. "
                         "auto: on for Slurm sites, off for HTCondor.")
    ap.add_argument("--sites-yml", metavar="FILE", default="sites.yml",
                    help="local site catalog (default: sites.yml)")
    ap.add_argument("--skip-sites-catalog", action="store_true",
                    help="deprecated: same as --site-style none")
    ap.add_argument("--container-sif",
                    default="Apptainer/Species_Container.sif")
    ap.add_argument("-o", "--output", default="workflow.yml")
    args = ap.parse_args()

    if args.year_from > args.year_to:
        sys.exit("--year-from must not be after --year-to")

    wf = SpeciesOccurrenceWorkflow(args.output, args.container_sif)

    # Site catalog and the settings that depend on the site
    if args.skip_sites_catalog:
        args.site_style = "none"
    action, style = ensure_sites_yml(
        args.sites_yml, args.execution_site, wf.wf_dir,
        style=args.site_style, hosted=args.hosted_site_catalog,
        queue=args.queue, project=args.project, scratch=args.site_scratch,
        profiles=args.site_profile, tag_profiles=args.tag_profile)
    hosted = hosted_catalog(args.hosted_site_catalog)
    log.info("site catalog: %s: %s%s", args.sites_yml, action,
             f" (merged over hosted {hosted})" if hosted else "")
    if (style is None and hosted and args.execution_site != "local"
            and args.execution_site != HOSTED_SITE):
        # Nothing was written for this site, so planning works only if the
        # hosted catalog happens to define it.
        log.warning("%r is not defined in %s and hosted catalogs normally "
                    "define only %r: pegasus-plan will fail unless %s has it. "
                    "Use -e %s, or --site-style condor/slurm to describe %r.",
                    args.execution_site, args.sites_yml, HOSTED_SITE, hosted,
                    HOSTED_SITE, args.execution_site)
    if args.shared_filesystem == "auto":
        bypass = style is not None and style != "condor"
    else:
        bypass = args.shared_filesystem == "yes"
    wf.bind_workflow_dir = bypass or is_batch_site(style,
                                                   args.hosted_site_catalog)
    wf.worker_package_url = worker_package_url(CONTAINER_PLATFORM)
    if not wf.worker_package_url:
        log.warning("pegasus-version not found: Pegasus will choose the "
                    "container's worker package itself (needs curl/wget in "
                    "the image and internet on the workers)")

    wf.properties(sites_yml=args.sites_yml, bypass_input_staging=bypass,
                  hosted_site_catalog=args.hosted_site_catalog)
    wf.transformations()
    wf.build(args.species, args.year_from, args.year_to, args.max_records)
    wf.write()

    log.info("species: %s", ", ".join(args.species))
    log.info("years: %d-%d, max %d records each", args.year_from, args.year_to,
             args.max_records)
    log.info("wrote %s (%d jobs)", args.output, 2 * len(args.species) + 1)
    print(f"\nSubmit with:\n  pegasus-plan --submit -s {args.execution_site} "
          f"-o local {args.output}")


if __name__ == "__main__":
    main()
