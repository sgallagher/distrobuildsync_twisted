import git
import logging
import os
import tempfile
import twisted.internet.utils
import yaml

from twisted.internet.defer import Deferred, inlineCallbacks

# Global logger
logger = logging.getLogger(__name__)

# Configuration options
batch_timer = None
config_timer = 300
configuration = None
config_ref = None
distrogitsync = None
dry_run = False
retries = 3
scmurl = None
main = None
comps = None


class ConfigError(Exception):
    pass


class UnknownRefError(ConfigError):
    pass


def split_scmurl(scmurl):
    """Splits a `link#ref` style URLs into the link and ref parts.  While
    generic, many code paths in DistroBuildSync expect these to be branch names.
    `link` forms are also accepted, in which case the returned `ref` is None.

    It also attempts to extract the namespace and component, where applicable.
    These can only be detected if the link matches the standard dist-git
    pattern; in other cases the results may be bogus or None.

    :param scmurl: A link#ref style URL, with #ref being optional
    :returns: A dictionary with `link`, `ref`, `ns` and `comp` keys
    """
    scm = scmurl.split("#", 1)
    nscomp = scm[0].split("/")
    return {
        "link": scm[0],
        "ref": scm[1] if len(scm) >= 2 else None,
        "ns": nscomp[-2] if len(nscomp) >= 2 else None,
        "comp": nscomp[-1] if nscomp else None,
    }


def split_module(comp):
    """Splits modules component name into name and stream pair.  Expects the
    name to be in the `name:stream` format.  Defaults to stream=master if the
    split fails.

    :param comp: The component name
    :returns: Dictionary with name and stream
    """
    ms = comp.split(":")
    return {
        "name": ms[0],
        "stream": ms[1] if len(ms) > 1 and ms[1] else "master",
    }


@inlineCallbacks
def get_config_ref(url):
    """Gets the ref for the config SCMURL

    Returns the actual ref for a symbolic ref possibly used in the
    config SCMURL.  Used by the update function to check whether the
    config should be resync'd.

    :param scmurl: Config SCMURL
    :returns: Remote ref or None on error
    """
    scm = split_scmurl(url)
    output = yield twisted.internet.utils.getProcessOutput(
        executable="git",
        args=(
            "ls-remote",
            "--heads",
            scm["link"],
            scm["ref"],
        ),
        errortoo=True,
    )

    if not output:
        scmref = scm["ref"]
        scmlink = scm["link"]
        raise UnknownRefError(f"{scmref} not found in {scmlink}")

    return output.split(b"\t", 1)[0]


@inlineCallbacks
def update_config():
    logger.critical(f"Updating configuration")

    try:
        ref = yield get_config_ref(scmurl)
    except UnknownRefError as e:
        logger.critical(e)
        raise ConfigError(f"The configuration repository is unavailable, skipping update.  Checking again in {config_timer} seconds.")

    main, comps = yield load_config()


# FIXME: This needs even more error checking, e.g.
#         - check if blocks are actual dictionaries
#         - check if certain values are what we expect
def load_config():
    """Loads or updates the global configuration from the provided URL in
    the `link#branch` format.  If no branch is provided, assumes `master`.

    The operation is atomic and the function can be safely called to update
    the configuration without the danger of clobbering the current one.

    `crepo` must be a git repository with `distrobaker.yaml` in it.

    :returns: The configuration dictionary, or None on error
    """
    cdir = tempfile.TemporaryDirectory(prefix="distrobaker-")
    logger.info("Fetching configuration from %s to %s", scmurl, cdir.name)
    scm = split_scmurl(scmurl)
    if scm["ref"] is None:
        scm["ref"] = "master"
    for attempt in range(retries):
        try:
            git.Repo.clone_from(scm["link"], cdir.name).git.checkout(
                scm["ref"]
            )
        except Exception:
            logger.warning(
                "Failed to fetch configuration, retrying (#%d).",
                attempt + 1,
                exc_info=True,
            )
            continue
        else:
            logger.info("Configuration fetched successfully.")
            break
    else:
        logger.error("Failed to fetch configuration, giving up.")
        return None
    if os.path.isfile(os.path.join(cdir.name, "distrobaker.yaml")):
        try:
            with open(os.path.join(cdir.name, "distrobaker.yaml")) as f:
                y = yaml.safe_load(f)
            logger.debug(
                "%s loaded, processing.",
                os.path.join(cdir.name, "distrobaker.yaml"),
            )
        except Exception:
            logger.exception("Could not parse distrobaker.yaml.")
            return None
    else:
        logger.error(
            "Configuration repository does not contain distrobaker.yaml."
        )
        return None
    n = dict()
    if "configuration" in y:
        cnf = y["configuration"]
        for k in ("source", "destination"):
            if k in cnf:
                n[k] = dict()
                if "scm" in cnf[k]:
                    n[k]["scm"] = str(cnf[k]["scm"])
                else:
                    logger.error("Configuration error: %s.scm missing.", k)
                    return None
                if "cache" in cnf[k]:
                    n[k]["cache"] = dict()
                    for kc in ("url", "cgi", "path"):
                        if kc in cnf[k]["cache"]:
                            n[k]["cache"][kc] = str(cnf[k]["cache"][kc])
                        else:
                            logger.error(
                                "Configuration error: %s.cache.%s missing.",
                                k,
                                kc,
                            )
                            return None
                else:
                    logger.error("Configuration error: %s.cache missing.", k)
                    return None
                if "profile" in cnf[k]:
                    n[k]["profile"] = str(cnf[k]["profile"])
                else:
                    logger.error("Configuration error: %s.profile missing.", k)
                    return None
                if "mbs" in cnf[k]:
                    n[k]["mbs"] = cnf[k]["mbs"]
                else:
                    logger.error("Configuration error: %s.mbs missing.", k)
                    return None
            else:
                logger.error("Configuration error: %s missing.", k)
                return None
        if "trigger" in cnf:
            n["trigger"] = dict()
            for k in ("rpms", "modules"):
                if k in cnf["trigger"]:
                    n["trigger"][k] = str(cnf["trigger"][k])
                else:
                    logger.error("Configuration error: trigger.%s missing.", k)
        else:
            logger.error("Configuration error: trigger missing.")
            return None
        if "build" in cnf:
            n["build"] = dict()
            for k in ("prefix", "target", "platform"):
                if k in cnf["build"]:
                    n["build"][k] = str(cnf["build"][k])
                else:
                    logger.error("Configuration error: build.%s missing.", k)
                    return None
            if "scratch" in cnf["build"]:
                n["build"]["scratch"] = bool(cnf["build"]["scratch"])
            else:
                logger.warning(
                    "Configuration warning: build.scratch not defined, assuming false."
                )
                n["build"]["scratch"] = False
        else:
            logger.error("Configuration error: build missing.")
            return None
        if "git" in cnf:
            n["git"] = dict()
            for k in ("author", "email", "message"):
                if k in cnf["git"]:
                    n["git"][k] = str(cnf["git"][k])
                else:
                    logger.error("Configuration error: git.%s missing.", k)
                    return None
        else:
            logger.error("Configuration error: git missing.")
            return None
        if "control" in cnf:
            n["control"] = dict()
            for k in ("build", "merge", "strict"):
                if k in cnf["control"]:
                    n["control"][k] = bool(cnf["control"][k])
                else:
                    logger.error("Configuration error: control.%s missing.", k)
                    return None
            n["control"]["exclude"] = {"rpms": set(), "modules": set()}
            if "exclude" in cnf["control"]:
                for cns in ("rpms", "modules"):
                    if cns in cnf["control"]["exclude"]:
                        n["control"]["exclude"][cns].update(
                            cnf["control"]["exclude"][cns]
                        )
            for cns in ("rpms", "modules"):
                if n["control"]["exclude"]["rpms"]:
                    logger.info(
                        "Excluding %d component(s) from the %s namespace.",
                        len(n["control"]["exclude"][cns]),
                        cns,
                    )
                else:
                    logger.info(
                        "Not excluding any components from the %s namespace.",
                        cns,
                    )
        else:
            logger.error("Configuration error: control missing.")
            return None
        if "defaults" in cnf:
            n["defaults"] = dict()
            for dk in ("cache", "rpms", "modules"):
                if dk in cnf["defaults"]:
                    n["defaults"][dk] = dict()
                    for dkk in ("source", "destination"):
                        if dkk in cnf["defaults"][dk]:
                            n["defaults"][dk][dkk] = str(
                                cnf["defaults"][dk][dkk]
                            )
                        else:
                            logger.error(
                                "Configuration error: defaults.%s.%s missing.",
                                dk,
                                dkk,
                            )
                else:
                    logger.error(
                        "Configuration error: defaults.%s missing.", dk
                    )
                    return None
        else:
            logger.error("Configuration error: defaults missing.")
            return None
    else:
        logger.error("The required configuration block is missing.")
        return None
    components = 0
    nc = {
        "rpms": dict(),
        "modules": dict(),
    }
    if "components" in y:
        cnf = y["components"]
        for k in ("rpms", "modules"):
            if k in cnf:
                for p in cnf[k].keys():
                    components += 1
                    nc[k][p] = dict()
                    cname = p
                    sname = ""
                    if k == "modules":
                        ms = split_module(p)
                        cname = ms["name"]
                        sname = ms["stream"]
                    nc[k][p]["source"] = n["defaults"][k]["source"] % {
                        "component": cname,
                        "stream": sname,
                    }
                    nc[k][p]["destination"] = n["defaults"][k][
                        "destination"
                    ] % {"component": cname, "stream": sname}
                    nc[k][p]["cache"] = {
                        "source": n["defaults"]["cache"]["source"]
                        % {"component": cname, "stream": sname},
                        "destination": n["defaults"]["cache"]["destination"]
                        % {"component": cname, "stream": sname},
                    }
                    if cnf[k][p] is None:
                        cnf[k][p] = dict()
                    for ck in ("source", "destination"):
                        if ck in cnf[k][p]:
                            nc[k][p][ck] = str(cnf[k][p][ck])
                    if "cache" in cnf[k][p]:
                        for ck in ("source", "destination"):
                            if ck in cnf[k][p]["cache"]:
                                nc[k][p]["cache"][ck] = str(
                                    cnf[k][p]["cache"][ck]
                                )
            logger.info(
                "Found %d configured component(s) in the %s namespace.",
                len(nc[k]),
                k,
            )
    if n["control"]["strict"]:
        logger.info(
            "Running in the strict mode.  Only configured components will be processed."
        )
    else:
        logger.info(
            "Running in the non-strict mode.  All trigger components will be processed."
        )
    if not components:
        if n["control"]["strict"]:
            logger.warning(
                "No components configured while running in the strict mode.  Nothing to do."
            )
        else:
            logger.info("No components explicitly configured.")
    main = n
    comps = nc
    return main, comps
