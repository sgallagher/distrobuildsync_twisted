import argparse
import fedora_messaging.api
import logging
import sys

from . import config
from . import listener

from twisted.internet import reactor, task


logger = config.logger


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


def oneshot(packages):
    raise NotImplementedError("Oneshot mode not yet implemented")


def main():
    logging.basicConfig(format="%(asctime)s : %(levelname)s : %(message)s")
    args = parse_args()
    loglevel = getattr(logging, args.loglevel.upper())
    logger.setLevel(loglevel)

    config.config_timer = args.update  # * 60
    config.retries = args.retry
    config.dry_run = args.dry_run
    config.distrogitsync = args.distrogitsync
    config.scmurl = args.config

    # Read in the config file
    if not config.load_config():
        logger.critical("Could not load configuration.")
        sys.exit(128)

    if args.oneshot:
        # TODO: Special handling for oneshot mode
        return oneshot(set([i for i in args.select.split(" ") if i]) if args.select else set())

    # Schedule configuration updates
    updater = task.LoopingCall(config.update_config)
    updater.start(config.config_timer, now=False)

    # Start listening for Fedora Messages
    fedora_messaging.api.twisted_consume(listener.process_message)


    logger.debug("Starting Twisted mainloop")
    reactor.run()


if __name__ == "__main__":
    main()
