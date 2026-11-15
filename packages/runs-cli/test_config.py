#!/usr/bin/env python3
"""Tests for runs_cli.config."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from runs_cli import config  # noqa: E402


class TestParse(unittest.TestCase):
    def test_key_value_comments_quotes(self):
        text = (
            "# comment\n"
            "\n"
            "AWS_PROFILE=myprof\n"
            'AWS_REGION="us-east-1"\n'
            "STACK_NAME='MyStack'\n"
            "  MICROVM_IMAGE_NAME = img  \n"
            "no-equals-line\n"
            "KIRO_MICROVM_RESULTS_DIR=/tmp/r=1\n"
        )
        got = config.parse_config_text(text)
        self.assertEqual(got["AWS_PROFILE"], "myprof")
        self.assertEqual(got["AWS_REGION"], "us-east-1")
        self.assertEqual(got["STACK_NAME"], "MyStack")
        self.assertEqual(got["MICROVM_IMAGE_NAME"], "img")
        self.assertEqual(got["KIRO_MICROVM_RESULTS_DIR"], "/tmp/r=1")
        self.assertNotIn("no-equals-line", got)
        self.assertEqual(len(got), 5)

    def test_missing_file_is_empty(self):
        self.assertEqual(config.load_config_file("/nonexistent/kiro/config"), {})


class TestResolve(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "config")

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_defaults(self):
        cfg = config.resolve({}, env={"HOME": "/h"}, config_path=self.path)
        self.assertIsNone(cfg.aws_profile)
        self.assertEqual(cfg.aws_region, "ap-northeast-1")
        self.assertEqual(cfg.stack_name, "KiroMicrovmBase")
        self.assertEqual(cfg.image_name, "kiro-microvm")
        self.assertTrue(cfg.results_dir.endswith(
            os.path.join("kiro-microvm", "results")))

    def test_precedence_cli_env_file(self):
        self._write("AWS_PROFILE=file\nAWS_REGION=file-region\nSTACK_NAME=file-stack\n")
        env = {"AWS_PROFILE": "env", "AWS_REGION": "env-region"}
        cfg = config.resolve({"AWS_PROFILE": "cli"}, env=env, config_path=self.path)
        self.assertEqual(cfg.aws_profile, "cli")          # CLI wins
        self.assertEqual(cfg.aws_region, "env-region")    # env beats file
        self.assertEqual(cfg.stack_name, "file-stack")    # file beats default

    def test_empty_cli_value_falls_through(self):
        cfg = config.resolve({"AWS_PROFILE": None}, env={"AWS_PROFILE": "env"},
                             config_path=self.path)
        self.assertEqual(cfg.aws_profile, "env")

    def test_results_dir_precedence(self):
        self._write("KIRO_MICROVM_RESULTS_DIR=/file/results\n")
        cfg = config.resolve({}, env={}, config_path=self.path)
        self.assertEqual(cfg.results_dir, "/file/results")
        cfg = config.resolve({}, env={"KIRO_MICROVM_RESULTS_DIR": "/env/results"},
                             config_path=self.path)
        self.assertEqual(cfg.results_dir, "/env/results")
        cfg = config.resolve({"KIRO_MICROVM_RESULTS_DIR": "/cli/results"},
                             env={"KIRO_MICROVM_RESULTS_DIR": "/env/results"},
                             config_path=self.path)
        self.assertEqual(cfg.results_dir, "/cli/results")

    def test_xdg_paths(self):
        env = {"XDG_CONFIG_HOME": "/xc", "XDG_DATA_HOME": "/xd"}
        self.assertEqual(config.config_file_path(env), "/xc/kiro-microvm/config")
        self.assertEqual(config.data_dir(env), "/xd/kiro-microvm")
        cfg = config.resolve({}, env=env, config_path=self.path)
        self.assertEqual(cfg.results_dir, "/xd/kiro-microvm/results")

    def test_profile_required(self):
        cfg = config.resolve({}, env={}, config_path=self.path)
        with self.assertRaises(config.ConfigError) as ctx:
            cfg.require_profile()
        self.assertIn("AWS_PROFILE is not set", str(ctx.exception))

    def test_profile_from_file_satisfies_requirement(self):
        self._write("AWS_PROFILE=p\n")
        cfg = config.resolve({}, env={}, config_path=self.path)
        self.assertEqual(cfg.require_profile(), "p")


if __name__ == "__main__":
    unittest.main()
