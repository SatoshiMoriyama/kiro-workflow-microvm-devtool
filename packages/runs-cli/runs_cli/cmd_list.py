#!/usr/bin/env python3
"""list subcommand.

Prints the runs returned by the loader as a fixed-width table, or as JSON
with --json. Only the standard library is used.

Semantics of the OK column and the failed filter:
- A run with broken=True is shown as "broken" and counts as failed, because
  its status.json could not be read and so it is not a success.
- --ok-only keeps runs with ok=True (broken excluded).
- --failed-only keeps runs with ok=False or broken.
- --ok-only and --failed-only together are an error (exit code 2). argparse
  already makes them exclusive; run() checks again defensively.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

# Column headers; the column order is fixed.
_HEADERS = ["REQUEST_ID", "OK", "EXIT", "TIMED_OUT", "ARTIFACTS", "WHEN"]

# Max artifacts shown in the ARTIFACTS column; the rest becomes "…".
_MAX_ARTIFACTS = 3


def _parse_finished(value):
    """Parse finished_at (ISO8601 string) into a datetime. Missing/unparsable gives None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _sort_key(run):
    """Sort key: finished_at descending, missing/unparsable last.

    Meant for sorted(reverse=True); returns (valid flag, datetime). A missing
    value gets flag 0 and datetime.min so it always ends up last.
    """
    dt = _parse_finished(run.finished_at)
    if dt is None:
        return (0, datetime.min)
    # Mixed tz-aware/naive values cannot be compared, so normalize to naive.
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return (1, dt)


def _format_ok(run) -> str:
    """Return the text for the OK column."""
    if run.broken:
        return "broken"
    if run.ok is True:
        return "true"
    if run.ok is False:
        return "false"
    return "-"


def _format_exit(run) -> str:
    """EXIT column; '-' when exit_code is None."""
    if run.exit_code is None:
        return "-"
    return str(run.exit_code)


def _format_timed_out(run) -> str:
    """TIMED_OUT column; '-' when None."""
    if run.timed_out is True:
        return "true"
    if run.timed_out is False:
        return "false"
    return "-"


def _format_artifacts(run) -> str:
    """ARTIFACTS column: comma separated; long lists are cut with '…'."""
    artifacts = run.artifacts or []
    if not artifacts:
        return "-"
    if len(artifacts) > _MAX_ARTIFACTS:
        shown = artifacts[:_MAX_ARTIFACTS]
        return ",".join(shown) + ",…"
    return ",".join(artifacts)


def _format_when(run) -> str:
    """WHEN column: finished_at in local time; '-' if missing/unparsable."""
    dt = _parse_finished(run.finished_at)
    if dt is None:
        return "-"
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _passes_filter(run, ok_only: bool, failed_only: bool) -> bool:
    """Return whether the run passes the filter."""
    if ok_only:
        return run.ok is True and not run.broken
    if failed_only:
        return run.broken or run.ok is False
    return True


def _to_dict(run) -> dict:
    """JSON form of one run for `list --json`."""
    return {
        "requestId": run.request_id,
        "ok": run.ok,
        "exitCode": run.exit_code,
        "timedOut": run.timed_out,
        "broken": run.broken,
        "artifacts": run.artifacts,
        "startedAt": run.started_at,
        "finishedAt": run.finished_at,
        "path": run.path,
    }


def run(runs, args) -> int:
    """Entry point of the list subcommand. Returns 0 on success."""
    ok_only = bool(getattr(args, "ok_only", False))
    failed_only = bool(getattr(args, "failed_only", False))

    # Both filters at once is not allowed.
    if ok_only and failed_only:
        print(
            "error: --ok-only and --failed-only cannot be used together.",
            file=sys.stderr,
        )
        return 2

    filtered = [r for r in runs if _passes_filter(r, ok_only, failed_only)]

    # finished_at descending, missing last.
    ordered = sorted(filtered, key=_sort_key, reverse=True)

    if getattr(args, "json", False):
        print(json.dumps([_to_dict(r) for r in ordered], ensure_ascii=False))
        return 0

    rows = []
    for r in ordered:
        rows.append(
            [
                str(r.request_id),
                _format_ok(r),
                _format_exit(r),
                _format_timed_out(r),
                _format_artifacts(r),
                _format_when(r),
            ]
        )

    # Fixed width: each column is as wide as its header and widest cell.
    widths = [len(h) for h in _HEADERS]
    for row in rows:
        for i, cell in enumerate(row):
            if len(cell) > widths[i]:
                widths[i] = len(cell)

    def _fmt_line(cells) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    print(_fmt_line(_HEADERS))
    for row in rows:
        print(_fmt_line(row))

    return 0
