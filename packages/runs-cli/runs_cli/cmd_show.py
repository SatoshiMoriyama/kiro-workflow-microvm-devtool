#!/usr/bin/env python3
"""show subcommand: print the details of one run.

Called from __main__.py as cmd_show.run(runs, args). args.request_id selects
the run: an exact match wins, otherwise a unique requestId prefix is accepted.
With --json the details are printed as one JSON object.

The public loader API (loader.Run / load_runs) is only read, never changed.
"""

from __future__ import annotations

import json
import os
import sys


def _resolve_run(runs, request_id):
    """Resolve one run from request_id. Returns (run, matches).

    - A single exact match wins (matches is [run]).
    - Otherwise a single prefix match is returned.
    - Several prefix matches give run=None and the candidates in matches.
    - No match gives run=None and an empty matches list.
    """
    exact = [r for r in runs if r.request_id == request_id]
    if len(exact) == 1:
        return exact[0], exact
    if len(exact) > 1:
        # requestId should be unique; if not, fall back to listing candidates.
        return None, exact

    prefix = [r for r in runs if r.request_id.startswith(request_id)]
    if len(prefix) == 1:
        return prefix[0], prefix
    return None, prefix


def _format_ok(run) -> str:
    """Format ok together with exit_code and timed_out."""
    return f"{run.ok} (exit_code={run.exit_code}, timed_out={run.timed_out})"


def _read_report_md(run):
    """Return the contents of report.md in the run directory, or None."""
    report_path = os.path.join(run.path, "report.md")
    if not os.path.isfile(report_path):
        return None
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _print_report_md(report_md, out) -> None:
    print("", file=out)
    print("--- report.md ---", file=out)
    out.write(report_md)
    if not report_md.endswith("\n"):
        out.write("\n")


def _print_run(run, out) -> None:
    """Print one run in human-readable form."""
    if run.broken:
        # A broken run only has a few readable fields.
        print("broken: status.json of this run is missing or corrupt", file=out)
        print(f"requestId: {run.request_id}", file=out)
        print(f"path: {run.path}", file=out)
        report_md = _read_report_md(run)
        if report_md is not None:
            _print_report_md(report_md, out)
        return

    print(f"requestId: {run.request_id}", file=out)
    print(f"ok: {_format_ok(run)}", file=out)
    print(f"prompt: {run.prompt}", file=out)
    print(f"branch: {run.branch}", file=out)
    print(f"pushedCommit: {run.pushed_commit}", file=out)

    print("artifacts:", file=out)
    if run.artifacts:
        for a in run.artifacts:
            print(f"  - {a}", file=out)
    else:
        print("  (none)", file=out)

    report_md = _read_report_md(run)
    if report_md is not None:
        _print_report_md(report_md, out)


def _to_dict(run) -> dict:
    """JSON form of one run for `show --json`."""
    return {
        "requestId": run.request_id,
        "ok": run.ok,
        "exitCode": run.exit_code,
        "creditsUsed": run.credits_used,
        "branch": run.branch,
        "pushedCommit": run.pushed_commit,
        "path": run.path,
        "startedAt": run.started_at,
        "finishedAt": run.finished_at,
        "summary": run.summary,
    }


def run(runs, args) -> int:
    """Body of the show subcommand. Returns 0, or non-zero on no/ambiguous match."""
    out = sys.stdout
    err = sys.stderr
    request_id = args.request_id

    target, matches = _resolve_run(runs, request_id)

    if target is None:
        if matches:
            print(f"Multiple runs match '{request_id}'. Candidates:", file=err)
            for r in matches:
                print(f"  - {r.request_id}", file=err)
            return 1
        print(f"No run found matching '{request_id}'", file=err)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(_to_dict(target), ensure_ascii=False), file=out)
        return 0

    _print_run(target, out)
    return 0
