import os
import sys
import tempfile

import distrobuildsync
from distrobuildsync import config
from parameterized import parameterized
import helpers
import re
import responses

try:
    import unittest2 as unittest
except ImportError:
    import unittest


class TestConfigSetting(unittest.TestCase):
    def test_load_config(self):
        with tempfile.TemporaryDirectory() as td:
            helpers.setup_test_repo(
                td,
                os.path.join(helpers.DATA_DIR, "config", "distrobaker.yaml"),
            )
            # try loading the config
            config.scmurl = td + "#main"
            config.load_config()

        # verify some derived values are present in the configuration
        # with the expected values
        self.assertEqual(
            config.comps["rpms"]["ipa"],
            {
                "source": "freeipa.git#f33",
                "destination": "ipa.git#fluff-42.0.0-alpha",
                "cache": {"source": "freeipa", "destination": "ipa"},
            },
        )
        self.assertEqual(
            config.comps["modules"]["testmodule:master"],
            {
                "source": "testmodule.git#master",
                "destination": "testmodule#stream-master-fluff-42.0.0-alpha-experimental",
                "cache": {"source": "testmodule", "destination": "testmodule"},
            },
        )

    # test for failure when loading config files that are missing required sections
    # (this is just a randomly selected few of many possibilities)
    @parameterized.expand(
        [
            # (testcase_name, config_file, expected_error)
            (
                "no configuration",
                "distrobaker-no-configuration.yaml",
                "configuration block is missing",
            ),
            ("no trigger", "distrobaker-no-trigger.yaml", "trigger missing"),
            (
                "no source profile",
                "distrobaker-no-source-profile.yaml",
                "source.profile missing",
            ),
        ]
    )
    def test_load_config_missing_section(
        self, testcase_name, config_file, expected_error
    ):
        with tempfile.TemporaryDirectory() as td:
            helpers.setup_test_repo(
                td, os.path.join(helpers.DATA_DIR, "config", config_file)
            )
            with self.assertLogs(config.logger) as cm:
                config.scmurl = td + "#main"
                config.load_config()
            # make sure expected_error appears in logger output
            self.assertTrue(
                helpers.strings_with_substring(cm.output, expected_error),
                msg="'{}' not found in logger output: {}".format(
                    expected_error, cm.output
                ),
            )

    @responses.activate
    def test_load_autopackagelist(self):
        def request_callback(request):
            return (200, {}, "PackageA\nPackageB")

        responses.add_callback(
            responses.GET,
            re.compile("https://cr.example.com/.*"),
            callback=request_callback,
        )

        with tempfile.TemporaryDirectory() as td:
            helpers.setup_test_repo(
                td,
                os.path.join(
                    helpers.DATA_DIR,
                    "config",
                    "distrobaker-autopackagelist.yaml",
                ),
            )
            config.scmurl = td + "#main"
            config.load_config()

        # verify some derived values are present in the configuration
        # with the expected values
        self.assertEqual(
            config.comps["rpms"]["PackageA"],
            {
                "source": "PackageA.git#rawhide",
                "destination": "PackageA.git#rawhide",
                "cache": {"source": "PackageA", "destination": "PackageA"},
            },
        )

        self.assertEqual(
            config.comps["rpms"]["PackageB"],
            {
                "source": "PackageB.git#rawhide",
                "destination": "PackageB.git#rawhide",
                "cache": {"source": "PackageB", "destination": "PackageB"},
            },
        )
