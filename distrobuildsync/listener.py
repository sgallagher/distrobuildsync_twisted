import logging

from . import config
from . import kojihelpers

from collections import defaultdict, namedtuple
from twisted.internet import reactor, task
from twisted.internet.defer import AlreadyCalledError, inlineCallbacks
from queue import Empty

logger = config.logger


RebuildData = namedtuple(
    "RebuildData",
    [
        "ns",
        "comp",
        "version",
        "release",
        "scmurl",
        "downstream_target",
        "ref_overrides",
    ],
    defaults=[None, None],
)


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

    # Check whether we care about this tag and component
    comp = msg.body["name"]
    version = msg.body["version"]
    release = msg.body["release"]
    target_override = None
    ref_overrides = None
    tag = msg.body["tag"]
    upstream_build_tag = config.main["trigger"]["rpms"].replace(
        "-gate", "-build"
    )

    # Check that we are watching for this tag
    if tag == config.main["trigger"]["rpms"]:
        ns = "rpms"
    elif (
        tag.startswith(upstream_build_tag) and tag.endswith("-stack-gate")
    ) or tag.startswith("%s-side" % upstream_build_tag):
        ns = "rpms"
    elif tag == config.main["trigger"]["modules"]:
        ns = "modules"
    else:
        logger.debug(
            f"Message tag {tag} not configured as a trigger, ignoring."
        )
        return

    # Check whether this component is meaningful to us
    if config.main["control"]["strict"] and comp not in config.comps[ns]:
        logger.debug(f"{comp} is not an approved component, ignoring")
        return

    if comp in config.main["control"]["exclude"][ns]:
        logger.debug(f"{ns}/{comp} is on the exclude list, skipping")
        return

    # Handle slower tasks after verifying component validity
    if tag == config.main["trigger"]["modules"]:
        logger.info("Handling an Module trigger for %s, tag %s.", comp, tag)
        nvr = f"{comp}-{version}-{release}"
        bi = kojihelpers.get_build_info(nvr)
        ref_overrides = kojihelpers.get_ref_overrides(bi["modulemd"])

    elif (
        tag.startswith(upstream_build_tag) and tag.endswith("-stack-gate")
    ) or tag.startswith("%s-side" % upstream_build_tag):
        # Ensure that the downstream side-tag exists
        target_override = kojihelpers.create_side_tag(
            config.main["build"]["target"], tag
        )

    scmurl = kojihelpers.get_scmurl(msg.body["build_id"])

    rd = RebuildData(
        ns, comp, version, release, scmurl, target_override, ref_overrides
    )

    reactor.callFromThread(config.batch_processor.reset)
    reactor.callFromThread(config.message_queue.put, rd)


def process_batch():
    batches = defaultdict(list)
    while True:
        try:
            rd = config.message_queue.get_nowait()
            batches[rd.downstream_target].append(rd)
        except Empty as e:
            break

    if not len(batches.keys()):
        return

    # Return to the mainloop so we don't block future batches
    # Schedule a task for each downstream target
    for target, builds in batches.items():
        task.deferLater(reactor, 0, rebuild_batch, target, builds)


@inlineCallbacks
def rebuild_batch(target, builds):
    bsys = kojihelpers.get_buildsys("destination")

    # skip tagging and waiting for the repo if source and destination build systems differ
    if (
        not config.dry_run
        and config.main["source"]["profile"]
        == config.main["destination"]["profile"]
    ):
        with bsys.multicall(batch=config.koji_batch) as mc:
            for build in builds:
                nvr = f"{build.comp}-{build.version}-{build.release}"
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
    if not target:
        target = config.main["build"]["target"]

    with bsys.multicall(batch=config.koji_batch) as mc:
        for rd in builds:
            logger.critical(f"REMOVEME: {rd}")
            component = rd.comp
            namespace = rd.ns
            ref = config.split_scmurl(rd.scmurl)["ref"]
            downstream_scmurl = f"{prefix}/{namespace}/{component}#{ref}"

            dry = "DRY-RUN: " if config.dry_run else ""
            scratch = "Scratch-b" if config.main["build"]["scratch"] else "B"
            logger.info(
                f"{dry}{scratch}uilding {downstream_scmurl} for {target}"
            )

            if not config.dry_run:
                kojihelpers.call_distrogitsync(
                    namespace, component, rd.ref_overrides
                )
                bsys.build(
                    downstream_scmurl,
                    target,
                    {"scratch": config.main["build"]["scratch"]},
                )
