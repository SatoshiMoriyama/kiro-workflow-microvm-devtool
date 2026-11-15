#!/usr/bin/env python3
"""Unit tests for runs_cli (work with both pytest and unittest).

Tests build results/ layouts in temporary directories. Config, cmd_run and
CLI contract tests live in separate files.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

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
            "stdoutKey": "req-1/runs/stdout.txt",
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
        # No report.json, so the report-derived fields are None.
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
        # report.json exists but some keys are missing -> those are None.
        _write_json(os.path.join(d, "report.json"), {"branch": "b"})

        run = loader.load_runs(self.results_dir)[0]
        self.assertEqual(run.branch, "b")
        self.assertIsNone(run.pushed_commit)
        self.assertIsNone(run.summary)

    def test_dir_without_status_is_broken(self):
        d = _make_run_dir(self.results_dir, "req-broken")
        # No status.json, only result.txt.
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
        # The status part is kept.
        self.assertFalse(run.broken)
        self.assertEqual(run.request_id, "req-1")
        self.assertIs(run.ok, True)
        # Only the report side is None.
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

    def test_non_dict_status_is_broken(self):
        # Valid JSON that is not a dict (list/null/string) is broken.
        for name, payload in (
            ("req-list", []),
            ("req-null", None),
            ("req-str", "just a string"),
        ):
            d = _make_run_dir(self.results_dir, name)
            _write_json(os.path.join(d, "status.json"), payload)

        runs = {r.request_id: r for r in loader.load_runs(self.results_dir)}
        for name in ("req-list", "req-null", "req-str"):
            self.assertIn(name, runs)
            self.assertTrue(runs[name].broken, f"{name} should be broken")
            self.assertIsNone(runs[name].ok)

    def test_non_dict_report_keeps_status_report_none(self):
        # A non-dict report.json keeps the status part; report fields are None.
        for name, payload in (
            ("req-rlist", []),
            ("req-rnull", None),
            ("req-rstr", "oops"),
        ):
            d = _make_run_dir(self.results_dir, name)
            _write_json(
                os.path.join(d, "status.json"),
                self._valid_status(requestId=name),
            )
            _write_json(os.path.join(d, "report.json"), payload)

        runs = {r.request_id: r for r in loader.load_runs(self.results_dir)}
        for name in ("req-rlist", "req-rnull", "req-rstr"):
            self.assertIn(name, runs)
            run = runs[name]
            # The status side is kept (not broken, ok is True).
            self.assertFalse(run.broken, f"{name} should keep status")
            self.assertIs(run.ok, True)
            # The report side is None.
            self.assertIsNone(run.branch)
            self.assertIsNone(run.pushed_commit)
            self.assertIsNone(run.summary)

    def test_duration_seconds_none_when_tz_mixed(self):
        # started_at aware and finished_at naive (mixed tz) gives None.
        d = _make_run_dir(self.results_dir, "req-tz")
        _write_json(
            os.path.join(d, "status.json"),
            self._valid_status(
                startedAt="2026-11-04T20:00:00+00:00",
                finishedAt="2026-11-04T20:00:42",
            ),
        )
        run = loader.load_runs(self.results_dir)[0]
        self.assertIsNone(loader.duration_seconds(run))


from contextlib import redirect_stdout, redirect_stderr  # noqa: E402

from runs_cli import cmd_list  # noqa: E402
from runs_cli import cmd_stats  # noqa: E402

class _Args:
    """Minimal args stub for cmd_list.run."""

    def __init__(self, results_dir=".", ok_only=False, failed_only=False):
        self.results_dir = results_dir
        self.ok_only = ok_only
        self.failed_only = failed_only


def _make_run(request_id, ok=None, exit_code=None, timed_out=None,
              artifacts=None, finished_at=None, broken=False):
    """Build a Run directly for tests (uses the loader public fields)."""
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
        """Run cmd_list.run and return (exit_code, stdout, stderr)."""
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cmd_list.run(runs, _Args(**arg_kwargs))
        return code, out.getvalue(), err.getvalue()

    def _data_lines(self, stdout):
        """Data rows without the header (first token = request_id)."""
        lines = [ln for ln in stdout.splitlines() if ln.strip()]
        # The first line is the header.
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
        # The OK column (second token) is "broken".
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
        # broken counts as failed. Descending order: fail-run, broken-run.
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
    """Simple args like argparse.Namespace (passed to cmd_show.run)."""

    def __init__(self, request_id, results_dir=None, json=False):
        self.request_id = request_id
        self.results_dir = results_dir
        self.json = json


class TestCmdShow(unittest.TestCase):
    """Behavior of cmd_show.run (independent of TestLoader)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.results_dir = self._tmp.name
        # Import cmd_show lazily to keep module dependencies local.
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
        """Run cmd_show.run and return (exit_code, stdout)."""
        runs = loader.load_runs(self.results_dir)
        args = _ShowArgs(request_id=request_id, results_dir=self.results_dir)
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = self.cmd_show.run(runs, args)
        return code, buf.getvalue() + err.getvalue()

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
        # "req-alpha" is a prefix of req-alpha-123 only.
        code, out = self._run_show("req-alpha")
        self.assertEqual(code, 0)
        self.assertIn("requestId: req-alpha-123", out)

    def test_exact_preferred_over_prefix(self):
        # "req-1" exists exactly and "req-12" also matches as a prefix.
        self._make_run("req-1")
        self._make_run("req-12")
        code, out = self._run_show("req-1")
        self.assertEqual(code, 0)
        self.assertIn("requestId: req-1", out)
        # The exact match wins, so no candidate list is printed.
        self.assertNotIn("Candidates", out)

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
            report_md="# Report body\nThis is the report.md content.\n",
        )
        code, out = self._run_show("req-alpha-123")
        self.assertEqual(code, 0)
        self.assertIn("# Report body", out)
        self.assertIn("This is the report.md content.", out)
        # report.md comes after the fields.
        self.assertLess(out.index("requestId:"), out.index("# Report body"))

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

    def test_broken_run_with_report_md_appended(self):
        # Even a broken run prints report.md when it exists.
        d = _make_run_dir(self.results_dir, "req-broken-md")
        with open(os.path.join(d, "status.json"), "w", encoding="utf-8") as f:
            f.write("{ not valid json ")
        with open(os.path.join(d, "report.md"), "w", encoding="utf-8") as f:
            f.write("# Report of a broken run\nPartial result body.\n")
        code, out = self._run_show("req-broken-md")
        self.assertEqual(code, 0)
        self.assertIn("broken", out)
        self.assertIn("--- report.md ---", out)
        self.assertIn("# Report of a broken run", out)
        self.assertIn("Partial result body.", out)
        # report.md comes after the broken notice.
        self.assertLess(out.index("broken"), out.index("--- report.md ---"))

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
    """Aggregation, formatting and JSON output of cmd_stats.run."""

    def _run(self, request_id, ok=None, broken=False, started=None, finished=None,
             credits_used=None):
        """Small helper that builds a Run directly."""
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
            credits_used=credits_used,
        )

    def _make_runs(self):
        # Layout:
        # - ok=True, duration 10 s
        # - ok=True, duration 20 s
        # - ok=False (failed), duration unavailable
        # - broken=True, duration unavailable
        # 4 runs in total: 2 ok / 1 failed / 1 broken.
        # The success rate denominator is all runs (4): 2/4 = 50.0%.
        # The average covers the 2 runs with a duration: (10+20)/2 = 15.0 s.
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
        # Denominator is all runs (4): 2/4 = 50.0%.
        self.assertEqual(data["successRate"], 50.0)
        # Average over the 2 runs with a duration: (10+20)/2 = 15.0 s.
        self.assertEqual(data["avgDurationSeconds"], 15.0)

    def test_json_expected_keys(self):
        runs = self._make_runs()
        _, out = self._capture(runs, self._Args(json=True))
        data = json.loads(out)
        self.assertEqual(
            set(data.keys()),
            {
                "total", "ok", "failed", "broken", "successRate",
                "avgDurationSeconds", "creditsTotal", "creditsUnavailable",
            },
        )

    def test_empty_runs_none_values(self):
        rc, out = self._capture([], self._Args(json=True))
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["ok"], 0)
        self.assertEqual(data["failed"], 0)
        self.assertEqual(data["broken"], 0)
        # With no runs, success rate and average are None.
        self.assertIsNone(data["successRate"])
        self.assertIsNone(data["avgDurationSeconds"])

    def test_human_output_contains_values(self):
        runs = self._make_runs()
        rc, out = self._capture(runs, self._Args(json=False))
        self.assertEqual(rc, 0)
        # The human output contains the main labels.
        self.assertIn("Total runs", out)
        self.assertIn("4", out)
        self.assertIn("50.0%", out)
        self.assertIn("15.0", out)

    def test_human_output_na_when_empty(self):
        rc, out = self._capture([], self._Args(json=False))
        self.assertEqual(rc, 0)
        # With no runs, success rate and average show N/A.
        self.assertIn("N/A", out)

    def test_total_equals_ok_plus_failed_plus_broken(self):
        # Even with a non-broken run whose ok is None,
        # total == ok + failed + broken holds.
        runs = [
            self._run("req-ok", ok=True),
            self._run("req-false", ok=False),
            self._run("req-none", ok=None),       # non-broken, ok None -> failed
            self._run("req-broken", ok=None, broken=True),
        ]
        rc, out = self._capture(runs, self._Args(json=True))
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(
            data["total"], data["ok"] + data["failed"] + data["broken"]
        )
        self.assertEqual(data["total"], 4)
        self.assertEqual(data["ok"], 1)
        self.assertEqual(data["failed"], 2)
        self.assertEqual(data["broken"], 1)

    def test_credits_total_and_unavailable(self):
        runs = [
            self._run("c1", ok=True, credits_used=1.5),
            self._run("c2", ok=False, credits_used=2.25),
            self._run("c3", ok=True),                      # None
            self._run("c4", broken=True),                  # broken is also None
        ]
        _, out = self._capture(runs, self._Args(json=True))
        data = json.loads(out)
        self.assertEqual(data["creditsTotal"], 3.75)
        self.assertEqual(data["creditsUnavailable"], 2)
        self.assertEqual(data["creditsUnavailable"] + 2, data["total"])

    def test_credits_all_none(self):
        runs = [self._run("n1", ok=True), self._run("n2", broken=True)]
        _, out = self._capture(runs, self._Args(json=True))
        data = json.loads(out)
        self.assertIsNone(data["creditsTotal"])
        self.assertEqual(data["creditsUnavailable"], 2)
        _, human = self._capture(runs, self._Args(json=False))
        self.assertIn("Credits total", human)
        self.assertIn("2 unavailable", human)
        self.assertIn("N/A", human)

    def test_credits_empty_runs(self):
        _, out = self._capture([], self._Args(json=True))
        data = json.loads(out)
        self.assertIsNone(data["creditsTotal"])
        self.assertEqual(data["creditsUnavailable"], 0)

    def test_credits_human_output(self):
        runs = [self._run("h1", ok=True, credits_used=0.5),
                self._run("h2", ok=True)]
        _, out = self._capture(runs, self._Args(json=False))
        self.assertIn("Credits total", out)
        self.assertIn("0.5 credits", out)
        self.assertIn("1 unavailable", out)


class TestLoaderCredits(unittest.TestCase):
    """The loader maps creditsUsed in status.json to credits_used."""

    def _load_one(self, status_extra, key_present=True, value=None):
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_run_dir(tmp, "req-c")
            status = {"requestId": "req-c", "ok": True}
            if key_present:
                status["creditsUsed"] = value
            _write_json(os.path.join(d, "status.json"), status)
            runs = loader.load_runs(tmp)
        self.assertEqual(len(runs), 1)
        return runs[0]

    def test_int_and_float_become_float(self):
        r = self._load_one(None, value=3)
        self.assertEqual(r.credits_used, 3.0)
        self.assertIsInstance(r.credits_used, float)
        r = self._load_one(None, value=0.123456789)
        self.assertEqual(r.credits_used, 0.123456789)

    def test_non_numeric_become_none(self):
        self.assertIsNone(self._load_one(None, value=None).credits_used)
        self.assertIsNone(self._load_one(None, key_present=False).credits_used)
        self.assertIsNone(self._load_one(None, value="1.5").credits_used)
        self.assertIsNone(self._load_one(None, value=True).credits_used)
        self.assertIsNone(self._load_one(None, value=False).credits_used)

    def test_non_finite_become_none(self):
        for v in (float("nan"), float("inf"), float("-inf")):
            self.assertIsNone(self._load_one(None, value=v).credits_used)

    def test_broken_run_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_run_dir(tmp, "req-b")  # no status.json -> broken
            runs = loader.load_runs(tmp)
        self.assertTrue(runs[0].broken)
        self.assertIsNone(runs[0].credits_used)


class TestMainDispatch(unittest.TestCase):
    """Dispatch through runs_cli.__main__.main(argv)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.results_dir = self._tmp.name
        d = _make_run_dir(self.results_dir, "req-main-1")
        _write_json(
            os.path.join(d, "status.json"),
            {
                "requestId": "req-main-1",
                "prompt": "p",
                "exitCode": 0,
                "timedOut": False,
                "artifacts": [],
                "skippedArtifacts": [],
                "failedUploads": [],
                "startedAt": "2026-11-04T20:00:00+00:00",
                "finishedAt": "2026-11-04T20:00:10+00:00",
                "ok": True,
            },
        )
        from runs_cli import __main__ as main_mod

        self.main = main_mod.main

    def tearDown(self):
        self._tmp.cleanup()

    def _run_main(self, argv):
        buf = io.StringIO()
        old = sys.stdout
        try:
            sys.stdout = buf
            code = self.main(argv)
        finally:
            sys.stdout = old
        return code, buf.getvalue()

    def test_list_dispatch_exit_zero(self):
        code, out = self._run_main(
            ["list", "--results-dir", self.results_dir]
        )
        self.assertEqual(code, 0)
        self.assertIn("req-main-1", out)

    def test_stats_dispatch_exit_zero(self):
        code, out = self._run_main(
            ["stats", "--results-dir", self.results_dir, "--json"]
        )
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["total"], 1)


class TestJsonAndDefaultDir(unittest.TestCase):
    """--json output of show/list and the default results directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.results_dir = self._tmp.name
        d = _make_run_dir(self.results_dir, "req-j-1")
        _write_json(os.path.join(d, "status.json"), {
            "requestId": "req-j-1", "ok": True, "exitCode": 0,
            "creditsUsed": 1.5,
            "startedAt": "2026-11-04T20:00:00+00:00",
            "finishedAt": "2026-11-04T20:00:10+00:00",
        })
        _write_json(os.path.join(d, "report.json"), {
            "branch": "kiro/x", "pushedCommit": "abc", "summary": "did it",
        })
        from runs_cli import __main__ as main_mod

        self.main = main_mod.main

    def tearDown(self):
        self._tmp.cleanup()

    def _run_main(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = self.main(argv)
        return code, buf.getvalue()

    def test_show_json_fields(self):
        code, out = self._run_main(
            ["show", "req-j-1", "--json", "--results-dir", self.results_dir]
        )
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(
            set(data.keys()),
            {"requestId", "ok", "exitCode", "creditsUsed", "branch",
             "pushedCommit", "path", "startedAt", "finishedAt", "summary"},
        )
        self.assertEqual(data["requestId"], "req-j-1")
        self.assertIs(data["ok"], True)
        self.assertEqual(data["creditsUsed"], 1.5)
        self.assertEqual(data["branch"], "kiro/x")
        self.assertEqual(data["summary"], "did it")

    def test_list_json(self):
        code, out = self._run_main(
            ["list", "--json", "--results-dir", self.results_dir]
        )
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual([r["requestId"] for r in data], ["req-j-1"])

    def test_default_results_dir_from_env(self):
        with mock.patch.dict(os.environ, {"KIRO_MICROVM_RESULTS_DIR": self.results_dir}):
            self.assertEqual(loader.default_results_dir(), os.path.abspath(self.results_dir))
            code, out = self._run_main(["list", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)), 1)

    def test_default_results_dir_is_not_repo_relative(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("KIRO_MICROVM_RESULTS_DIR", "XDG_DATA_HOME")}
        env["XDG_CONFIG_HOME"] = self.results_dir  # no config file there
        with mock.patch.dict(os.environ, env, clear=True):
            path = loader.default_results_dir()
        self.assertTrue(path.endswith(os.path.join(".local", "share", "kiro-microvm", "results")))

    def test_default_results_dir_honors_xdg_data_home(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/x/data"}):
            os.environ.pop("KIRO_MICROVM_RESULTS_DIR", None)
            os.environ["XDG_CONFIG_HOME"] = self.results_dir
            self.assertEqual(
                loader.default_results_dir(),
                os.path.join("/x/data", "kiro-microvm", "results"),
            )


if __name__ == "__main__":
    unittest.main()
