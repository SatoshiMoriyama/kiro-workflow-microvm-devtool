#!/usr/bin/env python3
"""runs_cli.loader の純関数ユニットテスト(pytest / unittest どちらでも動く)。

テストは一時ディレクトリ上に results/ 構造を組み立てて loader を検証する。
loader 以外(cmd_list / cmd_show / cmd_stats)のテストは並列タスクが
別クラス(TestCmdList / TestCmdShow / TestCmdStats)で追加するため、
このリード部分では TestLoader のみを定義しクラス名の衝突を避ける。
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from runs_cli import loader  # noqa: E402


def _write_json(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def _make_run_dir(base: str, name: str) -> str:
    d = os.path.join(base, name)
    os.makedirs(d, exist_ok=True)
    return d


class TestLoader(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.results_dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _valid_status(self, **overrides):
        status = {
            "requestId": "req-1",
            "prompt": "do something",
            "exitCode": 0,
            "timedOut": False,
            "artifacts": ["a.txt", "b.txt"],
            "skippedArtifacts": ["big.bin"],
            "failedUploads": [],
            "stdoutKey": "runs/req-1/stdout.txt",
            "startedAt": "2026-11-04T20:00:00+00:00",
            "finishedAt": "2026-11-04T20:00:42+00:00",
            "ok": True,
        }
        status.update(overrides)
        return status

    def test_valid_status_json_to_run(self):
        d = _make_run_dir(self.results_dir, "req-1")
        _write_json(os.path.join(d, "status.json"), self._valid_status())

        runs = loader.load_runs(self.results_dir)
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run.request_id, "req-1")
        self.assertIs(run.ok, True)
        self.assertEqual(run.exit_code, 0)
        self.assertIs(run.timed_out, False)
        self.assertEqual(run.artifacts, ["a.txt", "b.txt"])
        self.assertEqual(run.skipped_artifacts, ["big.bin"])
        self.assertEqual(run.failed_uploads, [])
        self.assertEqual(run.prompt, "do something")
        self.assertFalse(run.broken)
        self.assertEqual(run.path, os.path.abspath(d))
        # report.json が無いので report 由来のフィールドは None。
        self.assertIsNone(run.branch)
        self.assertIsNone(run.pushed_commit)
        self.assertIsNone(run.summary)

    def test_report_json_populates_fields(self):
        d = _make_run_dir(self.results_dir, "req-1")
        _write_json(os.path.join(d, "status.json"), self._valid_status())
        _write_json(
            os.path.join(d, "report.json"),
            {
                "branch": "kiro/req-1",
                "pushedCommit": "abc123",
                "summary": "done",
                "extra": "ignored",
            },
        )

        run = loader.load_runs(self.results_dir)[0]
        self.assertEqual(run.branch, "kiro/req-1")
        self.assertEqual(run.pushed_commit, "abc123")
        self.assertEqual(run.summary, "done")

    def test_report_json_absent_fields_none(self):
        d = _make_run_dir(self.results_dir, "req-1")
        _write_json(os.path.join(d, "status.json"), self._valid_status())
        # report.json あるが一部キー欠損 -> 欠損は None。
        _write_json(os.path.join(d, "report.json"), {"branch": "b"})

        run = loader.load_runs(self.results_dir)[0]
        self.assertEqual(run.branch, "b")
        self.assertIsNone(run.pushed_commit)
        self.assertIsNone(run.summary)

    def test_dir_without_status_is_broken(self):
        d = _make_run_dir(self.results_dir, "req-broken")
        # status.json が無く result.txt だけある。
        with open(os.path.join(d, "result.txt"), "w", encoding="utf-8") as f:
            f.write("partial output")

        runs = loader.load_runs(self.results_dir)
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertTrue(run.broken)
        self.assertEqual(run.request_id, "req-broken")
        self.assertIsNone(run.ok)
        self.assertIsNone(run.exit_code)
        self.assertEqual(run.path, os.path.abspath(d))

    def test_broken_json_status_is_broken(self):
        d = _make_run_dir(self.results_dir, "req-badjson")
        with open(os.path.join(d, "status.json"), "w", encoding="utf-8") as f:
            f.write("{ this is not valid json ")

        runs = loader.load_runs(self.results_dir)
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertTrue(run.broken)
        self.assertEqual(run.request_id, "req-badjson")
        self.assertIsNone(run.ok)

    def test_broken_report_keeps_status(self):
        d = _make_run_dir(self.results_dir, "req-1")
        _write_json(os.path.join(d, "status.json"), self._valid_status())
        with open(os.path.join(d, "report.json"), "w", encoding="utf-8") as f:
            f.write("{ broken ")

        run = loader.load_runs(self.results_dir)[0]
        # status 部分は保持される。
        self.assertFalse(run.broken)
        self.assertEqual(run.request_id, "req-1")
        self.assertIs(run.ok, True)
        # report 側のみ None。
        self.assertIsNone(run.branch)
        self.assertIsNone(run.pushed_commit)
        self.assertIsNone(run.summary)

    def test_duration_seconds_value(self):
        d = _make_run_dir(self.results_dir, "req-1")
        _write_json(os.path.join(d, "status.json"), self._valid_status())
        run = loader.load_runs(self.results_dir)[0]
        self.assertEqual(loader.duration_seconds(run), 42.0)

    def test_duration_seconds_none_when_missing(self):
        d = _make_run_dir(self.results_dir, "req-1")
        _write_json(
            os.path.join(d, "status.json"),
            self._valid_status(startedAt=None, finishedAt=None),
        )
        run = loader.load_runs(self.results_dir)[0]
        self.assertIsNone(loader.duration_seconds(run))

    def test_exit_code_null(self):
        d = _make_run_dir(self.results_dir, "req-1")
        _write_json(
            os.path.join(d, "status.json"),
            self._valid_status(exitCode=None),
        )
        run = loader.load_runs(self.results_dir)[0]
        self.assertIsNone(run.exit_code)
        self.assertFalse(run.broken)

    def test_missing_results_dir_returns_empty(self):
        missing = os.path.join(self.results_dir, "does-not-exist")
        self.assertEqual(loader.load_runs(missing), [])


if __name__ == "__main__":
    unittest.main()
