#!/usr/bin/env python3
"""Loader that reads run results and normalizes them into Run dataclasses.

The public API (Run / load_runs / duration_seconds / default_results_dir) is
a contract with cmd_list / cmd_show / cmd_stats; keep names and signatures.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Run:
    """One normalized run.

    Keep the snake_case field names; other modules import them.

    credits_used is the kiro-cli credit usage (unit: credit, not rounded). It is
    a float only when status.json has a numeric creditsUsed. It is None (not
    available) for null, a missing key (old runs), non-numeric values
    (including NaN/Infinity) and broken runs.
    """

    request_id: str
    ok: bool | None
    exit_code: int | None
    timed_out: bool | None
    artifacts: list[str]
    skipped_artifacts: list[str]
    failed_uploads: list[str]
    started_at: str | None
    finished_at: str | None
    prompt: str | None
    branch: str | None
    pushed_commit: str | None
    summary: str | None
    path: str
    broken: bool = False
    credits_used: float | None = None


def _read_json(path: str):
    """Read a JSON file. Raises if it is missing or malformed."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _parse_credits(value) -> float | None:
    """Normalize creditsUsed to float. Non-numbers (bool included) and NaN/Infinity give None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _run_from_status(request_id: str, abs_dir: str, status: dict) -> Run:
    """Build a Run from a status.json dict (report.json fields are unset)."""
    return Run(
        request_id=status.get("requestId", request_id),
        ok=status.get("ok"),
        exit_code=status.get("exitCode"),
        timed_out=status.get("timedOut"),
        artifacts=status.get("artifacts") or [],
        skipped_artifacts=status.get("skippedArtifacts") or [],
        failed_uploads=status.get("failedUploads") or [],
        started_at=status.get("startedAt"),
        finished_at=status.get("finishedAt"),
        prompt=status.get("prompt"),
        branch=None,
        pushed_commit=None,
        summary=None,
        path=abs_dir,
        broken=False,
        credits_used=_parse_credits(status.get("creditsUsed")),
    )


def _broken_run(request_id: str, abs_dir: str) -> Run:
    """Return a broken Run for a missing or corrupt status.json."""
    return Run(
        request_id=request_id,
        ok=None,
        exit_code=None,
        timed_out=None,
        artifacts=[],
        skipped_artifacts=[],
        failed_uploads=[],
        started_at=None,
        finished_at=None,
        prompt=None,
        branch=None,
        pushed_commit=None,
        summary=None,
        path=abs_dir,
        broken=True,
    )


def load_runs(results_dir: str) -> list[Run]:
    """Load each subdirectory of results_dir as one run.

    - A missing or corrupt status.json yields a broken=True Run (no crash).
    - If report.json exists, branch / pushed_commit / summary are taken from it.
      A corrupt report.json keeps the status part and leaves only those None.
    - A nonexistent results_dir yields an empty list.
    """
    if not os.path.isdir(results_dir):
        return []

    runs: list[Run] = []
    for name in sorted(os.listdir(results_dir)):
        sub = os.path.join(results_dir, name)
        if not os.path.isdir(sub):
            continue
        abs_dir = os.path.abspath(sub)

        status_path = os.path.join(sub, "status.json")
        try:
            status = _read_json(status_path)
        except (FileNotFoundError, ValueError, OSError):
            runs.append(_broken_run(name, abs_dir))
            continue

        # Valid JSON that is not a dict (e.g. []/null/string) would make
        # status.get raise AttributeError, so treat it as broken.
        if not isinstance(status, dict):
            runs.append(_broken_run(name, abs_dir))
            continue

        run = _run_from_status(name, abs_dir, status)

        report_path = os.path.join(sub, "report.json")
        if os.path.isfile(report_path):
            try:
                report = _read_json(report_path)
            except (ValueError, OSError):
                # Corrupt report.json: keep the status part, report fields stay None.
                report = None

            # Only a dict report is read. Otherwise keep the status part and
            # leave the report fields None.
            if isinstance(report, dict):
                run.branch = report.get("branch")
                run.pushed_commit = report.get("pushedCommit")
                run.summary = report.get("summary")
            else:
                run.branch = None
                run.pushed_commit = None
                run.summary = None

        runs.append(run)

    return runs


def duration_seconds(run: Run) -> float | None:
    """Return finished_at - started_at in seconds.

    Both are parsed as ISO8601 with datetime.fromisoformat. Returns None if
    either is missing or unparsable.
    """
    if not run.started_at or not run.finished_at:
        return None
    try:
        start = datetime.fromisoformat(run.started_at)
        finish = datetime.fromisoformat(run.finished_at)
        # Mixing aware and naive datetimes raises TypeError on subtraction, so
        # the subtraction stays inside the try and falls back to None.
        return (finish - start).total_seconds()
    except (ValueError, TypeError):
        return None


def default_results_dir() -> str:
    """Return the default results directory.

    KIRO_MICROVM_RESULTS_DIR (environment or config file), otherwise
    ~/.local/share/kiro-microvm/results (honoring XDG_DATA_HOME).
    """
    from runs_cli import config

    return config.resolve().results_dir
