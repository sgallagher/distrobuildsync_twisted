from . import config

import datetime
import koji
import yaml

from twisted.internet import reactor, task
from twisted.internet.defer import Deferred, inlineCallbacks

logger = config.logger


def get_buildsys(which, force_login=False):
    """Get a koji build system session for either the source or the
    destination.  Caches the sessions so future calls are cheap.
    Destination sessions are authenticated, source sessions are not.

    :param which: Session to select, source or destination
    :param bool force_login: Login also on source instance.
    :returns: Koji session object, or None on error
    """
    if not config.main:
        logger.critical("DistroBuildSync is not configured, aborting.")
        return None
    if which not in ("source", "destination"):
        logger.error('Cannot get "%s" build system.', which)
        return None

    session_timed_out = False
    if hasattr(get_buildsys, which):
        session_age = datetime.datetime.now() - getattr(
            get_buildsys, which + "_session_start_time"
        )
        # slightly less than an hour, to be safe
        if session_age.seconds > 3550 or session_age.days > 0:
            session_timed_out = True

    if session_timed_out or not hasattr(get_buildsys, which) or force_login:
        logger.debug(
            'Initializing the %s koji instance with the "%s" profile.',
            which,
            config.main[which]["profile"],
        )
        try:
            bsys = koji.read_config(profile_name=config.main[which]["profile"])
            bsys = koji.ClientSession(bsys["server"], opts=bsys)
        except Exception:
            logger.exception(
                'Failed initializing the %s koji instance with the "%s" profile, skipping.',
                which,
                config.main[which]["profile"],
            )
            return None
        logger.debug("The %s koji instance initialized.", which)
        if which == "destination" or force_login:
            logger.debug("Authenticating with the %s koji instance." % which)
            try:
                if session_timed_out:
                    bsys.logout()
                bsys.gssapi_login()
            except Exception:
                logger.exception(
                    "Failed authenticating against the %s koji instance, skipping." % which
                )
                return None
            logger.debug(
                "Successfully authenticated with the %s koji instance." % which
            )
        if which == "source":
            get_buildsys.source = bsys
            get_buildsys.source_session_start_time = datetime.datetime.now()
        else:
            get_buildsys.destination = bsys
            get_buildsys.destination_session_start_time = (
                datetime.datetime.now()
            )
    else:
        logger.debug(
            "The %s koji instance is already initialized, fetching from cache.",
            which,
        )
    return vars(get_buildsys)[which]


def get_build_info(nvr):
    """Get SCMURL, plus extra attributes for modules, for a source build system
    build NVR.  NVRs are unique.

    :param nvr: The build NVR to look up
    :returns: A dictionary with `scmurl`, `name`, `stream`, and `modulemd` keys,
    or None on error
    """
    if not config.main:
        logger.critical("DistroBuildSync is not configured, aborting.")
        return None

    bsys = get_buildsys("source")
    if bsys is None:
        logger.error(
            "Build system unavailable, cannot retrieve the build info of %s.",
            nvr,
        )
        return None
    try:
        bsrc = bsys.getBuild(nvr)
    except Exception:
        logger.exception(
            "An error occured while retrieving the build info for %s.", nvr
        )
        return None

    bi = dict()
    if "source" in bsrc:
        bi["scmurl"] = bsrc["source"]
        logger.debug("Retrieved SCMURL for %s: %s", nvr, bi["scmurl"])
    else:
        logger.error("Cannot find any SCMURL associated with %s.", nvr)
        return None

    try:
        minfo = bsrc["extra"]["typeinfo"]["module"]
        bi["name"] = minfo["name"]
        bi["stream"] = minfo["stream"]
        bi["module_version"] = minfo["version"]
        bi["modulemd"] = minfo["modulemd_str"]
        logger.debug(
            "Actual name:stream for %s is %s:%s", nvr, bi["name"], bi["stream"]
        )
    except Exception:
        bi["name"] = None
        bi["stream"] = None
        bi["module_version"] = None
        bi["modulemd"] = None
        logger.debug("No module info for %s.", nvr)

    return bi


def get_ref_overrides(modulemd):
    """
    Get RPM components ref overrides from the modulemd file.
    """
    ref_overrides = {}
    data = yaml.safe_load(modulemd)
    for name, rpm_data in data["data"]["xmd"]["mbs"]["rpms"].items():
        ref_overrides[name] = rpm_data["ref"]
    logger.info(f"RPM ref overrides {ref_overrides}")
    return ref_overrides


def get_target_info(target):
    """Get information about a build target

    :param target: the string name of the target
    :returns: A dictionary with the keys 'build_tag', 'build_tag_name',
    'dest_tag', 'dest_tag_name', 'id' and 'name' or None on error
    """
    bsys = get_buildsys("destination")
    if bsys is None:
        logger.error(
            "Build system unavailable, cannot retrieve the target info of %s.",
            target,
        )
        return None

    try:
        targetinfo = bsys.getBuildTarget(target)
    except Exception as e:
        logger.critical(e)
        logger.exception(
            "An error occured while retrieving the target info for %s.", target
        )
        return None

    return targetinfo


def get_scmurl(build_id):
    """Get the SCMURL that the build was created from

    :param build_id: The ID of the build (likely retrieved from a tagging message)
    :returns: A string containing the full, dereferenced SCMURL for the build
    """

    bsys = get_buildsys("source")
    if bsys is None:
        logger.error(f"Build system unavailable, cannot retrieve the SCMURL of {build_id}.")
        return None

    try:
        buildinfo = bsys.getBuild(build_id, strict=True)
    except koji.GenericError as e:
        logger.exception(f"Could not retrieve information for build {build_id}")
        return None

    return buildinfo["source"]


def wait_repo(tag):
    deferred = Deferred()
    deferred.addTimeout(config.waitrepo_timeout, reactor)
    config.awaited_repos[tag].append(deferred)

    logger.info(f"Waiting for {tag} to regenerate")
    return deferred
