#!/usr/bin/env python3

"""Write the site-specific half of a workflow run: sites.yml.

Copy this file next to workflow_generator.py. The generator imports it; it
also runs standalone.

The workflow itself never names a scheduler. Its jobs state cores, memory and
a wall-clock runtime, and jobs with unusual needs carry a Pegasus tag
(e.g. "gpu", "train"). What the execution site is (an HTCondor pool, a Slurm
cluster reached through glite), which partition and account jobs land on,
where scratch lives, and what each tag means there is the site catalog's
business, and this module writes that catalog.

Precedence, most specific first:

  1. A sites.yml entry for the site that someone wrote (by hand or with this
     script) — kept as-is.
  2. A hosted catalog (from github.com/pegasushub/pegasus-site-catalogs),
     which Pegasus merges a local sites.yml over, key by key. The generator
     names it with -s/--hosted-site-catalog FILE, which it writes into the
     workflow's pegasus.properties (pegasus.catalog.site.repo.file); without
     -s, one set in ~/.pegasusrc is used.
  3. A default written here: an HTCondor site, so the workflow plans with no
     setup at all (Pegasus Studio's default).

The execution site is called "compute" in every case — the hosted catalogs'
name for their one site — so "pegasus-plan -s compute" works whether the site
comes from a hosted catalog or is generated here.

Only the requested site's entry is ever written; other sites in sites.yml are
left alone. A "local" site (submit-host scratch and output storage under the
workflow directory) is always ensured, because planning with "-o local" and
Pegasus Studio's output discovery need it.

Standalone use:

    # A cluster with a hosted catalog (e.g. Unity) — add your account:
    ./custom_sites.py --style slurm --site compute --project my_lab

    # A local HTCondor pool with no hosted catalog:
    ./custom_sites.py --style condor

    # A Slurm cluster with no hosted catalog, GPU jobs on their own partition:
    ./custom_sites.py --style slurm --site compute --queue cpu --project my_lab \\
        --scratch /scratch/$USER/wf --tag-profile gpu:pegasus:queue=gpu

Requires the Pegasus Python API and PyYAML. Tags need Pegasus >= 5.1.3dev
(6.0 recommended) at plan time; this module writes them with any API version.
"""

import argparse
import io
import os
import subprocess
from pathlib import Path

import yaml
from Pegasus.api import (
    Directory, FileServer, Namespace, Operation, Site, SiteCatalog,
)

STYLES = ("condor", "slurm")

# The one site hosted catalogs (pegasushub/pegasus-site-catalogs) define, and
# the name workflows plan against by default, hosted catalog or not.
HOSTED_SITE = "compute"

NAMESPACES = {ns.value: ns for ns in Namespace}


# ----------------------------------------------------------------------
# Parsing helpers (shared with workflow_generator.py's CLI)
# ----------------------------------------------------------------------
def parse_profile(text):
    """'ns:key=value' or 'key=value' (pegasus namespace) -> (ns, key, value)."""
    key, sep, value = text.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError(
            f"{text!r}: expected NS:KEY=VALUE or KEY=VALUE")
    ns, colon, bare = key.partition(":")
    if not colon:
        ns, bare = "pegasus", key
    if ns not in NAMESPACES:
        raise argparse.ArgumentTypeError(
            f"{text!r}: unknown namespace {ns!r}; one of "
            f"{', '.join(sorted(NAMESPACES))}")
    return ns, bare, value


def parse_tag_profile(text):
    """'tag:ns:key=value' -> (tag, ns, key, value)."""
    tag, colon, rest = text.partition(":")
    if not colon or not tag:
        raise argparse.ArgumentTypeError(
            f"{text!r}: expected TAG:NS:KEY=VALUE, e.g. gpu:pegasus:queue=gpu")
    return (tag,) + parse_profile(rest)


# ----------------------------------------------------------------------
# Reading what already exists
# ----------------------------------------------------------------------
def hosted_catalog(name=None, pegasusrc=None):
    """The hosted catalog in use, or None.

    `name` is the one the workflow names itself (the generator's
    -s/--hosted-site-catalog) and wins; otherwise the one in ~/.pegasusrc.
    """
    if name:
        return name
    rc = Path(pegasusrc) if pegasusrc else Path.home() / ".pegasusrc"
    name = None
    try:
        for line in rc.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "pegasus.catalog.site.repo.file":
                name = value.strip()
    except OSError:
        return None
    return name or None


def load_sites_yml(path):
    """(document, {site name: entry}) from a sites.yml; (None, {}) if absent."""
    if not os.path.isfile(path):
        return None, {}
    with open(path) as fh:
        doc = yaml.safe_load(fh) or {}
    return doc, {s.get("name"): s for s in doc.get("sites") or []}


def site_style(entry):
    """'condor', 'slurm', another 'batch:<lrms>', or None if not stated."""
    profiles = (entry or {}).get("profiles") or {}
    style = (profiles.get("pegasus") or {}).get("style")
    grid = str((profiles.get("condor") or {}).get("grid_resource") or "")
    if style == "glite" or grid.startswith("batch"):
        lrms = grid.split()[1] if len(grid.split()) > 1 else "unknown"
        return "slurm" if lrms == "slurm" else f"batch:{lrms}"
    if style == "condor":
        return "condor"
    return None


def _hosted_style(path, hosted, site_name):
    """The site's style per the hosted catalog, or None if unknown.

    The planner leaves a copy of the hosted file in the directory it ran
    in; without that copy (first run) the style cannot be read.
    """
    if not hosted:
        return None
    _, entries = load_sites_yml(
        os.path.join(os.path.dirname(os.path.abspath(path)), hosted))
    return site_style(entries.get(site_name))


def _hosted_defines(path, hosted, site_name):
    """Whether the hosted catalog defines site_name.

    Read from the planner's copy of the hosted file when one is at hand;
    before the first plan, assume only the hosted convention, HOSTED_SITE.
    """
    copy = os.path.join(os.path.dirname(os.path.abspath(path)), hosted)
    if os.path.isfile(copy):
        return site_name in load_sites_yml(copy)[1]
    return site_name == HOSTED_SITE


def is_batch_site(style, hosted=None):
    """True if jobs stage through the site's own filesystem (not condorio).

    An unknown style over a hosted catalog counts: hosted catalogs describe
    batch clusters. On such a site pegasus.transfer.links stages inputs as
    symlinks into the workflow directory, so containers must bind it.
    `hosted` is the catalog the workflow names, as for hosted_catalog().
    """
    if style is None:
        return hosted_catalog(hosted) is not None
    return style != "condor"


# ----------------------------------------------------------------------
# Container support: the Pegasus worker package inside the container
# ----------------------------------------------------------------------
def planner_version():
    """Version of the pegasus-plan that will plan this workflow, or None."""
    try:
        out = subprocess.run(["pegasus-version"], capture_output=True,
                             text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    version = out.stdout.strip()
    return version if out.returncode == 0 and version else None


def worker_package_url(platform, version=None):
    """Download URL of the worker package for `platform`, or None.

    platform is the *container's* OS, e.g. "x86_64_deb_12" or "x86_64_rhel_8"
    (list: https://download.pegasus.isi.edu/pegasus/<version>/). Pegasus only
    publishes packages for current distributions; for an older container
    pick the oldest-glibc one that runs there (rhel_8 = glibc 2.28 covers
    Debian 11 and newer).
    """
    version = version or planner_version()
    if not version:
        return None
    return (f"https://download.pegasus.isi.edu/pegasus/{version}/"
            f"pegasus-worker-{version}-{platform}.tar.gz")


# ----------------------------------------------------------------------
# Building site entries
# ----------------------------------------------------------------------
def build_compute_site(name, style, full=True, queue=None, project=None,
                       scratch=None, storage=None, profiles=(),
                       tag_profiles=()):
    """A Site for `name`. full=False writes only the overrides (an overlay).

    tag_profiles: iterable of (tag, ns, key, value).
    Raises ValueError for combinations that cannot work.
    """
    if (scratch or storage) and not (full and style == "slurm"):
        raise ValueError("scratch/storage only apply to a full slurm site; a "
                         "condor pool stages through the submit host")
    site = Site(name)
    if full:
        if style == "slurm":
            if not queue:
                raise ValueError("a full slurm site needs a queue (the "
                                 "partition jobs submit to)")
            scratch = os.path.abspath(scratch or "work")
            storage = os.path.abspath(storage or "storage")
            site.add_directories(
                Directory(Directory.SHARED_SCRATCH, scratch,
                          shared_file_system=False)
                .add_file_servers(FileServer("file://" + scratch,
                                             Operation.ALL)),
                Directory(Directory.LOCAL_STORAGE, storage,
                          shared_file_system=False)
                .add_file_servers(FileServer("file://" + storage,
                                             Operation.ALL)),
            )
            site.add_condor_profile(grid_resource="batch slurm")
            site.add_pegasus_profile(style="glite",
                                     data_configuration="nonsharedfs",
                                     auxillary_local="true")
        else:
            site.add_condor_profile(universe="vanilla")
            site.add_pegasus_profile(style="condor")
    else:
        # An overlay still states how the site submits — the same values the
        # hosted entry carries, so merging changes nothing — so later runs
        # can tell (bypass staging, container binds) without the hosted file.
        if style == "slurm":
            site.add_condor_profile(grid_resource="batch slurm")
            site.add_pegasus_profile(style="glite")
        else:
            site.add_pegasus_profile(style="condor")
    if queue:
        site.add_pegasus_profile(queue=queue)
    if project:
        site.add_pegasus_profile(project=project)
    for ns, key, value in profiles:
        site.add_profiles(NAMESPACES[ns], key=key, value=value)
    # Tag profiles are written by hand (write_sites), not with
    # Site.add_tag_profiles, which only exists in Pegasus >= 5.1.3dev's API.
    site.x_tags = {}
    for tag, ns, key, value in tag_profiles:
        site.x_tags.setdefault(tag, {}).setdefault(ns, {})[key] = value
    return site


def build_local_site(wf_dir):
    """Submit-host scratch and output storage under the workflow directory."""
    scratch = os.path.join(wf_dir, "scratch")
    storage = os.path.join(wf_dir, "output")
    return Site("local").add_directories(
        Directory(Directory.SHARED_SCRATCH, scratch)
        .add_file_servers(FileServer("file://" + scratch, Operation.ALL)),
        Directory(Directory.LOCAL_STORAGE, storage)
        .add_file_servers(FileServer("file://" + storage, Operation.ALL)),
    )


def _site_dicts(*sites):
    """Pegasus Site objects -> the plain dicts a sites.yml holds."""
    sc = SiteCatalog()
    sc.add_sites(*sites)
    buf = io.StringIO()
    sc.write(buf, _format="yml")
    return yaml.safe_load(buf.getvalue())


def write_sites(path, sites, base_doc=None):
    """Write `sites` into path, replacing same-named entries, keeping others."""
    new = _site_dicts(*sites)
    for site, entry in zip(sites, new["sites"]):
        tags = getattr(site, "x_tags", None)
        if tags:
            entry["x-tags"] = [{"name": name, "profiles": profiles}
                               for name, profiles in tags.items()]
    doc = base_doc or new
    by_name = {s["name"]: s for s in new["sites"]}
    kept = [s for s in doc.get("sites") or [] if s.get("name") not in by_name]
    doc["sites"] = kept + new["sites"]
    doc.setdefault("pegasus", new.get("pegasus"))
    with open(path, "w") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)


def ensure_sites_yml(path, site_name, wf_dir, style="auto", hosted=None,
                     **site_opts):
    """Make sure planning against `site_name` works; return (action, style).

    style "auto" keeps anything already provided and only fills gaps;
    "condor"/"slurm" (re)write the site_name entry; "none" writes nothing.
    `hosted` is the hosted catalog the workflow names (-s), if any; without
    it, one in ~/.pegasusrc counts.
    `action` says what happened, for the caller to report; the returned style
    is the site's effective one ("condor", "slurm", ... or None if unknown).
    """
    doc, entries = load_sites_yml(path)
    hosted = hosted_catalog(hosted)
    if style == "none":
        return "untouched (--site-style none)", (
            site_style(entries.get(site_name))
            or _hosted_style(path, hosted, site_name))

    to_write = []
    if "local" not in entries:
        to_write.append(build_local_site(wf_dir))

    if style in STYLES:
        if site_name == "local":
            raise ValueError("--site-style describes the execution site; it "
                             "cannot be applied to 'local'")
        # Over a hosted catalog the hosted entry already carries the
        # scheduler settings, so write only the overrides — but only for a
        # site the hosted catalog defines. Any other site (e.g. a site named
        # "condorpool" next to a hosted "compute") has nothing to overlay and
        # needs a complete entry, or the planner gets a site with no submit
        # setup.
        full = site_opts.pop("full", None)
        if full is None:
            full = not (hosted and _hosted_defines(path, hosted, site_name))
        found = _hosted_style(path, hosted, site_name)
        if not full and found not in (None, style):
            raise ValueError(f"--site-style {style} contradicts hosted "
                             f"catalog {hosted}, where {site_name!r} is "
                             f"{found}")
        to_write.append(build_compute_site(site_name, style, full=full,
                                           **site_opts))
        action = (f"wrote {style} site {site_name!r}"
                  + ("" if full else " (overlay)"))
        effective = style
    elif site_name in entries or site_name == "local":
        action = f"using existing site {site_name!r}"
        # An overlay on a hosted catalog may state no style of its own.
        effective = (site_style(entries.get(site_name))
                     or _hosted_style(path, hosted, site_name))
    elif hosted:
        action = f"site {site_name!r} expected from hosted catalog {hosted}"
        effective = _hosted_style(path, hosted, site_name)
    else:
        site_opts.pop("full", None)
        to_write.append(build_compute_site(site_name, "condor", full=True,
                                           **site_opts))
        action = f"added default condor site {site_name!r}"
        effective = "condor"

    if to_write:
        write_sites(path, to_write, base_doc=doc)
    return action, effective


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n", 1)[1])
    parser.add_argument("--style", choices=STYLES, required=True,
                        help="how the compute site is submitted to: an "
                             "HTCondor pool, or Slurm through glite")
    parser.add_argument("--site", default="compute",
                        help="compute site name (default: compute, the "
                             "hosted catalogs' convention)")
    parser.add_argument("--full", action="store_true", default=None,
                        help="write a complete site rather than an overlay "
                             "(default: complete unless the hosted catalog "
                             "defines the site)")
    parser.add_argument("--hosted-site-catalog", metavar="FILE",
                        help="hosted catalog the site is overlaid on, e.g. "
                             "unity.yml (default: the one named in "
                             "~/.pegasusrc, if any)")
    parser.add_argument("--scratch", metavar="DIR",
                        help="full slurm site: shared scratch the workers and "
                             "submit host both see (default: $PWD/work)")
    parser.add_argument("--storage", metavar="DIR",
                        help="full slurm site: the site's output storage "
                             "(default: $PWD/storage)")
    parser.add_argument("--queue", metavar="Q",
                        help="partition/queue jobs submit to (pegasus.queue)")
    parser.add_argument("--project", metavar="ACCOUNT",
                        help="allocation charged (pegasus.project; "
                             "--account on Slurm)")
    parser.add_argument("--profile", action="append", default=[],
                        type=parse_profile, metavar="NS:KEY=VALUE",
                        help="extra profile on the site, e.g. "
                             "pegasus:glite.arguments=--constraint=avx512; "
                             "repeatable")
    parser.add_argument("--tag-profile", action="append", default=[],
                        type=parse_tag_profile, metavar="TAG:NS:KEY=VALUE",
                        help="profile for jobs carrying a tag, e.g. "
                             "gpu:pegasus:queue=gpu; repeatable")
    parser.add_argument("-o", "--output", default="sites.yml",
                        help="where to write (default: sites.yml). Other "
                             "sites in it are kept.")
    args = parser.parse_args()

    try:
        action, _ = ensure_sites_yml(
            args.output, args.site,
            wf_dir=os.path.dirname(os.path.abspath(__file__)),
            style=args.style, hosted=args.hosted_site_catalog,
            full=args.full, queue=args.queue,
            project=args.project, scratch=args.scratch, storage=args.storage,
            profiles=args.profile, tag_profiles=args.tag_profile)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"{args.output}: {action}")


if __name__ == "__main__":
    main()
