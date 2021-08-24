from . import config
from . import kojihelpers

from collections import defaultdict
from twisted.internet import reactor, task
from twisted.internet.defer import AlreadyCalledError, inlineCallbacks
from queue import Empty

logger = config.logger


def process_message(msg):
    logger.debug("Received a message with topic %s.", msg.topic)

    # Listen for repositories we are waiting on.
    if msg.topic.endswith("buildsys.repo.done"):
        tag = msg.body["tag"]
        if tag in config.awaited_repos:
            for deferred in config.awaited_repos[tag]:
                logger.info(f"Repo {tag} has regenerated")
                try:
                    deferred.callback(None)
                except AlreadyCalledError:
                    # Most likely due to a timeout, so ignore it
                    pass
            # Clear the awaited list
            del config.awaited_repos[tag]

    if not msg.topic.endswith("buildsys.tag"):
        # Ignore any non-tagging messages
        logger.debug("Unable to handle %s topics, ignoring.", msg.topic)
        return

    reactor.callFromThread(config.batch_processor.reset)
    reactor.callFromThread(config.message_queue.put, msg)


def process_batch():
    batch = []
    while True:
        try:
            batch.append(config.message_queue.get_nowait())
        except Empty as e:
            break

    if not batch:
        return

    # Return to the mainloop so we don't block future batches
    reactor.callLater(0, split_batch, batch)


def split_batch(batch):
    # The targets may differ
    # Get the build tags associated with the targets
    targets = defaultdict(list)
    for msg in batch:
        comp = msg.body["name"]
        nvr = "{}-{}-{}".format(
            msg.body["name"], msg.body["version"], msg.body["release"]
        )
        tag = msg.body["tag"]
        scmurl = kojihelpers.get_scmurl(msg.body["build_id"])

        upstream_build_tag = config.main["trigger"]["rpms"].replace("-gate", "-build")
        if tag == config.main["trigger"]["rpms"]:
            downstream_target = config.main["build"]["target"]

            targets[downstream_target].append({
                "comp": comp,
                "nvr": nvr,
                "scmurl": scmurl,
                "namespace": "rpms",
                "ref_overrides": None,
                "sidetag": None,
            })
        elif tag == config.main["trigger"]["modules"]:
            # TODO
            pass

        elif ((tag.startswith(upstream_build_tag) and tag.endswith("-stack-gate"))
              or tag.startswith("%s-side" % upstream_build_tag)):
            # TODO
            pass
        else:
            logger.debug("Message tag not configured as a trigger, ignoring.")

    for target, builds in targets.items():
        task.deferLater(reactor, 0, rebuild_batch, target, builds)


@inlineCallbacks
def rebuild_batch(target, builds):
    bsys = kojihelpers.get_buildsys("destination")

    # skip tagging and waiting for the repo if source and destination build systems differ
    if not config.dry_run and config.main["source"]["profile"] == config.main["destination"]["profile"]:
        with bsys.multicall() as mc:
            for build in builds:
                nvr = build["nvr"]
                logger.info(f"Tagging {nvr} into {target}")
                mc.tagBuild(target, nvr)

        buildroot = kojihelpers.get_target_info(target)["build_tag_name"]

        # Wait for the buildroot repo to regenerate
        try:
            yield kojihelpers.wait_repo(buildroot)
        except TimeoutError:
            # If we timed out, it's likely that the repo regenerated before we had time to
            # start waiting for it, so just proceed and hope.
            pass

    build_components(target, builds)


def build_components(target, builds):
    bsys = kojihelpers.get_buildsys("destination")
    prefix = config.main["build"]["prefix"]

    with bsys.multicall() as mc:
        for build in builds:
            component = build["comp"]
            namespace = build["namespace"]
            ref = config.split_scmurl(build["scmurl"])["ref"]
            scmurl = f"{prefix}/{namespace}/{component}#{ref}"

            dry = "DRY-RUN: " if config.dry_run else ""
            scratch = "Scratch-b" if config.main["build"]["scratch"] else "B"
            logger.info(f"{dry}{scratch}uilding {scmurl} for {target}")

            if not config.dry_run:
                bsys.build(scmurl, target, {"scratch": config.main["build"]["scratch"]})
