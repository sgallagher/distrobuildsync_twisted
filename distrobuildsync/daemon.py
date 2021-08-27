import argparse
import fedora_messaging.api
import fedora_messaging.config
import logging
import re
import sys

from . import config
from . import listener
from . import kojihelpers

from twisted.internet import reactor, task


logger = config.logger

# Matching the namespace/component text format
cre = re.compile(
    r"^(?P<namespace>rpms|modules)/(?P<component>[A-Za-z0-9:._+-]+)$"
)


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("config", help="configuration repository SCMURL")
    ap.add_argument(
        "-l",
        "--loglevel",
        dest="loglevel",
        help="logging level; default: info",
        default="INFO",
    )
    ap.add_argument(
        "-u",
        "--update",
        dest="update",
        type=int,
        help="configuration refresh interval in minutes; default: 5",
        default=5,
    )
    ap.add_argument(
        "-r",
        "--retry",
        dest="retry",
        type=int,
        help="number of retries on network failures; default: 3",
        default=3,
    )
    ap.add_argument(
        "-1",
        "--oneshot",
        action="store_true",
        help="sync all components and exit",
        default=False,
    )
    ap.add_argument(
        "-d",
        "-n",
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="do not upload, push or build anything",
        default=False,
    )
    ap.add_argument(
        "-s",
        "--select",
        dest="select",
        help="space-separated list of configured components to sync in the ns/component form; defaults to all",
    )
    ap.add_argument(
        "--distrogitsync-endpoint",
        dest="distrogitsync",
        help="API endpoint for distrogitsync to trigger the sync of git repositories (eg. http://distrogitsync:8080/)",
    )

    args = ap.parse_args()
    if args.select and not args.oneshot:
        print("Selecting components only works with oneshot mode.")
        sys.exit(1)

    loglevel = getattr(logging, args.loglevel.upper())
    if not isinstance(loglevel, int):
        print("Invalid loglevel: {}".format(args.loglevel))
        sys.exit(1)

    return args


def oneshot(compset):
    """Processes the supplied set of components.  If the set is empty,
    fetch all latest components from the trigger tags.

    :param compset: A set of components to process in the `ns/comp` form
    :returns: None
    """
    if not config.main:
        logger.critical("DistroBuildSync is not configured, aborting.")
        return None

    if not compset:
        logger.debug(
            "No components selected, gathering components from triggers."
        )
        compset.update(
            "{}/{}".format("rpms", x["package_name"])
            for x in kojihelpers.get_buildsys("source").listTagged(
                config.main["trigger"]["rpms"], latest=True
            )
        )

    logger.info("Processing %d component(s).", len(compset))
    rd_list = []
    for rec in sorted(compset, key=str.lower):
        m = cre.match(rec)
        if m is None:
            logger.error("Cannot process %s; looks like garbage.", rec)
            continue
        m = m.groupdict()
        logger.info("Processing %s.", rec)

        if m["component"] in config.main["control"]["exclude"][m["namespace"]]:
            logger.info(
                "The %s/%s component is excluded from sync, skipping.",
                m["namespace"],
                m["component"],
            )
            continue

        if (
            config.main["control"]["strict"]
            and m["component"] not in config.comps[m["namespace"]]
        ):
            logger.info(
                "The %s/%s component not configured while the strict mode is enabled, ignoring.",
                m["namespace"],
                m["component"],
            )
            continue

        namespace = m["namespace"]
        component = m["component"]
        nvr = kojihelpers.get_build(component, namespace)
        if not nvr:
            logger.info("The {namespace}/{component} component's build not tagged in the source Koji tag.")
            continue

        bi = kojihelpers.get_build_info(nvr)
        scmurl = bi["scmurl"]
        ref = config.split_scmurl(scmurl)["ref"]
        if ref:
            if namespace == "modules":
                ref_overrides = kojihelpers.get_ref_overrides(bi["modulemd"])
            else:
                ref_overrides = None

        rd_list.append(listener.RebuildData(namespace, component, None, None, scmurl, None, ref_overrides))
        logger.debug("Scheduled {namespace}/{component} for rebuild")

    # Fire off the builds
    listener.build_components(None, rd_list)

    rd_list_len = len(rd_list)
    skipped = len(compset) - rd_list_len
    logger.info(f"Synchronized {rd_list_len} component(s), {skipped} skipped.")


def main():
    logging.basicConfig(format="%(asctime)s : %(levelname)s : %(message)s")
    args = parse_args()
    loglevel = getattr(logging, args.loglevel.upper())
    logger.setLevel(loglevel)

    config.config_timer = args.update * 60
    config.retries = args.retry
    config.dry_run = args.dry_run
    config.distrogitsync = args.distrogitsync
    config.scmurl = args.config

    # Read in the config file
    if not config.load_config():
        logger.critical("Could not load configuration.")
        sys.exit(128)

    if args.oneshot:
        return oneshot(set([i for i in args.select.split(" ") if i]) if args.select else set())

    # Schedule configuration updates
    updater = task.LoopingCall(config.update_config)
    updater.start(config.config_timer, now=False)

    # Schedule batch checking
    config.batch_processor = task.LoopingCall(listener.process_batch)
    config.batch_processor.start(config.batch_timer, now=False)

    # Start listening for Fedora Messages
    fedora_messaging.api.twisted_consume(listener.process_message)


    logger.debug("Starting Twisted mainloop")
    reactor.run()


if __name__ == "__main__":
    main()
