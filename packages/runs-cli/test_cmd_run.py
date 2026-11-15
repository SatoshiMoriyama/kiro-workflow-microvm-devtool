#!/usr/bin/env python3
"""Tests for runs_cli.cmd_run. subprocess is mocked; AWS is never contacted."""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from runs_cli import cmd_run  # noqa: E402
from runs_cli import __main__ as main_mod  # noqa: E402


class TestPureLogic(unittest.TestCase):
    def test_request_id_format(self):
        rid = cmd_run.generate_request_id(time.localtime(), 4242)
        self.assertRegex(rid, r"^req-\d{8}-\d{6}-4242$")
        self.assertRegex(cmd_run.generate_request_id(), r"^req-\d{8}-\d{6}-\d+$")

    def test_validate_request_id(self):
        self.assertEqual(cmd_run.validate_request_id("req-1_a.b"), "req-1_a.b")
        for bad in ("", "../x", "a/b", "a b", "-x", "a..b"):
            with self.assertRaises(cmd_run.RunError):
                cmd_run.validate_request_id(bad)

    def test_payload_without_repo(self):
        p = json.loads(cmd_run.build_payload("r", "r/runs/", "r/inputs/prompt.md", "", "main"))
        self.assertEqual(p, {"requestId": "r", "prefix": "r/runs/",
                             "promptKey": "r/inputs/prompt.md", "baseBranch": "main"})

    def test_payload_with_repo(self):
        p = json.loads(cmd_run.build_payload("r", "r/runs/", "k", "https://x/y.git", "dev"))
        self.assertEqual(p["repoUrl"], "https://x/y.git")
        self.assertEqual(p["baseBranch"], "dev")

    def test_payload_size_limit(self):
        ok = "x" * 4000
        self.assertGreater(cmd_run.check_payload_size(ok), 0)
        exactly = "a" * 4096
        self.assertEqual(cmd_run.check_payload_size(exactly), 4096)
        with self.assertRaises(cmd_run.RunError) as ctx:
            cmd_run.check_payload_size("a" * 4097)
        self.assertIn("exceeds 4096 bytes", str(ctx.exception))
        self.assertIn("over by 1 bytes", str(ctx.exception))

    def test_payload_size_counts_utf8_bytes(self):
        # 1366 three-byte characters = 4098 bytes but only 1366 characters.
        with self.assertRaises(cmd_run.RunError):
            cmd_run.check_payload_size("あ" * 1366)


class FakeAws:
    """Records `aws` invocations and answers them like the real CLI would."""

    def __init__(self, status_after=1, status_ok=True, fail_on=None):
        self.calls = []
        self.status_after = status_after  # head-object succeeds from this attempt on
        self.heads = 0
        self.status_ok = status_ok
        self.fail_on = fail_on
        self.put_body = None

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        sub = cmd[5:]  # drop: aws --profile P --region R
        key = " ".join(sub[:2])
        if self.fail_on and key == self.fail_on:
            return subprocess.CompletedProcess(cmd, 255, "", "boom")
        out = ""
        rc = 0
        if sub[0] == "cloudformation":
            out = "bucket-1\n" if "ResultBucketName" in " ".join(sub) else "arn:role\n"
        elif key == "lambda-microvms list-microvm-images":
            out = "arn:image\n"
        elif key == "s3api put-object":
            with open(sub[sub.index("--body") + 1], "rb") as f:
                self.put_body = f.read()
        elif key == "lambda-microvms run-microvm":
            out = "mvm-123\n"
        elif key == "s3api head-object":
            self.heads += 1
            rc = 0 if self.heads >= self.status_after else 254
        elif key == "s3 sync":
            dest = sub[3].rstrip("/")
            os.makedirs(dest, exist_ok=True)
            with open(os.path.join(dest, "status.json"), "w", encoding="utf-8") as f:
                json.dump({"ok": self.status_ok, "exitCode": 0 if self.status_ok else 1}, f)
            with open(os.path.join(dest, "report.md"), "w", encoding="utf-8") as f:
                f.write("# r")
        return subprocess.CompletedProcess(cmd, rc, out, "")

    def keys(self):
        return [" ".join(c[5:7]) for c in self.calls]


def _args(**kw):
    base = dict(prompt=None, repo_url=None, base_branch=None, request_id=None,
                profile=None, region=None, stack_name=None, image_name=None,
                results_dir=None, json=False)
    base.update(kw)
    return argparse.Namespace(**base)


class TestRun(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.results = os.path.join(self._tmp.name, "results")
        env = {"AWS_PROFILE": "test-prof", "XDG_CONFIG_HOME": self._tmp.name}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = mock.patch.object(cmd_run.time, "sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, fake, **kw):
        kw.setdefault("results_dir", self.results)
        kw.setdefault("request_id", "req-test-1")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cmd_run.subprocess, "run", fake), \
                redirect_stdout(out), redirect_stderr(err):
            code = cmd_run.run(_args(**kw))
        return code, out.getvalue(), err.getvalue()

    def test_success_json(self):
        fake = FakeAws(status_after=3)
        code, out, err = self._run(fake, json=True, prompt="日本語 prompt")
        self.assertEqual(code, 0)
        last = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(last, {
            "requestId": "req-test-1",
            "resultsDir": os.path.join(os.path.abspath(self.results), "req-test-1"),
            "ok": True, "exitCode": 0, "microvmId": "mvm-123",
        })
        self.assertEqual(fake.put_body.decode("utf-8"), "日本語 prompt")
        self.assertEqual(fake.heads, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertIn("[run] microvm=mvm-123", err)
        self.assertIn("report.md", err)
        # VM is terminated at the end.
        self.assertEqual(fake.keys()[-1], "lambda-microvms terminate-microvm")
        # Profile and region are applied to every call.
        for c in fake.calls:
            self.assertEqual(c[:5], ["aws", "--profile", "test-prof", "--region", "ap-northeast-1"])

    def test_run_microvm_arguments(self):
        fake = FakeAws()
        self._run(fake, repo_url="https://github.com/o/r.git", base_branch="dev")
        call = next(c for c in fake.calls if c[5:7] == ["lambda-microvms", "run-microvm"])
        self.assertEqual(call[call.index("--maximum-duration-in-seconds") + 1], "1800")
        egress = json.loads(call[call.index("--egress-network-connectors") + 1])
        self.assertEqual(egress, [
            "arn:aws:lambda:ap-northeast-1:aws:network-connector:aws-network-connector:INTERNET_EGRESS"])
        payload = json.loads(call[call.index("--run-hook-payload") + 1])
        self.assertEqual(payload["promptKey"], "req-test-1/inputs/prompt.md")
        self.assertEqual(payload["prefix"], "req-test-1/runs/")
        self.assertEqual(payload["repoUrl"], "https://github.com/o/r.git")
        self.assertEqual(payload["baseBranch"], "dev")
        put = next(c for c in fake.calls if c[5:7] == ["s3api", "put-object"])
        self.assertEqual(put[put.index("--tagging") + 1], "kind=input")

    def test_ok_false_still_exits_zero(self):
        code, out, _ = self._run(FakeAws(status_ok=False), json=True)
        self.assertEqual(code, 0)
        data = json.loads(out.strip().splitlines()[-1])
        self.assertIs(data["ok"], False)
        self.assertEqual(data["exitCode"], 1)

    def test_plain_output_shows_results_path(self):
        code, out, _ = self._run(FakeAws())
        self.assertEqual(code, 0)
        self.assertIn(os.path.join(os.path.abspath(self.results), "req-test-1"), out)

    def test_missing_profile_is_error_without_aws_calls(self):
        fake = FakeAws()
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ["XDG_CONFIG_HOME"] = self._tmp.name
            code, out, err = self._run(fake)
        self.assertEqual(code, 1)
        self.assertIn("AWS_PROFILE is not set", err)
        self.assertEqual(fake.calls, [])

    def test_oversized_payload_exits_before_put(self):
        fake = FakeAws()
        code, _, err = self._run(fake, repo_url="https://example.com/" + "a" * 5000)
        self.assertEqual(code, 1)
        self.assertIn("exceeds 4096 bytes", err)
        self.assertNotIn("s3api put-object", fake.keys())
        self.assertNotIn("lambda-microvms run-microvm", fake.keys())

    def test_failure_before_vm_deletes_prompt(self):
        fake = FakeAws(fail_on="lambda-microvms run-microvm")
        code, _, err = self._run(fake)
        self.assertEqual(code, 1)
        self.assertIn("s3api put-object", fake.keys())
        self.assertEqual(fake.keys()[-1], "s3api delete-object")
        self.assertNotIn("lambda-microvms terminate-microvm", fake.keys())

    def test_status_timeout_terminates_vm_and_fails(self):
        fake = FakeAws(status_after=10 ** 9)
        code, _, err = self._run(fake)
        self.assertEqual(code, 1)
        self.assertEqual(fake.heads, 372)
        self.assertIn("status.json not found", err)
        self.assertEqual(fake.keys()[-1], "lambda-microvms terminate-microvm")
        self.assertNotIn("s3api delete-object", fake.keys())

    def test_interrupt_terminates_vm(self):
        fake = FakeAws(status_after=10 ** 9)
        self.sleep.side_effect = KeyboardInterrupt
        code, _, _ = self._run(fake)
        self.assertEqual(code, 130)
        self.assertEqual(fake.keys()[-1], "lambda-microvms terminate-microvm")

    def test_missing_aws_cli(self):
        def boom(*a, **k):
            raise FileNotFoundError
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cmd_run.subprocess, "run", boom), \
                redirect_stdout(out), redirect_stderr(err):
            code = cmd_run.run(_args(results_dir=self.results))
        self.assertEqual(code, 1)
        self.assertIn("'aws' CLI was not found", err.getvalue())

    def test_env_vars_are_used(self):
        fake = FakeAws()
        env = {"PROMPT": "from env", "REPO_URL": "https://github.com/o/r.git",
               "BASE_BRANCH": "develop", "REQUEST_ID": "req-from-env"}
        with mock.patch.dict(os.environ, env):
            code, out, _ = self._run(fake, request_id=None)
        self.assertEqual(code, 0)
        self.assertEqual(fake.put_body, b"from env")
        self.assertIn("req-from-env", out)
        call = next(c for c in fake.calls if c[5:7] == ["lambda-microvms", "run-microvm"])
        self.assertEqual(json.loads(call[call.index("--run-hook-payload") + 1])["baseBranch"], "develop")

    def test_default_prompt(self):
        fake = FakeAws()
        self._run(fake)
        self.assertEqual(fake.put_body.decode("utf-8"), cmd_run.DEFAULT_PROMPT)


class TestCli(unittest.TestCase):
    def test_help_is_english_and_lists_run(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main_mod.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("run", out.getvalue())
        self.assertIsNone(re.search(r"[\u3000-\u9fff]", out.getvalue()))

    def test_run_without_profile_exits_nonzero(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}, clear=True), \
                redirect_stderr(err):
            code = main_mod.main(["run", "hello"])
        self.assertNotEqual(code, 0)
        self.assertIn("AWS_PROFILE is not set", err.getvalue())


if __name__ == "__main__":
    unittest.main()
