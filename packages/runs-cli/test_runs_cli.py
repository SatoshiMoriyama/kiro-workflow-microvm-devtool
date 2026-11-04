#!/usr/bin/env python3
"""runs_cli.loader の純関数ユニットテスト(pytest / unittest どちらでも動く)。

テストは一時ディレクトリ上に results/ 構造を組み立てて loader を検証する。
loader 以外(cmd_list / cmd_show / cmd_stats)のテストは並列タスクが
別クラス(TestCmdList / TestCmdShow / TestCmdStats)で追加するため、
このリード部分では TestLoader のみを定義しクラス名の衝突を避ける。
"""

import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from runs_cli import loader  # noqa: E402
from runs_cli import cmd_stats  # noqa: E402


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


import io  # noqa: E402
from contextlib import redirect_stdout, redirect_stderr  # noqa: E402

from runs_cli import cmd_list  # noqa: E402
from runs_cli import cmd_stats  # noqa: E402


class _Args:
    """cmd_list.run に渡す args の最小スタブ。"""

    def __init__(self, results_dir=".", ok_only=False, failed_only=False):
        self.results_dir = results_dir
        self.ok_only = ok_only
        self.failed_only = failed_only


def _make_run(request_id, ok=None, exit_code=None, timed_out=None,
              artifacts=None, finished_at=None, broken=False):
    """テスト用に Run を直接組み立てる(loader の公開フィールドを使用)。"""
    return loader.Run(
        request_id=request_id,
        ok=ok,
        exit_code=exit_code,
        timed_out=timed_out,
        artifacts=artifacts or [],
        skipped_artifacts=[],
        failed_uploads=[],
        started_at=None,
        finished_at=finished_at,
        prompt=None,
        branch=None,
        pushed_commit=None,
        summary=None,
        path="/tmp/" + request_id,
        broken=broken,
    )


class TestCmdList(unittest.TestCase):
    def _capture(self, runs, **arg_kwargs):
        """cmd_list.run を実行し (exit_code, stdout, stderr) を返す。"""
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cmd_list.run(runs, _Args(**arg_kwargs))
        return code, out.getvalue(), err.getvalue()

    def _data_lines(self, stdout):
        """ヘッダ行を除いたデータ行(request_id のみ先頭トークンで抽出)。"""
        lines = [ln for ln in stdout.splitlines() if ln.strip()]
        # 先頭はヘッダ。
        return [ln.split()[0] for ln in lines[1:]]

    def test_header_present_and_order(self):
        runs = [_make_run("req-1", ok=True,
                           finished_at="2026-11-04T20:00:00+00:00")]
        code, out, _ = self._capture(runs)
        self.assertEqual(code, 0)
        header = out.splitlines()[0].split()
        self.assertEqual(
            header,
            ["REQUEST_ID", "OK", "EXIT", "TIMED_OUT", "ARTIFACTS", "WHEN"],
        )

    def test_sorted_by_finished_at_desc(self):
        runs = [
            _make_run("old", ok=True,
                      finished_at="2026-11-01T10:00:00+00:00"),
            _make_run("new", ok=True,
                      finished_at="2026-11-03T10:00:00+00:00"),
            _make_run("mid", ok=True,
                      finished_at="2026-11-02T10:00:00+00:00"),
        ]
        _, out, _ = self._capture(runs)
        self.assertEqual(self._data_lines(out), ["new", "mid", "old"])

    def test_missing_finished_at_sorts_last(self):
        runs = [
            _make_run("has-time", ok=True,
                      finished_at="2026-11-02T10:00:00+00:00"),
            _make_run("no-time", ok=True, finished_at=None),
            _make_run("has-time2", ok=True,
                      finished_at="2026-11-03T10:00:00+00:00"),
        ]
        _, out, _ = self._capture(runs)
        self.assertEqual(
            self._data_lines(out), ["has-time2", "has-time", "no-time"]
        )

    def test_broken_displayed_as_broken(self):
        runs = [_make_run("req-broken", broken=True, finished_at=None)]
        _, out, _ = self._capture(runs)
        # OK 列(2 番目のトークン)が broken。
        data_line = [ln for ln in out.splitlines() if ln.strip()][1]
        self.assertEqual(data_line.split()[1], "broken")

    def test_ok_only_filter(self):
        runs = [
            _make_run("ok-run", ok=True,
                      finished_at="2026-11-03T10:00:00+00:00"),
            _make_run("fail-run", ok=False,
                      finished_at="2026-11-02T10:00:00+00:00"),
            _make_run("broken-run", broken=True,
                      finished_at="2026-11-01T10:00:00+00:00"),
        ]
        _, out, _ = self._capture(runs, ok_only=True)
        self.assertEqual(self._data_lines(out), ["ok-run"])

    def test_failed_only_filter_includes_broken(self):
        runs = [
            _make_run("ok-run", ok=True,
                      finished_at="2026-11-03T10:00:00+00:00"),
            _make_run("fail-run", ok=False,
                      finished_at="2026-11-02T10:00:00+00:00"),
            _make_run("broken-run", broken=True,
                      finished_at="2026-11-01T10:00:00+00:00"),
        ]
        _, out, _ = self._capture(runs, failed_only=True)
        # broken は failed に含める方針。降順で fail-run, broken-run の順。
        self.assertEqual(self._data_lines(out), ["fail-run", "broken-run"])

    def test_both_filters_is_error(self):
        runs = [_make_run("req-1", ok=True,
                          finished_at="2026-11-03T10:00:00+00:00")]
        code, out, err = self._capture(runs, ok_only=True, failed_only=True)
        self.assertEqual(code, 2)
        self.assertNotEqual(err.strip(), "")

    def test_artifacts_truncated(self):
        runs = [
            _make_run(
                "req-1", ok=True,
                artifacts=["a", "b", "c", "d", "e"],
                finished_at="2026-11-03T10:00:00+00:00",
            )
        ]
        _, out, _ = self._capture(runs)
        self.assertIn("a,b,c,…", out)
        self.assertNotIn("a,b,c,d", out)

    def test_empty_runs_prints_header_only(self):
        code, out, _ = self._capture([])
        lines = [ln for ln in out.splitlines() if ln.strip()]
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].split()[0], "REQUEST_ID")


class _ShowArgs:
    """argparse.Namespace 相当の簡易 args(cmd_show.run に渡す)。"""

    def __init__(self, request_id, results_dir=None):
        self.request_id = request_id
        self.results_dir = results_dir


class TestCmdShow(unittest.TestCase):
    """cmd_show.run の振る舞いを検証する(TestLoader とは独立)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.results_dir = self._tmp.name
        # cmd_show を遅延 import(loader 以外のモジュール依存を局所化)。
        from runs_cli import cmd_show  # noqa: E402

        self.cmd_show = cmd_show

    def tearDown(self):
        self._tmp.cleanup()

    def _valid_status(self, **overrides):
        status = {
            "requestId": "req-alpha-123",
            "prompt": "do something",
            "exitCode": 0,
            "timedOut": False,
            "artifacts": ["a.txt", "b.txt"],
            "skippedArtifacts": [],
            "failedUploads": [],
            "startedAt": "2026-11-04T20:00:00+00:00",
            "finishedAt": "2026-11-04T20:00:42+00:00",
            "ok": True,
        }
        status.update(overrides)
        return status

    def _make_run(self, name, status=None, report=None, report_md=None):
        d = _make_run_dir(self.results_dir, name)
        st = self._valid_status(requestId=name) if status is None else status
        _write_json(os.path.join(d, "status.json"), st)
        if report is not None:
            _write_json(os.path.join(d, "report.json"), report)
        if report_md is not None:
            with open(os.path.join(d, "report.md"), "w", encoding="utf-8") as f:
                f.write(report_md)
        return d

    def _run_show(self, request_id):
        """cmd_show.run を実行し (exit_code, stdout) を返す。"""
        runs = loader.load_runs(self.results_dir)
        args = _ShowArgs(request_id=request_id, results_dir=self.results_dir)
        buf = io.StringIO()
        old = sys.stdout
        try:
            sys.stdout = buf
            code = self.cmd_show.run(runs, args)
        finally:
            sys.stdout = old
        return code, buf.getvalue()

    def test_exact_match_displays(self):
        self._make_run("req-alpha-123")
        code, out = self._run_show("req-alpha-123")
        self.assertEqual(code, 0)
        self.assertIn("requestId: req-alpha-123", out)
        self.assertIn("exit_code=0", out)
        self.assertIn("timed_out=False", out)
        self.assertIn("prompt: do something", out)
        self.assertIn("a.txt", out)

    def test_prefix_shortened_match(self):
        self._make_run("req-alpha-123")
        self._make_run("req-beta-999")
        # "req-alpha" は req-alpha-123 のみに前方一致する。
        code, out = self._run_show("req-alpha")
        self.assertEqual(code, 0)
        self.assertIn("requestId: req-alpha-123", out)

    def test_exact_preferred_over_prefix(self):
        # "req-1" が完全一致で存在し、"req-12" も前方一致で存在するケース。
        self._make_run("req-1")
        self._make_run("req-12")
        code, out = self._run_show("req-1")
        self.assertEqual(code, 0)
        self.assertIn("requestId: req-1", out)
        # 完全一致を優先するので候補列挙にはならない。
        self.assertNotIn("候補", out)

    def test_multiple_prefix_matches_list_candidates(self):
        self._make_run("req-alpha-1")
        self._make_run("req-alpha-2")
        code, out = self._run_show("req-alpha")
        self.assertNotEqual(code, 0)
        self.assertIn("req-alpha-1", out)
        self.assertIn("req-alpha-2", out)

    def test_no_match_fails(self):
        self._make_run("req-alpha-123")
        code, out = self._run_show("does-not-exist")
        self.assertNotEqual(code, 0)
        self.assertIn("does-not-exist", out)

    def test_report_md_appended(self):
        self._make_run(
            "req-alpha-123",
            report_md="# レポート本文\nこれは report.md の中身です。\n",
        )
        code, out = self._run_show("req-alpha-123")
        self.assertEqual(code, 0)
        self.assertIn("# レポート本文", out)
        self.assertIn("これは report.md の中身です。", out)
        # report.md はフィールド表示の末尾に出る。
        self.assertLess(out.index("requestId:"), out.index("# レポート本文"))

    def test_report_md_absent_not_printed(self):
        self._make_run("req-alpha-123")
        code, out = self._run_show("req-alpha-123")
        self.assertEqual(code, 0)
        self.assertNotIn("report.md", out)

    def test_broken_run_shows_broken(self):
        d = _make_run_dir(self.results_dir, "req-broken")
        with open(os.path.join(d, "status.json"), "w", encoding="utf-8") as f:
            f.write("{ not valid json ")
        code, out = self._run_show("req-broken")
        self.assertEqual(code, 0)
        self.assertIn("broken", out)
        self.assertIn("req-broken", out)

    def test_report_fields_from_report_json(self):
        self._make_run(
            "req-alpha-123",
            report={"branch": "kiro/x", "pushedCommit": "deadbeef", "summary": "s"},
        )
        code, out = self._run_show("req-alpha-123")
        self.assertEqual(code, 0)
        self.assertIn("branch: kiro/x", out)
        self.assertIn("pushedCommit: deadbeef", out)


class TestCmdStats(unittest.TestCase):
    """cmd_stats.run の集計・整形・JSON 出力を検証する(別クラス)。"""

    def _run(self, request_id, ok=None, broken=False, started=None, finished=None):
        """テスト用に Run を直接組む小ヘルパ。"""
        return loader.Run(
            request_id=request_id,
            ok=ok,
            exit_code=None,
            timed_out=None,
            artifacts=[],
            skipped_artifacts=[],
            failed_uploads=[],
            started_at=started,
            finished_at=finished,
            prompt=None,
            branch=None,
            pushed_commit=None,
            summary=None,
            path="/tmp/" + request_id,
            broken=broken,
        )

    def _make_runs(self):
        # 構成:
        # - ok=True, duration 10 秒
        # - ok=True, duration 20 秒
        # - ok=False(失敗), duration 算出不可
        # - broken=True(壊れ), duration 算出不可
        # 合計 4 run / 成功 2 / 失敗 1 / 壊れ 1。
        # 成功率の分母は全 run = 4 -> 2/4 = 50.0%。
        # 平均実行時間は算出可の 2 run のみ -> (10+20)/2 = 15.0 秒。
        return [
            self._run(
                "req-ok-1",
                ok=True,
                started="2026-11-04T20:00:00+00:00",
                finished="2026-11-04T20:00:10+00:00",
            ),
            self._run(
                "req-ok-2",
                ok=True,
                started="2026-11-04T20:00:00+00:00",
                finished="2026-11-04T20:00:20+00:00",
            ),
            self._run("req-fail-1", ok=False),
            self._run("req-broken-1", ok=None, broken=True),
        ]

    class _Args:
        def __init__(self, json=False):
            self.json = json

    def _capture(self, runs, args):
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_stats.run(runs, args)
        return rc, buf.getvalue()

    def test_counts_and_rate_and_avg_json(self):
        runs = self._make_runs()
        rc, out = self._capture(runs, self._Args(json=True))
        self.assertEqual(rc, 0)

        data = json.loads(out)
        self.assertEqual(data["total"], 4)
        self.assertEqual(data["ok"], 2)
        self.assertEqual(data["failed"], 1)
        self.assertEqual(data["broken"], 1)
        # 分母は全 run(4): 2/4 = 50.0%。
        self.assertEqual(data["successRate"], 50.0)
        # 算出可の 2 run のみで平均: (10+20)/2 = 15.0 秒。
        self.assertEqual(data["avgDurationSeconds"], 15.0)

    def test_json_expected_keys(self):
        runs = self._make_runs()
        _, out = self._capture(runs, self._Args(json=True))
        data = json.loads(out)
        self.assertEqual(
            set(data.keys()),
            {"total", "ok", "failed", "broken", "successRate", "avgDurationSeconds"},
        )

    def test_empty_runs_none_values(self):
        rc, out = self._capture([], self._Args(json=True))
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["ok"], 0)
        self.assertEqual(data["failed"], 0)
        self.assertEqual(data["broken"], 0)
        # 総数 0 のとき成功率・平均は None 相当。
        self.assertIsNone(data["successRate"])
        self.assertIsNone(data["avgDurationSeconds"])

    def test_human_output_contains_values(self):
        runs = self._make_runs()
        rc, out = self._capture(runs, self._Args(json=False))
        self.assertEqual(rc, 0)
        # 人間向け表示に主要な数値ラベルが含まれる。
        self.assertIn("総 run 数", out)
        self.assertIn("4", out)
        self.assertIn("50.0%", out)
        self.assertIn("15.0", out)

    def test_human_output_na_when_empty(self):
        rc, out = self._capture([], self._Args(json=False))
        self.assertEqual(rc, 0)
        # 0 件のとき成功率・平均は N/A 表示。
        self.assertIn("N/A", out)


if __name__ == "__main__":
    unittest.main()
